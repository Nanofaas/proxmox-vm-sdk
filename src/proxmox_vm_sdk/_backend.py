from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from proxmox_vm_sdk.exceptions import (
    ProxmoxAPIError,
    ProxmoxTimeoutError,
    TaskFailedError,
    VmNotFoundError,
)

# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ProxmoxBackend(Protocol):
    """Abstracts all Proxmox API calls.

    Paths are flat strings, e.g. "nodes/pve/qemu/100/status/start".
    The backend owns the translation from path + kwargs to the actual
    HTTP call (or in-memory mutation for FakeBackend).
    """

    def get(self, path: str, **params: Any) -> Any: ...

    def post(self, path: str, **data: Any) -> Any: ...

    def put(self, path: str, **data: Any) -> Any: ...

    def delete(self, path: str, **params: Any) -> Any: ...

    def wait_for_task(self, node: str, upid: str, timeout: float = 60) -> None: ...


@runtime_checkable
class SshBackend(Protocol):
    """Abstracts SSH command execution on a remote host.

    Used by ProxmoxRoutingManager to run commands on the Proxmox host.
    Implementations: ParamikoSshBackend (real), FakeSshBackend (tests).
    """

    def run(self, command: str) -> tuple[int, str, str]:
        """Execute a command on the remote host.

        Returns (exit_code, stdout, stderr).
        """
        ...

    def read_file(self, path: str) -> str:
        """Read a remote file and return its contents."""
        ...

    def write_file(self, path: str, content: str) -> None:
        """Write content to a remote file (atomic via tmp file)."""
        ...


# ---------------------------------------------------------------------------
# Real implementations
# ---------------------------------------------------------------------------


class ProxmoxerBackend:
    """Real backend that delegates to the proxmoxer library.

    Translates flat path strings like "nodes/pve/qemu/100/status/start"
    into proxmoxer attribute-chain calls.
    """

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> Any:
        """Walk the proxmoxer attribute/call chain from a flat path string."""
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
            raise  # unreachable, but satisfies type checker

    @staticmethod
    def _translate_exception(exc: Exception, path: str) -> None:
        """Re-raise proxmoxer exceptions as proxmox-vm-sdk exceptions."""
        # proxmoxer raises ResourceException; we check by name to avoid
        # importing proxmoxer in the exception module
        exc_type = type(exc).__name__
        if exc_type == "ResourceException":
            status_code = getattr(exc, "status_code", 0)
            content = str(getattr(exc, "content", str(exc)))
            if status_code == 404 or "does not exist" in content.lower():
                raise VmNotFoundError(path) from exc
            raise ProxmoxAPIError(status_code, content, path) from exc


class ParamikoSshBackend:
    """Real SSH backend using paramiko."""

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
                "paramiko is required for SSH operations. "
                "Install it with: pip install paramiko"
            ) from exc

        self._client = paramiko.SSHClient()
        # The SDK trusts an unknown host key on first connect. This is a
        # deliberate, if debatable, default: the routing manager is pointed at
        # a host the caller already administers over SSH, and RejectPolicy
        # would break every first-time connection. It is NOT free - a
        # machine-in-the-middle between the SDK and the Proxmox node is
        # accepted - so verify against known_hosts if that threat is in scope.
        auto_add_policy = paramiko.AutoAddPolicy()
        self._client.set_missing_host_key_policy(auto_add_policy)  # nosec B507
        # paramiko already types key_filename and password as `str | None`, so
        # the keyword arguments can be passed directly. They used to be built up
        # in a `dict[str, object]` and splatted, which made every argument
        # `object` and defeated the type check for this call entirely. The
        # `or None` keeps the old `if ssh_key_path:` semantics: an empty string
        # means "not supplied", not "use the empty path".
        self._client.connect(
            hostname=host,
            username=user,
            port=port,
            key_filename=ssh_key_path or None,
            password=password or None,
        )

    def run(self, command: str) -> tuple[int, str, str]:
        _, stdout, stderr = self._client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout.read().decode(), stderr.read().decode()

    def read_file(self, path: str) -> str:
        # Reads go over SFTP to match write_file below. Building a `cat {path}`
        # shell string instead interpolated the caller's path straight into a
        # remote command; the path is caller-supplied, so that was an injection
        # surface for no benefit over asking the SFTP subsystem for the bytes.
        sftp = self._client.open_sftp()
        with sftp.file(path, "r") as f:
            return f.read().decode()

    def write_file(self, path: str, content: str) -> None:
        # Write atomically via a tmp file
        tmp = f"{path}.proxmox_vm_sdk_tmp"
        sftp = self._client.open_sftp()
        with sftp.file(tmp, "w") as f:
            f.write(content)
        sftp.close()
        self.run(f"mv {tmp} {path}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ParamikoSshBackend:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Command result
# ---------------------------------------------------------------------------


class CommandResult:
    """Result of a command executed inside a VM via the QEMU guest agent."""

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
        self.args = args

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def __repr__(self) -> str:
        return (
            f"CommandResult(exit_code={self.exit_code}, "
            f"stdout={self.stdout!r}, stderr={self.stderr!r}, "
            f"args={self.args!r})"
        )
