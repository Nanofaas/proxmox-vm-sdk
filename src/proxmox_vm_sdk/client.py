"""ProxmoxClient, the entry point for talking to a Proxmox VE cluster.

The client wraps a backend (proxmoxer by default, or any object implementing
the `ProxmoxBackend` protocol) and exposes the high-level operations an SDK
caller needs: look up, list, clone, and manage VMs and templates.
"""

from __future__ import annotations

import builtins
import contextlib
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from proxmox_vm_sdk._backend import ProxmoxBackend
from proxmox_vm_sdk._utils import parse_proxmox_url
from proxmox_vm_sdk.exceptions import ProxmoxError, VmNotFoundError
from proxmox_vm_sdk.models import (
    CloudInitConfig,
    NodeInfo,
    TemplateInfo,
    VmConfig,
    VmInfo,
)
from proxmox_vm_sdk.vm import ProxmoxVM


class ProxmoxClient:
    """Entry point for the proxmox-vm-sdk.

    Usage::

        client = ProxmoxClient(host="192.168.1.100", user="root@pam", password="secret")
        vms = client.list()
        vm = client.get_vm(100)
        vm.start()

    For testing, inject a FakeBackend::

        from proxmox_vm_sdk.testing import FakeBackend
        fake = FakeBackend()
        fake.add_vm(100, name="test-vm")
        client = ProxmoxClient(host="x", user="x", backend=fake)
    """

    def __init__(
        self,
        host: str,
        user: str,
        password: str | None = None,
        token_name: str | None = None,
        token_value: str | None = None,
        port: int = 8006,
        verify_ssl: bool = False,
        node: str | None = None,
        backend: ProxmoxBackend | None = None,
    ) -> None:
        """Connect to a Proxmox VE host.

        Authenticate either with `password` or with an API token
        (`token_name` and `token_value`); if `backend` is given it is used as
        is and the connection arguments are ignored. `node` pins operations
        that would otherwise span the cluster to a single node.
        """
        self._host = host
        self._user = user
        self._node = node

        if backend is not None:
            self._backend = backend
        else:
            self._backend = self._build_backend(
                host=host,
                user=user,
                password=password,
                token_name=token_name,
                token_value=token_value,
                port=port,
                verify_ssl=verify_ssl,
            )

    @classmethod
    def from_url(
        cls,
        api_url: str,
        user: str,
        password: str | None = None,
        *,
        token_name: str | None = None,
        token_value: str | None = None,
        verify_ssl: bool = False,
        node: str | None = None,
    ) -> ProxmoxClient:
        """Build a client from a full API URL such as `https://host:8006/`.

        The host and port are parsed out of `api_url`; the remaining arguments
        are forwarded to the constructor unchanged.
        """
        host, port = parse_proxmox_url(api_url)
        return cls(
            host=host,
            user=user,
            password=password,
            token_name=token_name,
            token_value=token_value,
            port=port,
            verify_ssl=verify_ssl,
            node=node,
        )

    # ------------------------------------------------------------------
    # get_vm
    # ------------------------------------------------------------------

    def get_vm(self, identifier: int | str) -> ProxmoxVM:
        """Return the VM with the given numeric ID or name.

        Raises VmNotFoundError if no matching VM exists.
        """
        if isinstance(identifier, int) or identifier.isdigit():
            return self._get_vm_by_id(int(identifier))
        return self._get_vm_by_name(identifier)

    def _get_vm_by_id(self, vmid: int) -> ProxmoxVM:
        node = self._node_for_vm(vmid)
        return ProxmoxVM(vmid, node, self._backend)

    def _get_vm_by_name(self, name: str) -> ProxmoxVM:
        for vm in self._all_vms():
            if vm.get("name") == name:
                return ProxmoxVM(int(vm["vmid"]), vm["node"], self._backend)
        raise VmNotFoundError(name)

    # ------------------------------------------------------------------
    # launch
    # ------------------------------------------------------------------

    def launch(
        self,
        name_or_config: str | VmConfig | None = None,
        template_id: int | None = None,
        node: str | None = None,
        *,
        cores: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        cloud_init_config: CloudInitConfig | None = None,
        start: bool = True,
        timeout: float = 300.0,
    ) -> ProxmoxVM:
        """Clone a template VM, optionally apply cloud-init, and start it.

        Accepts either a VmConfig object or keyword arguments::

            vm = client.launch("node-1", template_id=9000, cores=4, memory_mb=4096)

            vm = client.launch(VmConfig(name="node-1", template_id=9000, cores=4))

        Returns the new ProxmoxVM instance.
        """
        if isinstance(name_or_config, VmConfig):
            cfg = name_or_config
        else:
            cfg = VmConfig(
                name=name_or_config or uuid.uuid4().hex[:8],
                template_id=template_id,
                node=node,
                cores=cores,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                cloud_init_config=cloud_init_config,
                start=start,
            )

        name = cfg.name or uuid.uuid4().hex[:8]
        tid = cfg.template_id or 0
        target_node = cfg.node or node or self._default_node()
        new_vmid = self._next_vmid()

        if cfg.cloud_init_config is not None:
            template = ProxmoxVM(tid, target_node, self._backend)
            if not template.has_cloud_init_drive():
                raise ProxmoxError(
                    f"Template {tid} has no cloud-init drive. "
                    "Attach a cloud-init CD-ROM to the template in Proxmox before "
                    "using cloud_init_config."
                )

        upid = self._backend.post(
            f"nodes/{target_node}/qemu/{tid}/clone",
            newid=new_vmid,
            name=name,
            target=target_node,
            full=1,
        )
        self._backend.wait_for_task(target_node, upid, timeout=timeout)

        vm = ProxmoxVM(new_vmid, target_node, self._backend)

        hw_params: dict[str, Any] = {}
        if cfg.cores is not None:
            hw_params["cores"] = cfg.cores
        if cfg.memory_mb is not None:
            hw_params["memory"] = cfg.memory_mb
            hw_params["balloon"] = 0
        if hw_params:
            self._backend.put(
                f"nodes/{target_node}/qemu/{new_vmid}/config", **hw_params
            )
        if cfg.disk_gb is not None:
            vm.resize_disk("scsi0", f"{cfg.disk_gb}G")

        if cfg.cloud_init_config is not None:
            vm.configure_cloud_init(cfg.cloud_init_config)

        if cfg.start:
            vm.start()

        return vm

    # ------------------------------------------------------------------
    # launch_many
    # ------------------------------------------------------------------

    def launch_many(
        self,
        configs: list[VmConfig],
        *,
        max_workers: int | None = None,
        timeout: float = 300.0,
    ) -> list[ProxmoxVM]:
        """Launch multiple VMs in parallel. Rolls back all on any failure."""
        if not configs:
            return []

        workers = max_workers if max_workers is not None else len(configs)
        created: list[ProxmoxVM] = []
        first_error: BaseException | None = None

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for cfg in configs:
                futures[executor.submit(self.launch, cfg, timeout=timeout)] = cfg

            for fut in as_completed(futures):
                if first_error is not None:
                    continue
                exc = fut.exception()
                if exc is not None:
                    first_error = exc
                    for pending in futures:
                        pending.cancel()
                else:
                    created.append(fut.result())

        if first_error is not None:
            with ThreadPoolExecutor(max_workers=max(len(created), 1)) as rollback:
                for rf in [rollback.submit(vm.delete) for vm in created]:
                    # Rollback failures must not mask the original error, which
                    # is raised below; each future gets its own suppression so a
                    # single bad delete does not skip the remaining ones.
                    with contextlib.suppress(Exception):
                        rf.result()
            raise first_error

        return created

    # ------------------------------------------------------------------
    # ensure_running
    # ------------------------------------------------------------------

    def ensure_running(
        self,
        name: str,
        template_id: int,
        *,
        node: str | None = None,
        cores: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        cloud_init_config: CloudInitConfig | None = None,
        timeout: float = 300.0,
    ) -> ProxmoxVM:
        """Ensure the named VM exists and is running.

        Clones the VM from `template_id` when it is missing, and starts it when
        it exists but is stopped. Returns the running VM.
        """
        try:
            vm = self.get_vm(name)
            info = vm.info()
            if info.state.value == "running":
                return vm
            vm.start()
            return vm
        except VmNotFoundError:
            return self.launch(
                name,
                template_id,
                node=node,
                cores=cores,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                cloud_init_config=cloud_init_config,
                start=True,
                timeout=timeout,
            )

    # ------------------------------------------------------------------
    # list / nodes / templates
    # ------------------------------------------------------------------

    def list(self, node: str | None = None) -> builtins.list[VmInfo]:
        """Return all VMs (optionally filtered to a node)."""
        vms = self._all_vms(node=node)
        return [VmInfo.from_api(v) for v in vms]

    def list_nodes(self) -> builtins.list[NodeInfo]:
        """Return all nodes in the cluster."""
        raw = self._backend.get("nodes")
        return [NodeInfo.from_api(n) for n in raw]

    def list_templates(self, node: str | None = None) -> builtins.list[TemplateInfo]:
        """Return VMs flagged as templates."""
        vms = self._all_vms(node=node)
        return [TemplateInfo.from_api(v) for v in vms if v.get("template")]

    def find_template(self, name: str) -> TemplateInfo:
        """Find a template by name. Raises VmNotFoundError if not found."""
        for t in self.list_templates():
            if t.name == name:
                return t
        raise VmNotFoundError(name)

    # ------------------------------------------------------------------
    # purge
    # ------------------------------------------------------------------

    def purge(self, node: str | None = None) -> None:
        """Delete all VMs (use with care)."""
        for vm_info in self.list(node=node):
            pvm = ProxmoxVM(vm_info.vm_id, vm_info.node, self._backend)
            pvm.delete(purge=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _all_vms(self, node: str | None = None) -> builtins.list[dict[str, Any]]:
        resources = self._backend.get("cluster/resources", type="vm")
        if node:
            return [r for r in resources if r.get("node") == node]
        if self._node:
            return [r for r in resources if r.get("node") == self._node]
        return list(resources)

    def _node_for_vm(self, vmid: int) -> str:
        resources = self._backend.get("cluster/resources", type="vm")
        for entry in resources:
            if int(entry.get("vmid", -1)) == vmid:
                return str(entry["node"])
        raise VmNotFoundError(vmid)

    def _default_node(self) -> str:
        if self._node:
            return self._node
        nodes = self._backend.get("nodes")
        if nodes:
            return str(nodes[0]["node"])
        raise RuntimeError("No nodes available in the cluster")

    def _next_vmid(self) -> int:
        try:
            result = self._backend.get("cluster/nextid")
            return int(result)
        except Exception:
            existing = {
                int(v.get("vmid", 0))
                for v in self._backend.get("cluster/resources", type="vm")
            }
            return max(existing, default=99) + 1

    @staticmethod
    def _build_backend(
        host: str,
        user: str,
        password: str | None,
        token_name: str | None,
        token_value: str | None,
        port: int,
        verify_ssl: bool,
    ) -> ProxmoxBackend:
        try:
            from proxmoxer import ProxmoxAPI
        except ImportError as exc:
            raise ImportError(
                "proxmoxer is required for the real backend. "
                "Install it with: pip install proxmoxer requests"
            ) from exc
        from proxmox_vm_sdk._backend import ProxmoxerBackend

        if token_name and token_value:
            api = ProxmoxAPI(
                host,
                user=user,
                token_name=token_name,
                token_value=token_value,
                verify_ssl=verify_ssl,
                port=port,
            )
        else:
            api = ProxmoxAPI(
                host,
                user=user,
                password=password or "",
                verify_ssl=verify_ssl,
                port=port,
            )
        return ProxmoxerBackend(api)

    def __repr__(self) -> str:
        """Return a debug string naming the host and user, without credentials."""
        return f"ProxmoxClient(host={self._host!r}, user={self._user!r})"
