"""Exceptions raised by proxmox-vm-sdk.

Every error derives from `ProxmoxError`, so a caller can catch the base class
for a blanket handler or one of the subclasses below to react to a specific
failure. Each subclass keeps the arguments describing the failure as
attributes and formats a human-readable message from them.
"""

from __future__ import annotations


class ProxmoxError(Exception):
    """Base class for all proxmox-vm-sdk errors."""


class ProxmoxAuthError(ProxmoxError):
    """Authentication or authorization failed."""

    def __init__(self, host: str, user: str) -> None:
        """Record the host and user whose authentication was rejected."""
        self.host = host
        self.user = user
        super().__init__(f"Authentication failed for user '{user}' on host '{host}'")


class ProxmoxConnectionError(ProxmoxError):
    """Could not reach the Proxmox host."""

    def __init__(self, host: str, port: int) -> None:
        """Record the host and port that could not be reached."""
        self.host = host
        self.port = port
        super().__init__(f"Could not connect to Proxmox at {host}:{port}")


class ProxmoxAPIError(ProxmoxError):
    """Proxmox API returned an error response."""

    def __init__(self, status_code: int, message: str, path: str) -> None:
        """Record the failed request's status code, message, and API path."""
        self.status_code = status_code
        self.message = message
        self.path = path
        super().__init__(f"Proxmox API error {status_code} at '{path}': {message}")


class VmNotFoundError(ProxmoxError):
    """No VM matching the given ID or name."""

    def __init__(self, identifier: int | str) -> None:
        """Record the VM ID or name that matched no VM."""
        self.identifier = identifier
        super().__init__(f"VM not found: {identifier!r}")


class VmStateError(ProxmoxError):
    """Operation not valid for the VM's current state."""

    def __init__(self, vm_id: int, current: str, required: str) -> None:
        """Record the VM, its current state, and the state the operation needs."""
        self.vm_id = vm_id
        self.current = current
        self.required = required
        super().__init__(
            f"VM {vm_id} is {current!r}, but operation requires {required!r}"
        )


class NodeNotFoundError(ProxmoxError):
    """Specified node does not exist in the cluster."""

    def __init__(self, name: str) -> None:
        """Record the node name that is absent from the cluster."""
        self.name = name
        super().__init__(f"Node not found: {name!r}")


class ProxmoxTimeoutError(ProxmoxError):
    """A wait operation exceeded its timeout."""

    def __init__(self, vm_id: int, operation: str, timeout: float) -> None:
        """Record the VM, the operation, and its timeout in seconds."""
        self.vm_id = vm_id
        self.operation = operation
        self.timeout = timeout
        super().__init__(f"VM {vm_id}: '{operation}' timed out after {timeout}s")


class SnapshotNotFoundError(ProxmoxError):
    """Specified snapshot does not exist on the VM."""

    def __init__(self, vm_id: int, snapshot: str) -> None:
        """Record the VM and the snapshot name that does not exist on it."""
        self.vm_id = vm_id
        self.snapshot = snapshot
        super().__init__(f"Snapshot {snapshot!r} not found on VM {vm_id}")


class TaskFailedError(ProxmoxError):
    """An async Proxmox task finished with a non-OK exit status."""

    def __init__(self, upid: str, exit_status: str) -> None:
        """Record the task UPID and the non-OK exit status it reported."""
        self.upid = upid
        self.exit_status = exit_status
        super().__init__(f"Task {upid!r} failed with status: {exit_status}")
