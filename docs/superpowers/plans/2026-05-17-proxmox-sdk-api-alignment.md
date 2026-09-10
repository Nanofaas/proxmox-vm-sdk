# proxmox-sdk → azure-vm-sdk Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Full restructure of proxmox-sdk package structure, API, and tooling to mirror azure-vm-sdk patterns: consolidated `_backend.py`, `VmConfig` model, `launch`/`launch_many`/`ensure_running` API, `e2e.py` CLI, `devtools/`, `testing.py`, and unit/integration test split.

**Architecture:** Merge the three `backends/` modules into `_backend.py` (protocols + real implementations) and `testing.py` (fakes). Rename client methods to match Azure conventions. Add `VmConfig` dataclass as reusable launch configuration. Add `e2e.py` CLI for end-to-end VM lifecycle verification. Add `devtools/` with quality, package_report, and code_eval CLI tools.

**Tech Stack:** Python 3.11+, uv, pytest, ruff, mypy, hatchling, paramiko, proxmoxer, requests, grimp (new), import-linter (new)

**Breaking changes:** Yes — full API rename, module restructure, import path changes.

---

### File Structure (Final)

```
src/proxmox_sdk/
    __init__.py              # re-exports: ProxmoxClient, ProxmoxVM, ProxmoxRoutingManager, models, exceptions, FakeBackend, FakeSshBackend
    _backend.py              # ProxmoxBackend (protocol), ProxmoxerBackend, CommandResult, SshBackend (protocol), ParamikoSshBackend
    _utils.py                # parse_proxmox_url
    client.py                # ProxmoxClient: launch(), launch_many(), ensure_running(), get_vm(), list(), list_nodes(), list_templates(), find_template(), purge()
    vm.py                    # ProxmoxVM: info(), metrics(), start(), stop(), shutdown(), restart(), delete(), clone(), snapshot(), restore(), list_snapshots(), exec(), exec_structured(), transfer(), wait_for_ip(), wait_for_agent(), wait_ready(), resize_disk(), configure_cloud_init()
    models.py                # VmConfig, VmInfo, VmMetrics, VmState, CloudInitConfig, TemplateInfo, NodeInfo, SnapshotInfo, CommandResult, PortMapping
    exceptions.py            # ProxmoxError, ProxmoxAuthError, ProxmoxConnectionError, ProxmoxAPIError, VmNotFoundError, VmStateError, NodeNotFoundError, ProxmoxTimeoutError, SnapshotNotFoundError, TaskFailedError
    routing.py               # ProxmoxRoutingManager, PortMapping
    testing.py               # FakeBackend, FakeSshBackend
    py.typed
    e2e.py                   # CLI: proxmox-e2e — VM lifecycle end-to-end test
    devtools/
        __init__.py
        quality.py           # CLI: proxmox-quality — ruff + mypy
        package_report.py    # CLI: proxmox-package-report — grimp import analysis
        code_eval.py         # CLI: proxmox-eval — AST-based code smells + grimp coupling
tests/
    conftest.py
    unit/
        __init__.py
        test_client.py
        test_vm.py
        test_models.py
        test_cloud_init.py
        test_backend.py
        test_routing.py
    integration/
        __init__.py
        test_integration.py  # requires PROXMOX_HOST, PROXMOX_USER, PROXMOX_PASSWORD env vars
```

---

### Task 1: Create `_backend.py` — consolidate all backends

**Files:**
- Create: `src/proxmox_sdk/_backend.py`
- Delete: `src/proxmox_sdk/backends/protocol.py`
- Delete: `src/proxmox_sdk/backends/proxmoxer.py`
- Delete: `src/proxmox_sdk/backends/ssh.py`
- Delete: `src/proxmox_sdk/backends/fake.py`
- Delete: `src/proxmox_sdk/backends/__init__.py`
- Modify: `src/proxmox_sdk/__init__.py`

- [ ] **Step 1: Write `_backend.py` with all protocols and real implementations**

