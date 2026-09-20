"""In-memory test doubles for the SDK's backends.

`FakeBackend` answers the subset of Proxmox REST routes the SDK uses, so unit
tests can drive a real `ProxmoxClient` without a cluster. `FakeSshBackend`
does the same for `SshBackend`, simulating a host filesystem and canned
command output.
"""

from __future__ import annotations

import time
from typing import Any


class FakeBackend:
    """In-memory backend for unit testing.

    Seed VMs with add_vm(), then pass to ProxmoxClient(backend=fake).
    All mutation calls are recorded in .calls for assertions.
    task wait resolves instantly.
    """

    def __init__(self) -> None:
        """Create a fake with no VMs, nodes, snapshots or recorded calls."""
        # vmid -> raw vm dict (Proxmox-shaped)
        self._vms: dict[int, dict[str, Any]] = {}
        # node -> list of raw node dicts
        self._nodes: dict[str, dict[str, Any]] = {}
        # vmid -> list of snapshot dicts
        self._snapshots: dict[int, list[dict[str, Any]]] = {}
        # upid -> exit_status
        self._tasks: dict[str, str] = {}
        self._task_counter = 0
        # recorded calls: (method, path, data)
        self._calls: list[tuple[str, str, dict[str, Any]]] = []

    # ------------------------------------------------------------------
    # Seeding helpers
    # ------------------------------------------------------------------

    def add_vm(
        self,
        vmid: int,
        node: str = "pve",
        name: str | None = None,
        status: str = "stopped",
        **kwargs: Any,
    ) -> None:
        """Seed a VM and create its node if that node is not present yet.

        The VM is stored in the Proxmox-shaped form the backend returns from
        the resource and config routes. `name` defaults to ``vm-<vmid>``, and
        any extra keyword argument is stored as a VM field (overriding the
        generated ones).
        """
        self._vms[vmid] = {
            "vmid": vmid,
            "node": node,
            "name": name or f"vm-{vmid}",
            "status": status,
            "cpus": kwargs.get("cpus", 2),
            "maxmem": kwargs.get("maxmem", 2 * 1024 * 1024 * 1024),
            "mem": kwargs.get("mem", 512 * 1024 * 1024),
            "cpu": kwargs.get("cpu", 0.01),
            "uptime": kwargs.get("uptime", 0),
            "template": kwargs.get("template", False),
            "netin": 0,
            "netout": 0,
            "diskread": 0,
            "diskwrite": 0,
            **kwargs,
        }
        if node not in self._nodes:
            self._nodes[node] = {
                "node": node,
                "status": "online",
                "maxcpu": 8,
                "maxmem": 16 * 1024 * 1024 * 1024,
                "mem": 4 * 1024 * 1024 * 1024,
                "uptime": 86400,
            }
        self._snapshots.setdefault(vmid, [])

    def add_node(self, name: str, **kwargs: Any) -> None:
        """Seed an online node with default resource stats.

        Any extra keyword argument is stored as a node field, overriding the
        generated ones.
        """
        self._nodes[name] = {
            "node": name,
            "status": "online",
            "maxcpu": 8,
            "maxmem": 16 * 1024 * 1024 * 1024,
            "mem": 4 * 1024 * 1024 * 1024,
            "uptime": 86400,
            **kwargs,
        }

    # ------------------------------------------------------------------
    # Backend protocol implementation
    # ------------------------------------------------------------------

    @property
    def calls(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Return a copy of the recorded ``(method, path, data)`` calls."""
        return list(self._calls)

    def assert_called_with(self, method: str, path: str) -> None:
        """Assert that a call to `method` on `path` was recorded.

        Raises `AssertionError` listing every recorded call when none matches.
        """
        for m, p, _ in self._calls:
            if m == method and p == path:
                return
        raise AssertionError(f"Expected {method} {path!r} but got: {self._calls}")

    def get(self, path: str, **params: Any) -> Any:
        """Record the call and return the fake's response for the GET route.

        Paths the fake does not implement raise `KeyError`.
        """
        self._calls.append(("GET", path, params))
        return self._handle_get(path, params)

    def post(self, path: str, **data: Any) -> Any:
        """Record the call, mutate the fake's state, and return the task UPID.

        Paths the fake does not implement raise `KeyError`.
        """
        self._calls.append(("POST", path, data))
        return self._handle_post(path, data)

    def put(self, path: str, **data: Any) -> Any:
        """Record the call and apply the update to the fake's state.

        Paths the fake does not implement raise `KeyError`.
        """
        self._calls.append(("PUT", path, data))
        return self._handle_put(path, data)

    def delete(self, path: str, **params: Any) -> Any:
        """Record the call and remove the addressed object from the fake.

        Paths the fake does not implement raise `KeyError`.
        """
        self._calls.append(("DELETE", path, params))
        return self._handle_delete(path, params)

    def wait_for_task(self, node: str, upid: str, timeout: float = 60) -> None:
        """Return immediately: fake tasks are always already finished."""
        # Tasks resolve instantly in the fake
        pass

    # ------------------------------------------------------------------
    # Internal routing
    # ------------------------------------------------------------------

    def _handle_get(self, path: str, params: dict[str, Any]) -> Any:
        parts = path.strip("/").split("/")

        # cluster/resources?type=vm
        if parts == ["cluster", "resources"]:
            vm_type = params.get("type")
            if vm_type == "vm":
                return list(self._vms.values())
            if vm_type == "node":
                return list(self._nodes.values())
            return list(self._vms.values()) + list(self._nodes.values())

        # nodes
        if parts == ["nodes"]:
            return list(self._nodes.values())

        # nodes/{node}/qemu
        if len(parts) == 3 and parts[0] == "nodes" and parts[2] == "qemu":
            node = parts[1]
            return [v for v in self._vms.values() if v.get("node") == node]

        # nodes/{node}/qemu/{vmid}/status/current
        if (
            len(parts) == 6
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "status"
            and parts[5] == "current"
        ):
            vmid = int(parts[3])
            vm = self._vms.get(vmid)
            if vm is None:
                raise KeyError(f"VM {vmid} not found")
            return vm

        # nodes/{node}/qemu/{vmid}/snapshots
        if (
            len(parts) == 5
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "snapshots"
        ):
            vmid = int(parts[3])
            return list(self._snapshots.get(vmid, []))

        # nodes/{node}/qemu/{vmid}/agent/network-get-interfaces
        if (
            len(parts) == 6
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "agent"
            and parts[5] == "network-get-interfaces"
        ):
            vmid = int(parts[3])
            vm = self._vms.get(vmid)
            if vm is None:
                raise KeyError(f"VM {vmid} not found")
            # Return empty result by default (no IP assigned)
            return {"result": []}

        # nodes/{node}/qemu/{vmid}/config
        if (
            len(parts) == 5
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "config"
        ):
            vmid = int(parts[3])
            vm = self._vms.get(vmid)
            if vm is None:
                raise KeyError(f"VM {vmid} not found")
            return vm

        raise KeyError(f"FakeBackend: unhandled GET path: {path!r}")

    def _handle_post(self, path: str, data: dict[str, Any]) -> Any:
        parts = path.strip("/").split("/")

        # nodes/{node}/qemu — create VM
        if len(parts) == 3 and parts[0] == "nodes" and parts[2] == "qemu":
            vmid = int(data.get("vmid", 9000 + len(self._vms)))
            node = parts[1]
            self.add_vm(vmid, node=node, name=data.get("name", f"vm-{vmid}"))
            return self._make_upid(node)

        # nodes/{node}/qemu/{vmid}/status/start
        if (
            len(parts) == 6
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "status"
            and parts[5] == "start"
        ):
            vmid = int(parts[3])
            self._require_vm(vmid)
            self._vms[vmid]["status"] = "running"
            self._vms[vmid]["uptime"] = 1
            return self._make_upid(parts[1])

        # nodes/{node}/qemu/{vmid}/status/stop
        if (
            len(parts) == 6
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "status"
            and parts[5] in ("stop", "shutdown")
        ):
            vmid = int(parts[3])
            self._require_vm(vmid)
            self._vms[vmid]["status"] = "stopped"
            self._vms[vmid]["uptime"] = 0
            return self._make_upid(parts[1])

        # nodes/{node}/qemu/{vmid}/status/reboot
        if (
            len(parts) == 6
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "status"
            and parts[5] == "reboot"
        ):
            vmid = int(parts[3])
            self._require_vm(vmid)
            self._vms[vmid]["status"] = "running"
            return self._make_upid(parts[1])

        # nodes/{node}/qemu/{vmid}/clone
        if (
            len(parts) == 5
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "clone"
        ):
            src_vmid = int(parts[3])
            self._require_vm(src_vmid)
            new_vmid = int(data["newid"])
            node = data.get("target", parts[1])
            name = data.get("name", f"vm-{new_vmid}")
            self.add_vm(new_vmid, node=node, name=name, status="stopped")
            return self._make_upid(parts[1])

        # nodes/{node}/qemu/{vmid}/snapshots
        if (
            len(parts) == 5
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "snapshots"
        ):
            vmid = int(parts[3])
            self._require_vm(vmid)
            snap = {
                "name": data.get("snapname", "snap"),
                "description": data.get("description", ""),
                "snaptime": int(time.time()),
                "parent": None,
            }
            existing = [s["name"] for s in self._snapshots.get(vmid, [])]
            if existing:
                snap["parent"] = existing[-1]
            self._snapshots.setdefault(vmid, []).append(snap)
            return self._make_upid(parts[1])

        # nodes/{node}/qemu/{vmid}/snapshots/{snap}/rollback
        if (
            len(parts) == 7
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "snapshots"
            and parts[6] == "rollback"
        ):
            vmid = int(parts[3])
            self._require_vm(vmid)
            return self._make_upid(parts[1])

        raise KeyError(f"FakeBackend: unhandled POST path: {path!r}")

    def _handle_put(self, path: str, data: dict[str, Any]) -> Any:
        parts = path.strip("/").split("/")

        # nodes/{node}/qemu/{vmid}/resize
        if (
            len(parts) == 5
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "resize"
        ):
            vmid = int(parts[3])
            self._require_vm(vmid)
            return None

        # nodes/{node}/qemu/{vmid}/config  — store cloud-init fields
        if (
            len(parts) == 5
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "config"
        ):
            vmid = int(parts[3])
            self._require_vm(vmid)
            self._vms[vmid].update(data)
            return None

        raise KeyError(f"FakeBackend: unhandled PUT path: {path!r}")

    def _handle_delete(self, path: str, params: dict[str, Any]) -> Any:
        parts = path.strip("/").split("/")

        # nodes/{node}/qemu/{vmid}
        if len(parts) == 4 and parts[0] == "nodes" and parts[2] == "qemu":
            vmid = int(parts[3])
            self._require_vm(vmid)
            del self._vms[vmid]
            self._snapshots.pop(vmid, None)
            return self._make_upid(parts[1])

        # nodes/{node}/qemu/{vmid}/snapshots/{snap}
        if (
            len(parts) == 6
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "snapshots"
        ):
            vmid = int(parts[3])
            snap_name = parts[5]
            snaps = self._snapshots.get(vmid, [])
            self._snapshots[vmid] = [s for s in snaps if s["name"] != snap_name]
            return self._make_upid(parts[1])

        raise KeyError(f"FakeBackend: unhandled DELETE path: {path!r}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_vm(self, vmid: int) -> None:
        if vmid not in self._vms:
            from proxmox_vm_sdk.exceptions import VmNotFoundError

            raise VmNotFoundError(vmid)

    def _make_upid(self, node: str) -> str:
        self._task_counter += 1
        upid = f"UPID:{node}:fake-{self._task_counter:04d}"
        self._tasks[upid] = "OK"
        return upid


class FakeSshBackend:
    """In-memory SSH backend for unit testing.

    Simulates a Proxmox host's filesystem and command output without
    any real network connection.
    """

    def __init__(self) -> None:
        """Create a fake with an empty filesystem and no canned responses."""
        # filename -> content
        self._files: dict[str, str] = {}
        # command prefix -> (exit_code, stdout, stderr)
        self._responses: dict[str, tuple[int, str, str]] = {}
        self.commands: list[str] = []

    def seed_file(self, path: str, content: str) -> None:
        """Pre-populate a file on the fake filesystem."""
        self._files[path] = content

    def seed_response(
        self, command_prefix: str, exit_code: int, stdout: str, stderr: str = ""
    ) -> None:
        """Register a canned response for commands starting with command_prefix."""
        self._responses[command_prefix] = (exit_code, stdout, stderr)

    def run(self, command: str) -> tuple[int, str, str]:
        """Record the command and return its canned ``(code, stdout, stderr)``.

        The first seeded prefix the command starts with wins; commands with no
        matching prefix succeed with no output.
        """
        self.commands.append(command)
        for prefix, response in self._responses.items():
            if command.startswith(prefix):
                return response
        # Default: success, no output
        return 0, "", ""

    def read_file(self, path: str) -> str:
        """Return the seeded contents of `path`, or an empty string if unset."""
        return self._files.get(path, "")

    def write_file(self, path: str, content: str) -> None:
        """Store `content` as the contents of `path` on the fake filesystem."""
        self._files[path] = content

    def assert_ran(self, substring: str) -> None:
        """Assert that a command containing substring was executed."""
        for cmd in self.commands:
            if substring in cmd:
                return
        raise AssertionError(
            f"Expected a command containing {substring!r}. Ran: {self.commands}"
        )
