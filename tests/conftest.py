import pytest

from proxmox_vm_sdk import FakeBackend, ProxmoxClient


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
    # Template VM (with cloud-init drive, as a properly configured template would have)
    backend.add_vm(
        9000,
        node="pve",
        name="ubuntu-template",
        status="stopped",
        template=True,
        ide2="local-lvm:vm-9000-cloudinit,media=cdrom",
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