```python
from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from proxmox_sdk.exceptions import (
    ProxmoxAPIError,
    ProxmoxTimeoutError,
    TaskFailedError,
    VmNotFoundError,
)


# -- REST API backend protocol --------------------------------------------


@runtime_checkable
class ProxmoxBackend(Protocol):
    def get(self, path: str, **params: Any) -> Any: ...
    def post(self, path: str, **data: Any) -> Any: ...
    def put(self, path: str, **data: Any) -> Any: ...
    def delete(self, path: str, **params: Any) -> Any: ...
    def wait_for_task(self, node: str, upid: str, timeout: float = 60) -> None: ...


# -- Proxmoxer (real REST) backend ---------------------------------------


class ProxmoxerBackend:
    def __init__(self, proxmox_api: Any) -> None:
        self._api = proxmox_api

    def get(self, path: str, **params: Any) -> Any:
        return self._call("get", path, **params)

    def post(self, path: str, **data: Any) -> Any:
        return self._call("post", path, **data)

    def put(self, path: str, **data: Any) -> Any:
        return self._call("put", path, **data)

    def delete(self, path: str, **params: Any) -> Any:
        return self._call("delete", path, **params)

    def wait_for_task(self, node: str, upid: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = self.get(f"nodes/{node}/tasks/{upid}/status")
                if result.get("status") == "stopped":
                    exit_status = result.get("exitstatus", "")
                    if exit_status != "OK":
                        raise TaskFailedError(upid, exit_status)
                    return
            except (KeyError, AttributeError):
                pass
            time.sleep(1)
        raise ProxmoxTimeoutError(0, "wait_for_task", timeout)

    def _resolve(self, path: str) -> Any:
        resource = self._api
        for segment in path.strip("/").split("/"):
            if segment.lstrip("-").isdigit():
                resource = resource(segment)
            else:
                resource = getattr(resource, segment)
        return resource

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resource = self._resolve(path)
            return getattr(resource, method)(**kwargs)
        except Exception as exc:
            self._translate_exception(exc, path)
            raise

    @staticmethod
    def _translate_exception(exc: Exception, path: str) -> None:
        exc_type = type(exc).__name__
        if exc_type == "ResourceException":
            status_code = getattr(exc, "status_code", 0)
            content = str(getattr(exc, "content", str(exc)))
            if status_code == 404 or "does not exist" in content.lower():
                raise VmNotFoundError(path) from exc
            raise ProxmoxAPIError(status_code, content, path) from exc


# -- SSH backend protocol and implementations -----------------------------


@runtime_checkable
class SshBackend(Protocol):
    def run(self, command: str) -> tuple[int, str, str]: ...
    def read_file(self, path: str) -> str: ...
    def write_file(self, path: str, content: str) -> None: ...


class ParamikoSshBackend:
    def __init__(
        self,
        host: str,
        user: str,
        ssh_key_path: str | None = None,
        password: str | None = None,
        port: int = 22,
    ) -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise ImportError(
                "paramiko is required for SSH operations. Install it with: pip install paramiko"
            ) from exc

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict[str, object] = {
            "hostname": host,
            "username": user,
            "port": port,
        }
        if ssh_key_path:
            connect_kwargs["key_filename"] = ssh_key_path
        if password:
            connect_kwargs["password"] = password
        self._client.connect(**connect_kwargs)

    def run(self, command: str) -> tuple[int, str, str]:
        _, stdout, stderr = self._client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout.read().decode(), stderr.read().decode()

    def read_file(self, path: str) -> str:
        _, stdout, _ = self._client.exec_command(f"cat {path}")
        stdout.channel.recv_exit_status()
        return str(stdout.read().decode())

    def write_file(self, path: str, content: str) -> None:
        tmp = f"{path}.proxmox_sdk_tmp"
        sftp = self._client.open_sftp()
        with sftp.file(tmp, "w") as f:
            f.write(content)
        sftp.close()
        self.run(f"mv {tmp} {path}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ParamikoSshBackend":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# -- Command result -------------------------------------------------------


class CommandResult:
    def __init__(
        self,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        args: list[str] | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.args = args or []

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def __repr__(self) -> str:
        return f"CommandResult(exit_code={self.exit_code}, stdout={self.stdout!r}, stderr={self.stderr!r})"
```

- [ ] **Step 2: Create `testing.py` with FakeBackend and FakeSshBackend**

```python
from __future__ import annotations

import time
from typing import Any

from proxmox_sdk.exceptions import VmNotFoundError


class FakeBackend:
    def __init__(self) -> None:
        self._vms: dict[int, dict[str, Any]] = {}
        self._nodes: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[int, list[dict[str, Any]]] = {}
        self._tasks: dict[str, str] = {}
        self._task_counter = 0
        self._calls: list[tuple[str, str, dict[str, Any]]] = []

    def add_vm(
        self,
        vmid: int,
        node: str = "pve",
        name: str | None = None,
        status: str = "stopped",
        **kwargs: Any,
    ) -> None:
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
        self._nodes[name] = {
            "node": name,
            "status": "online",
            "maxcpu": 8,
            "maxmem": 16 * 1024 * 1024 * 1024,
            "mem": 4 * 1024 * 1024 * 1024,
            "uptime": 86400,
            **kwargs,
        }

    @property
    def calls(self) -> list[tuple[str, str, dict[str, Any]]]:
        return list(self._calls)

    def assert_called_with(self, method: str, path: str) -> None:
        for m, p, _ in self._calls:
            if m == method and p == path:
                return
        raise AssertionError(f"Expected {method} {path!r} but got: {self._calls}")

    def get(self, path: str, **params: Any) -> Any:
        self._calls.append(("GET", path, params))
        return self._handle_get(path, params)

    def post(self, path: str, **data: Any) -> Any:
        self._calls.append(("POST", path, data))
        return self._handle_post(path, data)

    def put(self, path: str, **data: Any) -> Any:
        self._calls.append(("PUT", path, data))
        return self._handle_put(path, data)

    def delete(self, path: str, **params: Any) -> Any:
        self._calls.append(("DELETE", path, params))
        return self._handle_delete(path, params)

    def wait_for_task(self, node: str, upid: str, timeout: float = 60) -> None:
        pass

    def _handle_get(self, path: str, params: dict[str, Any]) -> Any:
        parts = path.strip("/").split("/")

        if parts == ["cluster", "resources"]:
            vm_type = params.get("type")
            if vm_type == "vm":
                return list(self._vms.values())
            if vm_type == "node":
                return list(self._nodes.values())
            return list(self._vms.values()) + list(self._nodes.values())

        if parts == ["nodes"]:
            return list(self._nodes.values())

        if len(parts) == 3 and parts[0] == "nodes" and parts[2] == "qemu":
            node = parts[1]
            return [v for v in self._vms.values() if v.get("node") == node]

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

        if (
            len(parts) == 5
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "snapshots"
        ):
            vmid = int(parts[3])
            return list(self._snapshots.get(vmid, []))

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
            return {"result": []}

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

        # cluster/nextid
        if parts == ["cluster", "nextid"]:
            existing = {int(v["vmid"]) for v in self._vms.values()}
            return max(existing, default=99) + 1

        raise KeyError(f"FakeBackend: unhandled GET path: {path!r}")

    def _handle_post(self, path: str, data: dict[str, Any]) -> Any:
        parts = path.strip("/").split("/")

        if len(parts) == 3 and parts[0] == "nodes" and parts[2] == "qemu":
            vmid = int(data.get("vmid", 9000 + len(self._vms)))
            node = parts[1]
            self.add_vm(vmid, node=node, name=data.get("name", f"vm-{vmid}"))
            return self._make_upid(node)

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

        # agent/ping — used by wait_for_agent
        if (
            len(parts) == 5
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "agent"
        ):
            return {}

        raise KeyError(f"FakeBackend: unhandled POST path: {path!r}")

    def _handle_put(self, path: str, data: dict[str, Any]) -> Any:
        parts = path.strip("/").split("/")

        if (
            len(parts) == 5
            and parts[0] == "nodes"
            and parts[2] == "qemu"
            and parts[4] == "resize"
        ):
            vmid = int(parts[3])
            self._require_vm(vmid)
            return None

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

        if len(parts) == 4 and parts[0] == "nodes" and parts[2] == "qemu":
            vmid = int(parts[3])
            self._require_vm(vmid)
            del self._vms[vmid]
            self._snapshots.pop(vmid, None)
            return self._make_upid(parts[1])

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

    def _require_vm(self, vmid: int) -> None:
        if vmid not in self._vms:
            raise VmNotFoundError(vmid)

    def _make_upid(self, node: str) -> str:
        self._task_counter += 1
        upid = f"UPID:{node}:fake-{self._task_counter:04d}"
        self._tasks[upid] = "OK"
        return upid


class FakeSshBackend:
    def __init__(self) -> None:
        self._files: dict[str, str] = {}
        self._responses: dict[str, tuple[int, str, str]] = {}
        self.commands: list[str] = []

    def seed_file(self, path: str, content: str) -> None:
        self._files[path] = content

    def seed_response(
        self, command_prefix: str, exit_code: int, stdout: str, stderr: str = ""
    ) -> None:
        self._responses[command_prefix] = (exit_code, stdout, stderr)

    def run(self, command: str) -> tuple[int, str, str]:
        self.commands.append(command)
        for prefix, response in self._responses.items():
            if command.startswith(prefix):
                return response
        return 0, "", ""

    def read_file(self, path: str) -> str:
        return self._files.get(path, "")

    def write_file(self, path: str, content: str) -> None:
        self._files[path] = content

    def assert_ran(self, substring: str) -> None:
        for cmd in self.commands:
            if substring in cmd:
                return
        raise AssertionError(
            f"Expected a command containing {substring!r}. Ran: {self.commands}"
        )
```

