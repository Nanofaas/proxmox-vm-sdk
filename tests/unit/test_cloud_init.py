"""Tests for CloudInitConfig model and its Proxmox API serialization."""

import pytest

from proxmox_vm_sdk import ProxmoxClient
from proxmox_vm_sdk.models import CloudInitConfig
from proxmox_vm_sdk.testing import FakeBackend
from proxmox_vm_sdk.vm import ProxmoxVM


@pytest.fixture
def vm_100() -> tuple[FakeBackend, ProxmoxVM]:
    """FakeBackend with VM 100 pre-seeded, plus the ProxmoxVM handle."""
    fb = FakeBackend()
    fb.add_vm(100, node="pve", name="my-vm")
    client = ProxmoxClient(host="x", user="x", node="pve", backend=fb)
    return fb, client.get_vm(100)


def test_cloud_init_to_api_params_full() -> None:
    cfg = CloudInitConfig(
        username="ubuntu",
        password="secret",
        ssh_keys=["ssh-rsa AAAA user@host"],
        ip_config="ip=dhcp",
        nameserver="8.8.8.8",
        searchdomain="local",
    )
    params = cfg.to_api_params()
    assert params["ciuser"] == "ubuntu"
    assert params["cipassword"] == "secret"
    assert "sshkeys" in params
    assert " " not in params["sshkeys"]  # spaces must be percent-encoded
    assert "%" in params["sshkeys"]  # encoding must have occurred
    assert params["ipconfig0"] == "ip=dhcp"
    assert params["nameserver"] == "8.8.8.8"
    assert params["searchdomain"] == "local"


def test_cloud_init_to_api_params_empty() -> None:
    cfg = CloudInitConfig()
    assert cfg.to_api_params() == {}


def test_cloud_init_to_api_params_omits_none_fields() -> None:
    cfg = CloudInitConfig(username="ubuntu")
    params = cfg.to_api_params()
    assert "cipassword" not in params
    assert "sshkeys" not in params
    assert "ipconfig0" not in params


def test_cloud_init_ssh_keys_url_encoded() -> None:
    cfg = CloudInitConfig(ssh_keys=["ssh-rsa AAAA key1", "ssh-rsa BBBB key2"])
    params = cfg.to_api_params()
    encoded = params["sshkeys"]
    # Must not contain literal spaces or newlines (they must be percent-encoded)
    assert " " not in encoded
    assert "\n" not in encoded
    # Must contain percent-encoding (spaces in "ssh-rsa AAAA" become %20)
    assert "%" in encoded
    # Round-trip must recover original
    from urllib.parse import unquote

    decoded = unquote(encoded)
    assert decoded == "ssh-rsa AAAA key1\nssh-rsa BBBB key2"


def test_cloud_init_empty_string_treated_as_absent() -> None:
    cfg = CloudInitConfig(username="", ip_config="")
    params = cfg.to_api_params()
    assert "ciuser" not in params
    assert "ipconfig0" not in params


def test_cloud_init_static_ip_config() -> None:
    cfg = CloudInitConfig(ip_config="ip=10.0.0.5/24,gw=10.0.0.1")
    params = cfg.to_api_params()
    assert params["ipconfig0"] == "ip=10.0.0.5/24,gw=10.0.0.1"


def test_fake_backend_stores_config_put() -> None:
    fb = FakeBackend()
    fb.add_vm(100, node="pve", name="test-vm")
    fb.put("nodes/pve/qemu/100/config", ciuser="ubuntu", ipconfig0="ip=dhcp")
    vm = fb.get("nodes/pve/qemu/100/config")
    assert vm["ciuser"] == "ubuntu"
    assert vm["ipconfig0"] == "ip=dhcp"


def test_fake_backend_get_config_returns_vm_dict() -> None:
    fb = FakeBackend()
    fb.add_vm(100, node="pve", name="my-vm")
    result = fb.get("nodes/pve/qemu/100/config")
    assert result["name"] == "my-vm"
    assert result["vmid"] == 100


def test_configure_cloud_init_calls_put_config(
    vm_100: tuple[FakeBackend, ProxmoxVM],
) -> None:
    fb, vm = vm_100
    vm.configure_cloud_init(CloudInitConfig(username="ubuntu", ip_config="ip=dhcp"))
    fb.assert_called_with("PUT", "nodes/pve/qemu/100/config")


def test_configure_cloud_init_stores_fields(
    vm_100: tuple[FakeBackend, ProxmoxVM],
) -> None:
    fb, vm = vm_100
    vm.configure_cloud_init(
        CloudInitConfig(
            username="ubuntu",
            password="s3cr3t",
            ssh_keys=["ssh-rsa AAAA user@host"],
            ip_config="ip=dhcp",
            nameserver="1.1.1.1",
            searchdomain="home.local",
        )
    )
    stored = fb.get("nodes/pve/qemu/100/config")
    assert stored["ciuser"] == "ubuntu"
    assert stored["cipassword"] == "s3cr3t"
    assert "ssh-rsa" in stored["sshkeys"]
    assert stored["ipconfig0"] == "ip=dhcp"
    assert stored["nameserver"] == "1.1.1.1"
    assert stored["searchdomain"] == "home.local"


def test_configure_cloud_init_noop_on_empty_config(
    vm_100: tuple[FakeBackend, ProxmoxVM],
) -> None:
    fb, vm = vm_100
    before_calls = len(fb.calls)
    vm.configure_cloud_init(CloudInitConfig())
    assert len(fb.calls) == before_calls


def test_create_vm_cloud_init_applied_before_start() -> None:
    """cloud-init config must be applied before VM is started."""
    fb = FakeBackend()
    fb.add_vm(
        9000,
        node="pve",
        name="template",
        status="stopped",
        template=True,
        ide2="local-lvm:vm-9000-cloudinit,media=cdrom",
    )
    client = ProxmoxClient(host="x", user="x", node="pve", backend=fb)

    cfg = CloudInitConfig(username="ubuntu")
    client.launch("new-vm", template_id=9000, cloud_init_config=cfg, start=True)

    # Find the new VM id (not 9000)
    new_vm = next(v for v in fb._vms.values() if v["name"] == "new-vm")
    assert new_vm is not None  # confirm VM was created

    # Verify order: config must appear before start in calls
    config_idx = next(
        i
        for i, (m, p, _) in enumerate(fb.calls)
        if m == "PUT" and p.endswith("/config")
    )
    start_idx = next(
        i
        for i, (m, p, _) in enumerate(fb.calls)
        if m == "POST" and p.endswith("/start")
    )
    assert config_idx < start_idx


def test_cloud_init_config_importable_from_top_level() -> None:
    from proxmox_vm_sdk import CloudInitConfig

    cfg = CloudInitConfig(username="ubuntu")
    assert cfg.username == "ubuntu"
