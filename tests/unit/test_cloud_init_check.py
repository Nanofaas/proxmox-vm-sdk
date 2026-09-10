"""Tests for cloud-init drive pre-flight check.

Proxmox silently ignores cloud-init config if the VM has no cloud-init
CD-ROM drive attached. These tests verify we detect that and raise early.
"""

from __future__ import annotations

import pytest

from proxmox_sdk import FakeBackend, ProxmoxClient
from proxmox_sdk.exceptions import ProxmoxError
from proxmox_sdk.models import CloudInitConfig
from proxmox_sdk.vm import ProxmoxVM


def _backend_with_ci_drive() -> FakeBackend:
    b = FakeBackend()
    b.add_vm(
        9000,
        node="pve",
        name="ci-template",
        status="stopped",
        template=True,
        ide2="local-lvm:vm-9000-cloudinit,media=cdrom",
    )
    return b


def _backend_without_ci_drive() -> FakeBackend:
    b = FakeBackend()
    b.add_vm(9000, node="pve", name="plain-template", status="stopped", template=True)
    return b


def _client(backend: FakeBackend) -> ProxmoxClient:
    return ProxmoxClient(
        host="fake", user="root@pam", password="x", node="pve", backend=backend
    )


# ---------------------------------------------------------------------------
# ProxmoxVM.has_cloud_init_drive
# ---------------------------------------------------------------------------


def test_has_cloud_init_drive_returns_true_when_drive_present() -> None:
    backend = _backend_with_ci_drive()
    vm = ProxmoxVM(9000, "pve", backend)
    assert vm.has_cloud_init_drive() is True


def test_has_cloud_init_drive_returns_false_when_no_drive() -> None:
    backend = _backend_without_ci_drive()
    vm = ProxmoxVM(9000, "pve", backend)
    assert vm.has_cloud_init_drive() is False


def test_has_cloud_init_drive_detects_different_bus_names() -> None:
    """Drive might be on sata, ide, or scsi bus."""
    for bus in ("ide2", "sata0", "scsi1"):
        backend = FakeBackend()
        backend.add_vm(
            9000,
            node="pve",
            name="t",
            status="stopped",
            template=True,
            **{bus: "local-lvm:vm-9000-cloudinit,media=cdrom"},
        )
        vm = ProxmoxVM(9000, "pve", backend)
        assert vm.has_cloud_init_drive() is True, f"should detect drive on {bus}"


# ---------------------------------------------------------------------------
# client.launch with cloud-init config
# ---------------------------------------------------------------------------


def test_launch_raises_when_cloud_init_config_but_no_drive() -> None:
    client = _client(_backend_without_ci_drive())
    cfg = CloudInitConfig(username="ubuntu", ssh_keys=["ssh-rsa AAAA test"])
    with pytest.raises(ProxmoxError, match="cloud-init drive"):
        client.launch("test-vm", template_id=9000, cloud_init_config=cfg, start=False)


def test_launch_error_mentions_template_vmid() -> None:
    client = _client(_backend_without_ci_drive())
    cfg = CloudInitConfig(username="ubuntu")
    with pytest.raises(ProxmoxError, match="9000"):
        client.launch("test-vm", template_id=9000, cloud_init_config=cfg, start=False)


def test_launch_succeeds_when_cloud_init_drive_present() -> None:
    client = _client(_backend_with_ci_drive())
    cfg = CloudInitConfig(username="ubuntu", ssh_keys=["ssh-rsa AAAA test"])
    vm = client.launch("test-vm", template_id=9000, cloud_init_config=cfg, start=False)
    assert vm is not None


def test_launch_without_cloud_init_config_skips_check() -> None:
    """No cloud_init_config → no check → template without drive is fine."""
    client = _client(_backend_without_ci_drive())
    vm = client.launch("plain-vm", template_id=9000, start=False)
    assert vm is not None