- [ ] **Step 3: Update `__init__.py` to import from new locations**

```python
from proxmox_sdk._backend import (
    ProxmoxBackend,
    ProxmoxerBackend,
    SshBackend,
    ParamikoSshBackend,
    CommandResult,
)
from proxmox_sdk.client import ProxmoxClient
from proxmox_sdk.exceptions import (
    NodeNotFoundError,
    ProxmoxAPIError,
    ProxmoxAuthError,
    ProxmoxConnectionError,
    ProxmoxError,
    ProxmoxTimeoutError,
    SnapshotNotFoundError,
    TaskFailedError,
    VmNotFoundError,
    VmStateError,
)
from proxmox_sdk.models import (
    CloudInitConfig,
    NodeInfo,
    SnapshotInfo,
    TemplateInfo,
    VmConfig,
    VmInfo,
    VmMetrics,
    VmState,
)
from proxmox_sdk.routing import PortMapping, ProxmoxRoutingManager
from proxmox_sdk.testing import FakeBackend, FakeSshBackend
from proxmox_sdk.vm import ProxmoxVM

__all__ = [
    "ProxmoxClient",
    "ProxmoxVM",
    "VmConfig",
    "CloudInitConfig",
    "CommandResult",
    "NodeInfo",
    "SnapshotInfo",
    "TemplateInfo",
    "VmInfo",
    "VmMetrics",
    "VmState",
    "ProxmoxError",
    "ProxmoxAuthError",
    "ProxmoxConnectionError",
    "ProxmoxAPIError",
    "VmNotFoundError",
    "VmStateError",
    "NodeNotFoundError",
    "ProxmoxTimeoutError",
    "SnapshotNotFoundError",
    "TaskFailedError",
    "FakeBackend",
    "FakeSshBackend",
    "ProxmoxBackend",
    "ProxmoxerBackend",
    "SshBackend",
    "ParamikoSshBackend",
    "ProxmoxRoutingManager",
    "PortMapping",
]
```

- [ ] **Step 4: Delete old `backends/` directory**

```bash
rm -r src/proxmox_sdk/backends/
```

- [ ] **Step 5: Run tests to verify nothing broken yet**

```bash
uv run pytest tests/ -v
```

Expected: import errors due to old paths — we'll fix in later tasks.

- [ ] **Step 6: Commit**

```bash
git add src/proxmox_sdk/_backend.py src/proxmox_sdk/testing.py src/proxmox_sdk/__init__.py
git add src/proxmox_sdk/backends/  # deletion
git commit -m "refactor: consolidate backends into _backend.py and testing.py"
```

---

### Task 2: Add `VmConfig` to models.py

**Files:**
- Modify: `src/proxmox_sdk/models.py`

- [ ] **Step 1: Add VmConfig dataclass**

```python
@dataclass
class VmConfig:
    """Reusable VM launch configuration — mirrors azure-vm-sdk VmConfig."""

    name: str | None = None
    template_id: int | None = None
    node: str | None = None
    cores: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    cloud_init_config: CloudInitConfig | None = None
    start: bool = True
```

- [ ] **Step 2: Commit**

```bash
git add src/proxmox_sdk/models.py
git commit -m "feat: add VmConfig dataclass for reusable launch configuration"
```

---

### Task 3: Update ProxmoxClient — new API

**Files:**
- Modify: `src/proxmox_sdk/client.py`

- [ ] **Step 1: Rewrite ProxmoxClient with launch/launch_many/ensure_running**

Key changes:
- `create_vm()` → `launch()` (accepts both `VmConfig` and keyword args)
- Add `launch_many()` with ThreadPoolExecutor + rollback
- Add `ensure_running()` — idempotent create-or-start
- `get_vm()` accepts both int ID and str name
- Remove `find_vm()` (absorbed by `get_vm`)
- `purge_stopped()` → `purge()` — deletes ALL VMs, not just stopped

