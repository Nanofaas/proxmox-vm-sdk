"""The ProxmoxVM handle for a single VM on a Proxmox VE node.

A ProxmoxVM is returned by ``ProxmoxClient.get_vm()``, ``find_vm()`` and
``create_vm()``, and wraps the per-VM REST endpoints: state queries, lifecycle
control, cloning, snapshots, guest-agent command execution, disk resizing and
cloud-init configuration. State-mutating methods block until the asynchronous
Proxmox task they start has completed.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from proxmox_sdk._backend import CommandResult
from proxmox_sdk.exceptions import ProxmoxTimeoutError, SnapshotNotFoundError
from proxmox_sdk.models import (
    CloudInitConfig,
    SnapshotInfo,
    VmInfo,
    VmMetrics,
)

if TYPE_CHECKING:
    from proxmox_sdk._backend import ProxmoxBackend


class ProxmoxVM:
    """Represents a single Proxmox VM instance.

    Returned by ProxmoxClient.get_vm() / find_vm() / create_vm().
    All state-mutating methods block until the async Proxmox task completes.
    """

    def __init__(
        self,
        vm_id: int,
        node: str,
        backend: ProxmoxBackend,
    ) -> None:
        """Bind a VM handle to its node and backend.

        Instances are normally obtained through ``ProxmoxClient.get_vm()``,
        ``find_vm()`` or ``create_vm()`` rather than constructed directly;
        ``backend`` is the object used for every REST call made by this handle.
        """
        self.vm_id = vm_id
        self.node = node
        self._backend = backend

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def info(self) -> VmInfo:
        """Return current VM configuration and status."""
        data = self._backend.get(f"nodes/{self.node}/qemu/{self.vm_id}/status/current")
        return VmInfo.from_api(data)

    def metrics(self) -> VmMetrics:
        """Return real-time CPU/memory metrics via cluster.resources."""
        resources = self._backend.get("cluster/resources", type="vm")
        for entry in resources:
            if int(entry.get("vmid", -1)) == self.vm_id:
                return VmMetrics.from_api(entry)
        # VM exists but is not in cluster resources (e.g. template)
        return VmMetrics(
            vm_id=self.vm_id,
            cpu_pct=0.0,
            mem_used_bytes=0,
            mem_total_bytes=0,
            mem_used_pct=0.0,
            net_in_bytes=0,
            net_out_bytes=0,
            disk_read_bytes=0,
            disk_write_bytes=0,
        )

    # ------------------------------------------------------------------
    # Lifecycle control
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Power on the VM."""
        upid = self._backend.post(f"nodes/{self.node}/qemu/{self.vm_id}/status/start")
        self._backend.wait_for_task(self.node, upid)

    def stop(self, *, timeout: int = 30) -> None:
        """Hard-stop the VM (immediate power off)."""
        upid = self._backend.post(
            f"nodes/{self.node}/qemu/{self.vm_id}/status/stop",
        )
        self._backend.wait_for_task(self.node, upid, timeout=timeout)

    def shutdown(self) -> None:
        """Send ACPI shutdown signal (graceful)."""
        upid = self._backend.post(
            f"nodes/{self.node}/qemu/{self.vm_id}/status/shutdown"
        )
        self._backend.wait_for_task(self.node, upid)

    def restart(self) -> None:
        """Reboot the VM."""
        upid = self._backend.post(f"nodes/{self.node}/qemu/{self.vm_id}/status/reboot")
        self._backend.wait_for_task(self.node, upid)

    def delete(self, *, purge: bool = False) -> None:
        """Delete the VM. Pass purge=True to also remove from job configs."""
        params: dict[str, Any] = {}
        if purge:
            params["purge"] = 1
        upid = self._backend.delete(f"nodes/{self.node}/qemu/{self.vm_id}", **params)
        self._backend.wait_for_task(self.node, upid)

    # ------------------------------------------------------------------
    # Clone & snapshots
    # ------------------------------------------------------------------

    def clone(
        self,
        new_vm_id: int,
        name: str,
        *,
        node: str | None = None,
        full: bool = True,
    ) -> ProxmoxVM:
        """Clone this VM. Returns the new ProxmoxVM instance."""
        target_node = node or self.node
        upid = self._backend.post(
            f"nodes/{self.node}/qemu/{self.vm_id}/clone",
            newid=new_vm_id,
            name=name,
            target=target_node,
            full=int(full),
        )
        self._backend.wait_for_task(self.node, upid)
        return ProxmoxVM(new_vm_id, target_node, self._backend)

    def snapshot(self, name: str, description: str = "") -> SnapshotInfo:
        """Create a snapshot. Returns the SnapshotInfo for it."""
        upid = self._backend.post(
            f"nodes/{self.node}/qemu/{self.vm_id}/snapshots",
            snapname=name,
            description=description,
        )
        self._backend.wait_for_task(self.node, upid)
        for snap in self.list_snapshots():
            if snap.name == name:
                return snap
        # Should not happen, but return a minimal object if not found
        import time as _time

        return SnapshotInfo(
            name=name,
            vm_id=self.vm_id,
            created=int(_time.time()),
            description=description,
        )

    def restore(self, snapshot: str) -> None:
        """Roll back to a snapshot by name."""
        snaps = self.list_snapshots()
        if not any(s.name == snapshot for s in snaps):
            raise SnapshotNotFoundError(self.vm_id, snapshot)
        upid = self._backend.post(
            f"nodes/{self.node}/qemu/{self.vm_id}/snapshots/{snapshot}/rollback"
        )
        self._backend.wait_for_task(self.node, upid)

    def list_snapshots(self) -> list[SnapshotInfo]:
        """Return all snapshots for this VM."""
        raw = self._backend.get(f"nodes/{self.node}/qemu/{self.vm_id}/snapshots")
        return [
            SnapshotInfo.from_api(s, vm_id=self.vm_id)
            for s in raw
            if s.get("name") != "current"
        ]

    # ------------------------------------------------------------------
    # Command execution (QEMU guest agent)
    # ------------------------------------------------------------------

    def exec(self, command: list[str], *, timeout: float = 30.0) -> CommandResult:
        """Run a command inside the VM via QEMU guest agent."""
        from proxmox_sdk.exceptions import ProxmoxAPIError

        result = self._backend.post(
            f"nodes/{self.node}/qemu/{self.vm_id}/agent/exec",
            command=command,
        )
        pid = result.get("pid", 0)
        deadline = time.monotonic() + timeout
        interval = 0.5
        while time.monotonic() < deadline:
            status = self._backend.get(
                f"nodes/{self.node}/qemu/{self.vm_id}/agent/exec-status",
                pid=pid,
            )
            if status.get("exited"):
                return CommandResult(
                    exit_code=int(status.get("exitcode", 1)),
                    stdout=status.get("out-data", ""),
                    stderr=status.get("err-data", ""),
                )
            if "error" in status:
                raise ProxmoxAPIError(
                    status_code=500,
                    message=str(status.get("error", "guest agent error")),
                    path=f"nodes/{self.node}/qemu/{self.vm_id}/agent/exec-status",
                )
            time.sleep(interval)
            interval = min(interval * 1.5, 2.0)
        raise ProxmoxTimeoutError(self.vm_id, "exec", timeout)

    def exec_structured(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        """Run a command with env vars and working directory via guest agent."""
        from shlex import quote

        parts: list[str] = []
        if cwd:
            parts.append(f"cd {quote(cwd)}")
        for k, v in (env or {}).items():
            parts.append(f"export {k}={quote(v)}")
        parts.append(" ".join(quote(a) for a in argv))
        command = " && ".join(parts)
        return self.exec(["bash", "-lc", command])

    def transfer(self, source: str, dest: str) -> None:
        """Transfer a file to/from the VM via SSH.

        Requires NAT port forwarding (ProxmoxRoutingManager) and SSH access
        to the VM. Not implemented via QEMU guest agent.
        """
        raise NotImplementedError(
            "transfer() requires an SSH connection to the VM. "
            "Use ProxmoxRoutingManager for NAT port forwarding first."
        )

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    def wait_for_agent(self, timeout: float = 120, *, interval: float = 2.0) -> None:
        """Poll until the QEMU guest agent responds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Broad on purpose: a VM still booting fails this ping in a
            # different way on every backend (an API error, a missing route, a
            # connection reset), and all of them mean "not ready yet". The
            # deadline below is what turns a permanent failure into a timeout.
            with contextlib.suppress(Exception):
                self._backend.post(f"nodes/{self.node}/qemu/{self.vm_id}/agent/ping")
                return
            time.sleep(interval)
        raise ProxmoxTimeoutError(self.vm_id, "wait_for_agent", timeout)

    def wait_for_ip(self, timeout: float = 120, *, interval: float = 2.0) -> str:
        """Poll QEMU guest agent until an IPv4 address is available."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Same broad suppression as wait_for_agent: the guest agent has no
            # interfaces to report until the VM finishes booting.
            with contextlib.suppress(Exception):
                data = self._backend.get(
                    f"nodes/{self.node}/qemu/{self.vm_id}/agent/network-get-interfaces"
                )
                for iface in data.get("result", []):
                    if iface.get("name") in ("lo", "lo0"):
                        continue
                    for addr in iface.get("ip-addresses", []):
                        if addr.get("ip-address-type") == "ipv4":
                            return str(addr["ip-address"])
            time.sleep(interval)
        raise ProxmoxTimeoutError(self.vm_id, "wait_for_ip", timeout)

    def wait_ready(self, timeout: float = 120, *, interval: float = 2.0) -> None:
        """Wait until the VM is running and the guest agent is responding."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Same broad suppression again: a VM that is still starting raises
            # from info() or from the agent ping depending on how far along it
            # is, and every one of those means "keep polling".
            with contextlib.suppress(Exception):
                info = self.info()
                if info.state.value == "running":
                    self.wait_for_agent(
                        timeout=max(1.0, deadline - time.monotonic()),
                        interval=interval,
                    )
                    return
            time.sleep(interval)
        raise ProxmoxTimeoutError(self.vm_id, "wait_ready", timeout)

    # ------------------------------------------------------------------
    # Disk operations
    # ------------------------------------------------------------------

    def resize_disk(self, disk: str, size: str) -> None:
        """Resize a disk. size is a delta like '+10G' or absolute like '50G'."""
        self._backend.put(
            f"nodes/{self.node}/qemu/{self.vm_id}/resize",
            disk=disk,
            size=size,
        )

    def has_cloud_init_drive(self) -> bool:
        """Return True if the VM config contains a cloud-init CD-ROM drive."""
        config = self._backend.get(f"nodes/{self.node}/qemu/{self.vm_id}/config")
        return any(
            "cloudinit" in str(v).lower() for v in config.values() if isinstance(v, str)
        )

    def configure_cloud_init(self, config: CloudInitConfig) -> None:
        """Apply cloud-init configuration to this VM via the Proxmox config API.

        Must be called while the VM is stopped (cloud-init is applied at next boot).
        Has no effect if config carries no fields.

        Example::

            vm.configure_cloud_init(CloudInitConfig(
                username="ubuntu",
                ssh_keys=["ssh-rsa AAAA..."],
                ip_config="ip=dhcp",
            ))
        """
        params = config.to_api_params()
        if not params:
            return
        self._backend.put(
            f"nodes/{self.node}/qemu/{self.vm_id}/config",
            **params,
        )

    def __repr__(self) -> str:
        """Return a debug string naming the VM and its host node."""
        return f"ProxmoxVM(vm_id={self.vm_id}, node={self.node!r})"
