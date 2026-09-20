"""Tests for proxmox_vm_sdk.exceptions.

Each exception records the arguments describing the failure as attributes and
formats a human-readable message from them, so callers can both react
programmatically and surface a useful message. These tests pin the exact
attribute names, the exact message text, and the inheritance hierarchy.
"""

from __future__ import annotations

import pytest

import proxmox_vm_sdk
from proxmox_vm_sdk.exceptions import (
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

ALL_ERROR_CLASSES = [
    ProxmoxAuthError,
    ProxmoxConnectionError,
    ProxmoxAPIError,
    VmNotFoundError,
    VmStateError,
    NodeNotFoundError,
    ProxmoxTimeoutError,
    SnapshotNotFoundError,
    TaskFailedError,
]


# ---------------------------------------------------------------------------
# Hierarchy and public API
# ---------------------------------------------------------------------------


def test_proxmox_error_is_an_exception() -> None:
    assert issubclass(ProxmoxError, Exception)


@pytest.mark.parametrize("exc_class", ALL_ERROR_CLASSES)
def test_every_error_derives_from_proxmox_error(exc_class: type[Exception]) -> None:
    assert issubclass(exc_class, ProxmoxError)


def test_error_classes_are_distinct() -> None:
    assert len({cls.__name__ for cls in ALL_ERROR_CLASSES}) == len(ALL_ERROR_CLASSES)


@pytest.mark.parametrize("exc_class", ALL_ERROR_CLASSES)
def test_errors_are_exported_from_package_root(exc_class: type[Exception]) -> None:
    assert getattr(proxmox_vm_sdk, exc_class.__name__) is exc_class


def test_base_class_catches_every_subclass() -> None:
    raised = [
        ProxmoxAuthError("pve1", "root@pam"),
        ProxmoxConnectionError("pve1", 8006),
        ProxmoxAPIError(500, "boom", "/nodes"),
        VmNotFoundError(100),
        VmStateError(100, "stopped", "running"),
        NodeNotFoundError("pve9"),
        ProxmoxTimeoutError(100, "start", 30.0),
        SnapshotNotFoundError(100, "snap1"),
        TaskFailedError("UPID:x", "some error"),
    ]
    for exc in raised:
        with pytest.raises(ProxmoxError):
            raise exc


# ---------------------------------------------------------------------------
# ProxmoxAuthError
# ---------------------------------------------------------------------------


def test_auth_error_attributes_and_message() -> None:
    err = ProxmoxAuthError(host="pve1.example.com", user="root@pam")
    assert err.host == "pve1.example.com"
    assert err.user == "root@pam"
    assert str(err) == (
        "Authentication failed for user 'root@pam' on host 'pve1.example.com'"
    )


# ---------------------------------------------------------------------------
# ProxmoxConnectionError
# ---------------------------------------------------------------------------


def test_connection_error_attributes_and_message() -> None:
    err = ProxmoxConnectionError(host="10.0.0.5", port=8006)
    assert err.host == "10.0.0.5"
    assert err.port == 8006
    assert str(err) == "Could not connect to Proxmox at 10.0.0.5:8006"


def test_connection_error_keeps_port_as_int() -> None:
    err = ProxmoxConnectionError("10.0.0.5", 443)
    assert err.port == 443
    assert isinstance(err.port, int)


# ---------------------------------------------------------------------------
# ProxmoxAPIError
# ---------------------------------------------------------------------------


def test_api_error_attributes_and_message() -> None:
    err = ProxmoxAPIError(status_code=403, message="permission denied", path="/nodes/x")
    assert err.status_code == 403
    assert err.message == "permission denied"
    assert err.path == "/nodes/x"
    assert str(err) == "Proxmox API error 403 at '/nodes/x': permission denied"


# ---------------------------------------------------------------------------
# VmNotFoundError
# ---------------------------------------------------------------------------


def test_vm_not_found_with_int_identifier() -> None:
    err = VmNotFoundError(101)
    assert err.identifier == 101
    assert str(err) == "VM not found: 101"


def test_vm_not_found_with_name_identifier() -> None:
    err = VmNotFoundError("web-01")
    assert err.identifier == "web-01"
    assert str(err) == "VM not found: 'web-01'"


# ---------------------------------------------------------------------------
# VmStateError
# ---------------------------------------------------------------------------


def test_vm_state_error_attributes_and_message() -> None:
    err = VmStateError(vm_id=101, current="stopped", required="running")
    assert err.vm_id == 101
    assert err.current == "stopped"
    assert err.required == "running"
    assert str(err) == "VM 101 is 'stopped', but operation requires 'running'"


# ---------------------------------------------------------------------------
# NodeNotFoundError
# ---------------------------------------------------------------------------


def test_node_not_found_attributes_and_message() -> None:
    err = NodeNotFoundError("pve9")
    assert err.name == "pve9"
    assert str(err) == "Node not found: 'pve9'"


# ---------------------------------------------------------------------------
# ProxmoxTimeoutError
# ---------------------------------------------------------------------------


def test_timeout_error_attributes_and_message() -> None:
    err = ProxmoxTimeoutError(vm_id=100, operation="start", timeout=30.0)
    assert err.vm_id == 100
    assert err.operation == "start"
    assert err.timeout == 30.0
    assert str(err) == "VM 100: 'start' timed out after 30.0s"


# ---------------------------------------------------------------------------
# SnapshotNotFoundError
# ---------------------------------------------------------------------------


def test_snapshot_not_found_attributes_and_message() -> None:
    err = SnapshotNotFoundError(vm_id=100, snapshot="pre-upgrade")
    assert err.vm_id == 100
    assert err.snapshot == "pre-upgrade"
    assert str(err) == "Snapshot 'pre-upgrade' not found on VM 100"


# ---------------------------------------------------------------------------
# TaskFailedError
# ---------------------------------------------------------------------------


def test_task_failed_attributes_and_message() -> None:
    err = TaskFailedError(upid="UPID:pve1:0000ABCD", exit_status="some error")
    assert err.upid == "UPID:pve1:0000ABCD"
    assert err.exit_status == "some error"
    assert str(err) == "Task 'UPID:pve1:0000ABCD' failed with status: some error"


def test_message_is_the_only_arg() -> None:
    err = VmNotFoundError(100)
    assert err.args == ("VM not found: 100",)