```python
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, List

from proxmox_sdk._utils import parse_proxmox_url
from proxmox_sdk._backend import ProxmoxBackend
from proxmox_sdk.exceptions import VmNotFoundError
from proxmox_sdk.models import CloudInitConfig, NodeInfo, TemplateInfo, VmConfig, VmInfo
from proxmox_sdk.vm import ProxmoxVM


class ProxmoxClient:
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
    ) -> "ProxmoxClient":
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

    # ------------------------------------------------------------ get_vm

    def get_vm(self, identifier: int | str) -> ProxmoxVM:
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

    # -------------------------------------------------------- launch

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
    ) -> ProxmoxVM:
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
        template_id = cfg.template_id or 0
        target_node = cfg.node or node or self._default_node()
        new_vmid = self._next_vmid()

        upid = self._backend.post(
            f"nodes/{target_node}/qemu/{template_id}/clone",
            newid=new_vmid,
            name=name,
            target=target_node,
            full=1,
        )
        self._backend.wait_for_task(target_node, upid)

        vm = ProxmoxVM(new_vmid, target_node, self._backend)

        hw_params: dict[str, Any] = {}
        if cfg.cores is not None:
            hw_params["cores"] = cfg.cores
        if cfg.memory_mb is not None:
            hw_params["memory"] = cfg.memory_mb
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

    # ----------------------------------------------------- launch_many

    def launch_many(
        self,
        configs: list[VmConfig],
        *,
        max_workers: int | None = None,
    ) -> list[ProxmoxVM]:
        if not configs:
            return []

        workers = max_workers if max_workers is not None else len(configs)
        created: list[ProxmoxVM] = []
        first_error: BaseException | None = None

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for cfg in configs:
                futures[executor.submit(self.launch, cfg)] = cfg

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
                    try:
                        rf.result()
                    except Exception:
                        pass
            raise first_error

        return created

    # --------------------------------------------------- ensure_running

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
    ) -> ProxmoxVM:
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
            )

    # ------------------------------------------------------ list / nodes / templates

    def list(self, node: str | None = None) -> List[VmInfo]:
        vms = self._all_vms(node=node)
        return [VmInfo.from_api(v) for v in vms]

    def list_nodes(self) -> List[NodeInfo]:
        raw = self._backend.get("nodes")
        return [NodeInfo.from_api(n) for n in raw]

    def list_templates(self, node: str | None = None) -> List[TemplateInfo]:
        vms = self._all_vms(node=node)
        return [TemplateInfo.from_api(v) for v in vms if v.get("template")]

    def find_template(self, name: str) -> TemplateInfo:
        for t in self.list_templates():
            if t.name == name:
                return t
        raise VmNotFoundError(name)

    # ----------------------------------------------------------- purge

    def purge(self, node: str | None = None) -> None:
        for vm_info in self.list(node=node):
            pvm = ProxmoxVM(vm_info.vm_id, vm_info.node, self._backend)
            pvm.delete(purge=True)

    # ---------------------------------------------------- internal helpers

    def _all_vms(self, node: str | None = None) -> List[dict[str, Any]]:
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
        from proxmox_sdk._backend import ProxmoxerBackend

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
        return f"ProxmoxClient(host={self._host!r}, user={self._user!r})"
```

- [ ] **Step 2: Commit**

```bash
git add src/proxmox_sdk/client.py
git commit -m "feat: replace create_vm with launch/launch_many/ensure_running API"
```

---

### Task 4: Update ProxmoxVM — add `exec_structured`, `transfer`

**Files:**
- Modify: `src/proxmox_sdk/vm.py`

- [ ] **Step 1: Add `exec_structured` and `transfer` methods**

Add to ProxmoxVM class, after `exec()`:

```python
def exec_structured(
    self,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> CommandResult:
    """Run a command with env vars and working directory via guest agent."""
    from shlex import quote
    from proxmox_sdk._backend import CommandResult

    parts: list[str] = []
    if cwd:
        parts.append(f"cd {quote(cwd)}")
    for k, v in (env or {}).items():
        parts.append(f"export {k}={quote(v)}")
    parts.append(" ".join(quote(a) for a in argv))
    command = " && ".join(parts)
    return self.exec(["bash", "-lc", command])


def transfer(self, source: str, dest: str) -> None:
    """Transfer a file to/from the VM via SSH (not guest agent).

    Requires ParamikoSshBackend or similar SSH connection to the VM.
    The VM must be reachable via SSH.
    """
    raise NotImplementedError(
        "transfer() requires an SSH connection to the VM. "
        "Use ProxmoxRoutingManager for NAT port forwarding first."
    )
```

- [ ] **Step 2: Commit**

```bash
git add src/proxmox_sdk/vm.py
git commit -m "feat: add exec_structured and transfer methods to ProxmoxVM"
```

---

### Task 5: Create `e2e.py` — end-to-end CLI test program

**Files:**
- Create: `src/proxmox_sdk/e2e.py`

- [ ] **Step 1: Write e2e.py**

Mirrors `azure-vm-sdk/src/azure_vm/e2e.py`, adapted for Proxmox.

```python
"""End-to-end verification: create VM(s), verify readiness, delete VM(s).

Prerequisites:
    PROXMOX_HOST, PROXMOX_USER, PROXMOX_PASSWORD env vars set
    PROXMOX_NODE env var (or --node)

Usage:
    uv run proxmox-e2e --node pve --template-id 9000
    uv run proxmox-e2e --name test-vm --template-id 9000 --cores 2 --memory-mb 2048
    uv run proxmox-e2e --count 3 --template-id 9000
    uv run proxmox-e2e --configs '[{"name":"web","template_id":9000,"cores":2}]'
    uv run proxmox-e2e --list-templates
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .client import ProxmoxClient
from .exceptions import ProxmoxError
from .models import VmConfig
from .vm import ProxmoxVM


def _env_or_raise(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def _verify_vm(vm: ProxmoxVM, idx: int, total: int, timeout: float) -> int:
    label = f"  [{idx}/{total}] {vm.vm_id}"
    exit_code = 0

    print(f"{label}: waiting for guest agent ...")
    t0 = time.monotonic()
    try:
        vm.wait_for_agent(timeout=timeout)
    except ProxmoxError as exc:
        print(f"{label}: agent timeout — {exc}", file=sys.stderr)
        return 1
    print(f"{label}: agent ready in {time.monotonic() - t0:.1f}s")

    print(f"{label}: waiting for IP ...")
    t0 = time.monotonic()
    try:
        ip = vm.wait_for_ip(timeout=timeout)
    except ProxmoxError as exc:
        print(f"{label}: IP timeout — {exc}", file=sys.stderr)
        return 1
    print(f"{label}: got IP {ip} in {time.monotonic() - t0:.1f}s")

    print(f"{label}: running verification command ...")
    result = vm.exec(["hostname"])
    print(f"{label}: exit={result.exit_code}  stdout={result.stdout.strip()}")
    if result.stderr:
        print(f"{label}: stderr={result.stderr.strip()}")
    if not result.success:
        print(f"{label}: FAILED", file=sys.stderr)
        exit_code = 1

    info = vm.info()
    print(
        f"{label}: state={info.state.value}  node={info.node}  cores={info.cpu_count}  mem={info.memory_mb}MB"
    )
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(
        description="End-to-end Proxmox VM lifecycle test (create, verify, delete)."
    )
    parser.add_argument("--name", default=None, help="VM name prefix.")
    parser.add_argument(
        "--template-id", type=int, default=None, help="Template VMID to clone."
    )
    parser.add_argument(
        "--node", default=None, help="Proxmox node (default: PROXMOX_NODE env)."
    )
    parser.add_argument("--cores", type=int, default=None)
    parser.add_argument("--memory-mb", type=int, default=None)
    parser.add_argument("--disk-gb", type=int, default=None)
    parser.add_argument(
        "--timeout", type=float, default=300, help="Max wait seconds (default: 300)."
    )
    parser.add_argument(
        "--count", type=int, default=1, help="Number of VMs (default: 1)."
    )
    parser.add_argument(
        "--configs",
        default=None,
        help="JSON array of VmConfig objects. Mutually exclusive with --count.",
    )
    parser.add_argument(
        "--list-templates", action="store_true", help="List templates and exit."
    )
    args = parser.parse_args()

    if args.configs and args.count != 1:
        raise SystemExit("--configs and --count are mutually exclusive")
    if args.count < 1:
        raise SystemExit("--count must be at least 1")

    host = _env_or_raise("PROXMOX_HOST")
    user = _env_or_raise("PROXMOX_USER")
    password = os.environ.get("PROXMOX_PASSWORD")
    token_name = os.environ.get("PROXMOX_TOKEN_NAME")
    token_value = os.environ.get("PROXMOX_TOKEN_VALUE")
    node = args.node or os.environ.get("PROXMOX_NODE")

    client = ProxmoxClient(
        host=host,
        user=user,
        password=password,
        token_name=token_name,
        token_value=token_value,
        node=node,
        verify_ssl=False,
    )

    if args.list_templates:
        templates = client.list_templates()
        print(f"{'VMID':>6}  {'Name':<30}  {'Node':<10}  {'Cores':>6}  {'Memory':>10}")
        print("-" * 75)
        for t in templates:
            print(
                f"{t.vm_id:>6}  {t.name:<30}  {t.node:<10}  {t.cores:>6}  {t.memory_mb:>8}MB"
            )
        raise SystemExit(0)

    if args.configs:
        try:
            raw = json.loads(args.configs)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--configs: invalid JSON — {exc}") from exc
        if not isinstance(raw, list) or not raw:
            raise SystemExit("--configs: expected a non-empty JSON array")
        configs = [VmConfig(**item) for item in raw]
    else:
        if not args.template_id:
            raise SystemExit("--template-id is required (or use --configs)")
        prefix = args.name or f"e2e-{int(time.time())}"
        if args.count == 1:
            configs = [
                VmConfig(
                    name=prefix,
                    template_id=args.template_id,
                    node=node,
                    cores=args.cores,
                    memory_mb=args.memory_mb,
                    disk_gb=args.disk_gb,
                )
            ]
        else:
            configs = [
                VmConfig(
                    name=f"{prefix}-{i}",
                    template_id=args.template_id,
                    node=node,
                    cores=args.cores,
                    memory_mb=args.memory_mb,
                    disk_gb=args.disk_gb,
                )
                for i in range(args.count)
            ]

    n = len(configs)
    vms: list[ProxmoxVM] = []
    exit_code = 0

    try:
        if n == 1:
            print(f"[1/3] Launching VM '{configs[0].name}' ...")
        else:
            print(f"[1/3] Launching {n} VMs in parallel ...")
            for cfg in configs:
                print(f"       - {cfg.name}")
        t0 = time.monotonic()
        vms = client.launch_many(configs)
        dt = time.monotonic() - t0
        print(
            f"       {'launch' if n == 1 else f'all {n} launches'} completed in {dt:.1f}s"
        )

        print(f"[2/3] Verifying {n} VM(s) ...")
        for i, vm in enumerate(vms, start=1):
            rc = _verify_vm(vm, i, n, args.timeout)
            if rc != 0:
                exit_code = 1

    except ProxmoxError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        exit_code = 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        exit_code = 130

    if vms:
        if n == 1:
            print(f"[3/3] Deleting VM '{vms[0].vm_id}' ...")
        else:
            print(f"[3/3] Deleting {len(vms)} VM(s) ...")
        try:
            for vm in vms:
                vm.delete(purge=True)
            print("       done.")
        except ProxmoxError as exc:
            print(f"       cleanup failed: {exc}", file=sys.stderr)
            exit_code = 3

    print()
    if exit_code == 0:
        print(f"SUCCESS — {n} VM(s) completed full lifecycle.")
    else:
        print(f"FAILURE (exit code {exit_code})")

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add src/proxmox_sdk/e2e.py
git commit -m "feat: add e2e.py CLI for end-to-end VM lifecycle verification"
```

---

### Task 6: Create `devtools/` — quality, package_report, code_eval

**Files:**
- Create: `src/proxmox_sdk/devtools/__init__.py`
- Create: `src/proxmox_sdk/devtools/quality.py`
- Create: `src/proxmox_sdk/devtools/package_report.py`
- Create: `src/proxmox_sdk/devtools/code_eval.py`

- [ ] **Step 1: Write `devtools/quality.py`** — mirrors azure-vm-sdk version

```python
from __future__ import annotations

import subprocess
import sys

CHECKS = (
    ("ruff", ["uv", "run", "ruff", "check", "src/", "tests/"]),
    ("mypy", ["uv", "run", "mypy", "src/"]),
)


def main() -> None:
    failures: list[str] = []
    for name, command in CHECKS:
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            failures.append(name)

    if failures:
        joined = ", ".join(failures)
        raise SystemExit(f"Quality checks failed: {joined}")

    sys.stdout.write("Quality checks passed\n")
```

- [ ] **Step 2: Write `devtools/package_report.py`** — mirrors azure-vm-sdk version, adapted for proxmox_sdk

```python
from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import grimp

ROOT_PACKAGE = "proxmox_sdk"
EXCLUDED_MODULES = frozenset(
    {
        "proxmox_sdk.devtools.package_report",
        "proxmox_sdk.devtools.quality",
    }
)


@dataclass(frozen=True)
class ModuleMetrics:
    module: str
    internal_imports: int
    outgoing_imports: int
    incoming_imports: int
    external_imports: int
    instability: float


def calculate_metrics(
    *,
    modules: Sequence[str],
    edges: Iterable[tuple[str, str]],
) -> list[ModuleMetrics]:
    mod_set = frozenset(modules)
    internal_counts = {m: 0 for m in modules}
    outgoing_counts = {m: 0 for m in modules}
    incoming_counts = {m: 0 for m in modules}
    external_counts = {m: 0 for m in modules}

    for importer, imported in edges:
        if importer not in mod_set:
            continue
        if imported not in mod_set:
            external_counts[importer] += 1
        elif importer == imported:
            internal_counts[importer] += 1
        else:
            outgoing_counts[importer] += 1
            incoming_counts[imported] += 1

    metrics: list[ModuleMetrics] = []
    for module in modules:
        outgoing = outgoing_counts[module]
        incoming = incoming_counts[module]
        denominator = incoming + outgoing
        instability = round(outgoing / denominator, 2) if denominator else 0.0
        short = (
            module[len(ROOT_PACKAGE) + 1 :]
            if module.startswith(f"{ROOT_PACKAGE}.")
            else module
        )
        metrics.append(
            ModuleMetrics(
                module=short,
                internal_imports=internal_counts[module],
                outgoing_imports=outgoing,
                incoming_imports=incoming,
                external_imports=external_counts[module],
                instability=instability,
            )
        )
    return metrics


def format_metrics_table(metrics: Sequence[ModuleMetrics]) -> str:
    header = (
        f"{'module':30} {'internal':>8} {'outgoing':>8} "
        f"{'incoming':>8} {'external':>8} {'instability':>11}"
    )
    rows = [header, "-" * len(header)]
    for m in metrics:
        rows.append(
            f"{m.module:30} {m.internal_imports:8d} {m.outgoing_imports:8d} "
            f"{m.incoming_imports:8d} {m.external_imports:8d} "
            f"{m.instability:11.2f}"
        )
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report import graph metrics for proxmox_sdk."
    )
    parser.add_argument("--edges", action="store_true")
    parser.add_argument("--orphans", action="store_true")
    args = parser.parse_args()

    graph = grimp.build_graph(ROOT_PACKAGE, include_external_packages=False)
    modules = sorted(
        m
        for m in graph.modules
        if (m == ROOT_PACKAGE or m.startswith(f"{ROOT_PACKAGE}."))
        and m not in EXCLUDED_MODULES
    )

    edges: list[tuple[str, str]] = []
    for importer in modules:
        for imported in sorted(graph.find_modules_directly_imported_by(importer)):
            edges.append((importer, imported))

    print(format_metrics_table(calculate_metrics(modules=modules, edges=edges)))

    if args.edges:
        print("\n[Dependency edges]")
        for importer, imported in edges:
            if importer in EXCLUDED_MODULES or imported in EXCLUDED_MODULES:
                continue
            short_i = (
                importer[len(ROOT_PACKAGE) + 1 :]
                if importer.startswith(f"{ROOT_PACKAGE}.")
                else importer
            )
            short_d = (
                imported[len(ROOT_PACKAGE) + 1 :]
                if imported.startswith(f"{ROOT_PACKAGE}.")
                else imported
            )
            print(f"  {short_i} -> {short_d}")

    if args.orphans:
        all_with_deps: set[str] = set()
        for importer, imported in edges:
            if importer in EXCLUDED_MODULES or imported in EXCLUDED_MODULES:
                continue
            all_with_deps.add(importer)
            all_with_deps.add(imported)
        orphans = sorted(m for m in modules if m not in all_with_deps)
        if orphans:
            print("\n[Orphan modules]")
            for o in orphans:
                short = (
                    o[len(ROOT_PACKAGE) + 1 :]
                    if o.startswith(f"{ROOT_PACKAGE}.")
                    else o
                )
                print(f"  {short}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write `devtools/code_eval.py`** — stripped-down version of azure-vm-sdk's code_eval

```python
from __future__ import annotations

import ast
import argparse
from dataclasses import dataclass
from pathlib import Path

ROOT_PACKAGE = "proxmox_sdk"
EXCLUDED_MODULES = frozenset(
    {
        "proxmox_sdk.devtools.package_report",
        "proxmox_sdk.devtools.quality",
        "proxmox_sdk.devtools.code_eval",
    }
)


@dataclass
class Smell:
    category: str
    severity: str
    file: str
    line: int
    message: str


def _check_ast(modules: list[str]) -> list[Smell]:
    smells: list[Smell] = []
    root = Path(f"src/{ROOT_PACKAGE}")

    for py_file in sorted(root.rglob("*.py")):
        if "devtools" in str(py_file) and py_file.name != "__init__.py":
            continue
        source = py_file.read_text()
        tree = ast.parse(source)

        # bare except
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    if handler.type is None:
                        smells.append(
                            Smell(
                                "bug",
                                "high",
                                str(py_file),
                                handler.lineno,
                                "Bare except: — catches KeyboardInterrupt and SystemExit",
                            )
                        )

        # broad except
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type and ast.unparse(node.type) in (
                    "Exception",
                    "BaseException",
                ):
                    smells.append(
                        Smell(
                            "bug",
                            "medium",
                            str(py_file),
                            node.lineno,
                            f"Broad except clause catches {ast.unparse(node.type)}",
                        )
                    )

        # mutable defaults
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for default in node.args.defaults + node.args.kw_defaults:
                    if default and isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        smells.append(
                            Smell(
                                "bug",
                                "high",
                                str(py_file),
                                default.lineno,
                                f"Mutable default argument in `{node.name}()`",
                            )
                        )

        # large functions (>30 lines)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = node.end_lineno or node.lineno
                loc = end - node.lineno + 1
                if loc > 30:
                    smells.append(
                        Smell(
                            "simplification",
                            "medium",
                            str(py_file),
                            node.lineno,
                            f"Function `{node.name}()` is {loc} lines (max: 30)",
                        )
                    )

    return smells


def format_report(smells: list[Smell]) -> str:
    if not smells:
        return "No issues found."

    by_category: dict[str, list[Smell]] = {}
    for s in smells:
        by_category.setdefault(s.category, []).append(s)

    category_names = {
        "bug": "1. Possible Bugs",
        "simplification": "2. Simplification Opportunities",
    }

    lines: list[str] = []
    for cat_key in ("bug", "simplification"):
        items = by_category.get(cat_key, [])
        lines.append(category_names[cat_key])
        lines.append("-" * len(category_names[cat_key]))
        if not items:
            lines.append("  (none)\n")
            continue
        for item in sorted(
            items, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s.severity]
        ):
            lines.append(
                f"  [{item.severity.upper()}] {item.file}:{item.line} — {item.message}"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate code quality: bugs, simplifications, smells."
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path(f"src/{ROOT_PACKAGE}")
    modules = [
        str(p).replace("/", ".").replace(".py", "").replace("src.", "")
        for p in sorted(root.rglob("*.py"))
        if "devtools" not in str(p) or p.name == "__init__.py"
    ]

    all_smells = _check_ast(modules)

    if args.json:
        import json

        items = [
            {
                "category": s.category,
                "severity": s.severity,
                "file": s.file,
                "line": s.line,
                "message": s.message,
            }
            for s in all_smells
        ]
        print(json.dumps(items, indent=2))
    else:
        print(format_report(all_smells))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Commit**

```bash
git add src/proxmox_sdk/devtools/__init__.py src/proxmox_sdk/devtools/quality.py \
        src/proxmox_sdk/devtools/package_report.py src/proxmox_sdk/devtools/code_eval.py
git commit -m "feat: add devtools (quality, package_report, code_eval)"
```

---

### Task 7: Update `pyproject.toml` — entry points and new dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add entry points and grimp/import-linter dependencies**

```toml
[project.scripts]
proxmox-quality = "proxmox_sdk.devtools.quality:main"
proxmox-package-report = "proxmox_sdk.devtools.package_report:main"
proxmox-eval = "proxmox_sdk.devtools.code_eval:main"
proxmox-e2e = "proxmox_sdk.e2e:main"

[dependency-groups]
dev = [
    "grimp>=3.14",
    "import-linter>=2.11",
    "mypy>=1.0",
    "pytest>=9.0.2",
    "pytest-cov>=7.1.0",
    "ruff>=0.15.9",
]
```

- [ ] **Step 2: Run `uv sync`**

```bash
uv sync
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add entry points and grimp/import-linter dependencies"
```

---

### Task 8: Migrate tests to `tests/unit/` and update imports

**Files:**
- Move: `tests/test_client.py` → `tests/unit/test_client.py`
- Move: `tests/test_vm.py` → `tests/unit/test_vm.py`
- Move: `tests/test_models.py` → `tests/unit/test_models.py`
- Move: `tests/test_cloud_init.py` → `tests/unit/test_cloud_init.py`
- Move: `tests/test_fake_backend.py` → `tests/unit/test_backend.py`
- Move: `tests/test_routing.py` → `tests/unit/test_routing.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_integration.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Create unit/ and integration/ directories and move files**

```bash
mkdir -p tests/unit tests/integration
mv tests/test_client.py tests/unit/
mv tests/test_vm.py tests/unit/
mv tests/test_models.py tests/unit/
mv tests/test_cloud_init.py tests/unit/
mv tests/test_fake_backend.py tests/unit/test_backend.py
mv tests/test_routing.py tests/unit/
touch tests/unit/__init__.py tests/integration/__init__.py
```

- [ ] **Step 2: Update conftest.py** — `FakeBackend` now in `testing.py`

```python
import pytest

from proxmox_sdk import FakeBackend, ProxmoxClient


@pytest.fixture
def fake_backend() -> FakeBackend:
    backend = FakeBackend()
    backend.add_vm(
        100,
        node="pve",
        name="stopped-vm",
        status="stopped",
        cpus=2,
        maxmem=2 * 1024 * 1024 * 1024,
        mem=512 * 1024 * 1024,
        cpu=0.0,
    )
    backend.add_vm(
        101,
        node="pve",
        name="running-vm",
        status="running",
        cpus=4,
        maxmem=4 * 1024 * 1024 * 1024,
        mem=1 * 1024 * 1024 * 1024,
        cpu=0.05,
        uptime=3600,
    )
    backend.add_vm(
        9000, node="pve", name="ubuntu-template", status="stopped", template=True
    )
    return backend


@pytest.fixture
def client(fake_backend: FakeBackend) -> ProxmoxClient:
    return ProxmoxClient(
        host="fake-host",
        user="root@pam",
        password="fake-password",
        node="pve",
        backend=fake_backend,
    )
```

- [ ] **Step 3: Update test imports** — all tests that imported from `proxmox_sdk.backends.fake` must now import from `proxmox_sdk.testing`

```python
# Old (in test_cloud_init.py, test_routing.py, etc.)
from proxmox_sdk.backends.fake import FakeBackend
from proxmox_sdk.backends.ssh import FakeSshBackend

# New
from proxmox_sdk.testing import FakeBackend, FakeSshBackend

# or
from proxmox_sdk import FakeBackend, FakeSshBackend
```

- [ ] **Step 4: Update conftest.py and test imports for new `launch()` API**

All tests calling `client.create_vm(...)` must change to `client.launch(...)`.
All tests checking for `first == mgr.add_rules(...)` already fixed.

- [ ] **Step 5: Write `tests/integration/test_integration.py`** — skeleton requiring real Proxmox

```python
"""Integration tests — require a real Proxmox server.

Set env vars: PROXMOX_HOST, PROXMOX_USER, PROXMOX_PASSWORD, PROXMOX_NODE
Skip by default: pytest -m "not integration"
"""

import os
import uuid

import pytest

from proxmox_sdk import ProxmoxClient, VmConfig


pytestmark = pytest.mark.integration


def _env_or_skip(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    return value


@pytest.fixture
def client() -> ProxmoxClient:
    return ProxmoxClient(
        host=_env_or_skip("PROXMOX_HOST"),
        user=_env_or_skip("PROXMOX_USER"),
        password=os.environ.get("PROXMOX_PASSWORD"),
        node=os.environ.get("PROXMOX_NODE"),
        verify_ssl=False,
    )


def test_launch_and_delete(client: ProxmoxClient) -> None:
    name = f"test-{uuid.uuid4().hex[:8]}"
    templates = client.list_templates()
    if not templates:
        pytest.skip("no templates available")
    template_id = templates[0].vm_id

    vm = client.launch(name, template_id=template_id, start=True)
    try:
        vm.wait_for_agent(timeout=120)
        info = vm.info()
        assert info.name == name
        assert info.state.value == "running"
    finally:
        vm.delete(purge=True)
```

- [ ] **Step 6: Update `pyproject.toml` for test markers and integration skip by default**

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "integration: marks tests as integration tests (require real Proxmox server)",
]
addopts = "-m 'not integration'"
```

- [ ] **Step 7: Run all unit tests to verify**

```bash
uv run pytest tests/unit/ -v
```

Expected: all 83 tests pass.

- [ ] **Step 8: Commit**

```bash
git add tests/ pyproject.toml
git commit -m "test: restructure to unit/integration split, add integration skeleton"
```

---

### Task 9: Update imports across all source files

**Files:**
- Modify: `src/proxmox_sdk/vm.py`
- Modify: `src/proxmox_sdk/routing.py`

- [ ] **Step 1: Fix imports in `vm.py`** — `ProxmoxBackend` now in `_backend.py`

```python
# In vm.py TYPE_CHECKING block
from proxmox_sdk._backend import ProxmoxBackend
```

- [ ] **Step 2: Fix imports in `routing.py`** — `SshBackend`, `PortMapping` now in new locations

```python
# In routing.py TYPE_CHECKING block
from proxmox_sdk._backend import SshBackend
```

Also move `PortMapping` from `routing.py` to `models.py` (or keep it in routing.py and import from there in `__init__.py`). Keep it in routing.py for now — `__init__.py` already imports it from there.

- [ ] **Step 3: Run full checks**

```bash
uv run ruff check src/ tests/
uv run mypy src/
uv run pytest tests/unit/ -v
```

Expected: all clean, all tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/proxmox_sdk/vm.py src/proxmox_sdk/routing.py
git commit -m "fix: update internal imports after backend consolidation"
```

---

### Task 10: Final verification and tag

- [ ] **Step 1: Run all quality checks**

```bash
uv run ruff check src/ tests/
uv run mypy src/
uv run pytest tests/unit/ -v --cov=src --cov-report=term --cov-fail-under=75
```

- [ ] **Step 2: Bump version and tag**

```bash
# Edit pyproject.toml: version = "0.2.0"
git add pyproject.toml
git commit -m "release: bump version to 0.2.0 — full API alignment with azure-vm-sdk"
git push origin master
git tag -a v0.2.0 -m "v0.2.0: API aligned with azure-vm-sdk patterns"
git push origin v0.2.0
```

---

### Dependency Order

```
Task 1 (_backend.py, testing.py) → Task 2 (VmConfig) → Task 3 (client.py)
→ Task 4 (vm.py) → Task 5 (e2e.py) → Task 6 (devtools)
→ Task 7 (pyproject.toml) → Task 8 (tests migration) → Task 9 (fix imports)
→ Task 10 (final verification + tag)
```

Tasks 1-2 are foundational — everything depends on them. Tasks 3-7 can be done in parallel. Task 8 depends on 1,2,3. Task 9 is cleanup. Task 10 is the finish line.
