import sys
import time

import pytest

from proxmox_vm_sdk import (
    FakeBackend,
    ProxmoxClient,
    ProxmoxerBackend,
    ProxmoxVM,
    VmConfig,
    VmInfo,
    VmNotFoundError,
)
from proxmox_vm_sdk.exceptions import ProxmoxError
from proxmox_vm_sdk.models import CloudInitConfig, VmState


def test_list_returns_all_vms(client: ProxmoxClient) -> None:
    vms = client.list()
    assert len(vms) == 3
    assert all(isinstance(v, VmInfo) for v in vms)


def test_list_node_filter(client: ProxmoxClient) -> None:
    vms = client.list(node="pve")
    assert len(vms) == 3


def test_get_vm_returns_proxmox_vm(client: ProxmoxClient) -> None:
    from proxmox_vm_sdk import ProxmoxVM

    vm = client.get_vm(100)
    assert vm.vm_id == 100
    assert vm.node == "pve"
    assert isinstance(vm, ProxmoxVM)


def test_get_vm_raises_not_found(client: ProxmoxClient) -> None:
    with pytest.raises(VmNotFoundError) as exc_info:
        client.get_vm(999)
    assert exc_info.value.identifier == 999


def test_find_vm_by_name(client: ProxmoxClient) -> None:
    vm = client.get_vm("stopped-vm")
    assert vm.vm_id == 100


def test_find_vm_not_found(client: ProxmoxClient) -> None:
    with pytest.raises(VmNotFoundError) as exc_info:
        client.get_vm("nonexistent")
    assert exc_info.value.identifier == "nonexistent"


def test_list_nodes(client: ProxmoxClient) -> None:
    from proxmox_vm_sdk import NodeInfo

    nodes = client.list_nodes()
    assert len(nodes) == 1
    assert isinstance(nodes[0], NodeInfo)
    assert nodes[0].name == "pve"


def test_list_templates(client: ProxmoxClient) -> None:
    templates = client.list_templates()
    assert len(templates) == 1
    assert templates[0].vm_id == 9000
    assert templates[0].name == "ubuntu-template"


def test_list_templates_returns_hardware_fields(
    fake_backend: FakeBackend,
) -> None:
    fake_backend.add_vm(
        9001,
        node="pve",
        name="debian-template",
        status="stopped",
        template=True,
        maxcpu=2,
        maxmem=2 * 1024 * 1024 * 1024,
    )
    client = ProxmoxClient(host="x", user="x", node="pve", backend=fake_backend)
    templates = client.list_templates()
    debian_tmpl = next(t for t in templates if t.name == "debian-template")
    assert debian_tmpl.cores == 2
    assert debian_tmpl.memory_mb == 2048


def test_find_template_by_name(
    fake_backend: FakeBackend,
) -> None:
    fake_backend.add_vm(
        9001,
        node="pve",
        name="custom-template",
        status="stopped",
        template=True,
        maxcpu=4,
        maxmem=4 * 1024 * 1024 * 1024,
    )
    client = ProxmoxClient(host="x", user="x", node="pve", backend=fake_backend)
    t = client.find_template("custom-template")
    assert t.vm_id == 9001
    assert t.name == "custom-template"
    assert t.cores == 4
    assert t.memory_mb == 4096


def test_find_template_raises_if_not_found(
    client: ProxmoxClient,
) -> None:
    with pytest.raises(VmNotFoundError):
        client.find_template("nonexistent-template")


def test_create_vm_calls_clone_then_start(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.launch("new-vm", template_id=9000, start=True)
    assert vm.vm_id is not None
    # Should have called POST clone and POST start
    methods_paths = [(m, p) for m, p, _ in fake_backend.calls]
    assert any("clone" in p for _, p in methods_paths)
    assert any("start" in p for _, p in methods_paths)


def test_create_vm_no_start(client: ProxmoxClient, fake_backend: FakeBackend) -> None:
    client.launch("lazy-vm", template_id=9000, start=False)
    methods_paths = [(m, p) for m, p, _ in fake_backend.calls]
    assert not any("start" in p for _, p in methods_paths)


def test_from_url_parses_host_and_port() -> None:
    from proxmox_vm_sdk._utils import parse_proxmox_url

    host, port = parse_proxmox_url("https://192.168.1.5:8006/api2/json")
    assert host == "192.168.1.5"
    assert port == 8006

    host2, port2 = parse_proxmox_url("proxmox-host")
    assert host2 == "proxmox-host"
    assert port2 == 8006


def test_purge_stopped(client: ProxmoxClient, fake_backend: FakeBackend) -> None:
    client.purge()
    # VM 100 (stopped) and 9000 (template, stopped) should be deleted;
    # VM 101 (running) should remain
    remaining = client.list()
    for vm in remaining:
        assert vm.state == VmState.RUNNING


def test_create_vm_with_cloud_init_applies_config(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    cfg = CloudInitConfig(username="ubuntu", ip_config="ip=dhcp")
    vm = client.launch("ci-vm", template_id=9000, cloud_init_config=cfg, start=False)

    fake_backend.assert_called_with("PUT", f"nodes/pve/qemu/{vm.vm_id}/config")
    stored = fake_backend.get(f"nodes/pve/qemu/{vm.vm_id}/config")
    assert stored["ciuser"] == "ubuntu"
    assert stored["ipconfig0"] == "ip=dhcp"


def test_create_vm_without_cloud_init_does_not_call_config(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    client.launch("plain-vm", template_id=9000, start=False)
    calls = [(m, p) for m, p, _ in fake_backend.calls]
    assert not any(p.endswith("/config") for _, p in calls)


def test_create_vm_applies_cores_and_memory(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.launch("hw-vm", template_id=9000, cores=4, memory_mb=4096, start=False)
    stored = fake_backend.get(f"nodes/pve/qemu/{vm.vm_id}/config")
    assert stored["cores"] == 4
    assert stored["memory"] == 4096


def test_create_vm_info_reports_configured_cores(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.launch("info-vm", template_id=9000, cores=2, start=False)
    assert vm.info().cpu_count == 2


def test_create_vm_uses_long_task_timeout(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    seen_timeouts: list[float] = []

    def wait_for_task(node: str, upid: str, timeout: float = 60) -> None:
        seen_timeouts.append(timeout)

    fake_backend.wait_for_task = wait_for_task  # type: ignore[method-assign]

    client.launch("timeout-vm", template_id=9000, start=False)
    assert seen_timeouts == [300.0]


def test_create_vm_resizes_disk(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.launch("disk-vm", template_id=9000, disk_gb=50, start=False)
    fake_backend.assert_called_with("PUT", f"nodes/pve/qemu/{vm.vm_id}/resize")


def test_create_vm_no_config_when_no_hw_params(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.launch("plain-vm", template_id=9000, start=False)
    # No PUT /config call should be made for hw (cloud_init is also None here)
    hw_config_calls = [
        (m, p)
        for m, p, _ in fake_backend.calls
        if m == "PUT" and p == f"nodes/pve/qemu/{vm.vm_id}/config"
    ]
    assert hw_config_calls == []


# ---------------------------------------------------------------------------
# Connection / backend construction
# ---------------------------------------------------------------------------


def _stub_proxmoxer(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Replace proxmoxer.ProxmoxAPI with a recorder and return the records."""
    import proxmoxer

    created: list[tuple[str, dict]] = []

    def factory(host: str, **kwargs: object) -> object:
        created.append((host, kwargs))
        return object()

    monkeypatch.setattr(proxmoxer, "ProxmoxAPI", factory)
    return created


def test_init_with_password_builds_proxmoxer_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _stub_proxmoxer(monkeypatch)
    client = ProxmoxClient(
        host="pve1",
        user="root@pam",
        password="pw",
        port=8007,
        verify_ssl=True,
    )
    assert isinstance(client._backend, ProxmoxerBackend)
    assert created == [
        (
            "pve1",
            {
                "user": "root@pam",
                "password": "pw",
                "verify_ssl": True,
                "port": 8007,
            },
        )
    ]


def test_init_with_api_token_omits_password(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _stub_proxmoxer(monkeypatch)
    ProxmoxClient(
        host="pve1",
        user="root@pam",
        token_name="sdk",
        token_value="sec",
        verify_ssl=True,
    )
    assert created == [
        (
            "pve1",
            {
                "user": "root@pam",
                "token_name": "sdk",
                "token_value": "sec",
                "verify_ssl": True,
                "port": 8006,
            },
        )
    ]


def test_init_with_incomplete_token_falls_back_to_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _stub_proxmoxer(monkeypatch)
    ProxmoxClient(host="pve1", user="root@pam", token_name="sdk", password="pw")
    assert created[0][1]["password"] == "pw"
    assert "token_name" not in created[0][1]


def test_init_without_password_sends_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _stub_proxmoxer(monkeypatch)
    ProxmoxClient(host="pve1", user="root@pam")
    assert created == [
        (
            "pve1",
            {"user": "root@pam", "password": "", "verify_ssl": False, "port": 8006},
        )
    ]


def test_missing_proxmoxer_raises_helpful_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "proxmoxer", None)
    with pytest.raises(ImportError, match="proxmoxer is required for the real backend"):
        ProxmoxClient(host="pve1", user="root@pam", password="pw")


def test_init_with_backend_ignores_connection_args(
    fake_backend: FakeBackend,
) -> None:
    client = ProxmoxClient(
        host="ignored",
        user="ignored",
        password="ignored",
        backend=fake_backend,
    )
    assert client._backend is fake_backend


def test_from_url_forwards_parsed_host_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _stub_proxmoxer(monkeypatch)
    client = ProxmoxClient.from_url(
        "https://192.168.1.5:8006/api2/json",
        user="root@pam",
        password="pw",
        verify_ssl=True,
        node="pve",
    )
    assert created == [
        (
            "192.168.1.5",
            {
                "user": "root@pam",
                "password": "pw",
                "verify_ssl": True,
                "port": 8006,
            },
        )
    ]
    assert repr(client) == "ProxmoxClient(host='192.168.1.5', user='root@pam')"
    assert client._node == "pve"


def test_from_url_defaults_port_when_url_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _stub_proxmoxer(monkeypatch)
    ProxmoxClient.from_url("proxmox-host", user="root@pam", password="pw")
    assert created[0][0] == "proxmox-host"
    assert created[0][1]["port"] == 8006


def test_repr_names_host_and_user_without_credentials(
    client: ProxmoxClient,
) -> None:
    text = repr(client)
    assert text == "ProxmoxClient(host='fake-host', user='root@pam')"
    assert "fake-password" not in text


# ---------------------------------------------------------------------------
# Node / VM-id discovery helpers
# ---------------------------------------------------------------------------


def test_list_without_node_returns_every_cluster_vm(
    fake_backend: FakeBackend,
) -> None:
    client = ProxmoxClient(host="x", user="x", backend=fake_backend)
    assert sorted(vm.vm_id for vm in client.list()) == [100, 101, 9000]


def test_list_filtered_to_unknown_node_returns_nothing(
    client: ProxmoxClient,
) -> None:
    assert client.list(node="other-node") == []


def test_launch_without_node_uses_first_cluster_node(
    fake_backend: FakeBackend,
) -> None:
    client = ProxmoxClient(host="x", user="x", backend=fake_backend)
    vm = client.launch("auto-node", template_id=9000, start=False)
    assert vm.node == "pve"
    fake_backend.assert_called_with("POST", "nodes/pve/qemu/9000/clone")


def test_launch_raises_when_cluster_has_no_nodes() -> None:
    client = ProxmoxClient(host="x", user="x", backend=FakeBackend())
    with pytest.raises(RuntimeError, match="No nodes available in the cluster"):
        client.launch("nowhere", template_id=9000, start=False)


class _NextIdBackend(FakeBackend):
    """FakeBackend that answers cluster/nextid like a real cluster does."""

    def get(self, path: str, **params: object) -> object:
        if path == "cluster/nextid":
            return "4242"
        return super().get(path, **params)


def test_launch_prefers_cluster_nextid_over_scanning() -> None:
    backend = _NextIdBackend()
    backend.add_vm(9000, node="pve", name="tmpl", status="stopped", template=True)
    client = ProxmoxClient(host="x", user="x", node="pve", backend=backend)

    vm = client.launch("nextid-vm", template_id=9000, start=False)

    assert vm.vm_id == 4242
    clone = next(d for m, p, d in backend.calls if m == "POST" and p.endswith("/clone"))
    assert clone["newid"] == 4242
    assert backend.get("nodes/pve/qemu/4242/config")["name"] == "nextid-vm"


# ---------------------------------------------------------------------------
# launch: VmConfig form and cloud-init guard
# ---------------------------------------------------------------------------


def test_launch_accepts_vm_config_object(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    cfg = VmConfig(name="cfg-vm", template_id=9000, cores=2, start=False)

    vm = client.launch(cfg)

    assert isinstance(vm, ProxmoxVM)
    assert vm.vm_id == 9001
    assert vm.node == "pve"
    clone = next(
        d for m, p, d in fake_backend.calls if m == "POST" and p.endswith("/clone")
    )
    assert clone == {"newid": 9001, "name": "cfg-vm", "target": "pve", "full": 1}
    assert fake_backend.get("nodes/pve/qemu/9001/config")["cores"] == 2


def test_launch_with_cloud_init_refuses_template_without_drive(
    fake_backend: FakeBackend,
) -> None:
    # A template whose config carries no cloud-init CD-ROM.
    fake_backend.add_vm(
        9001, node="pve", name="bare-template", status="stopped", template=True
    )
    client = ProxmoxClient(host="x", user="x", node="pve", backend=fake_backend)
    cfg = CloudInitConfig(username="ubuntu", ip_config="ip=dhcp")

    with pytest.raises(ProxmoxError) as exc_info:
        client.launch("ci-vm", template_id=9001, cloud_init_config=cfg)

    message = str(exc_info.value)
    assert "Template 9001" in message
    assert "no cloud-init drive" in message
    assert not any(
        m == "POST" and p.endswith("/clone") for m, p, _ in fake_backend.calls
    )


# ---------------------------------------------------------------------------
# launch_many
# ---------------------------------------------------------------------------


def test_launch_many_empty_returns_empty_list(client: ProxmoxClient) -> None:
    assert client.launch_many([]) == []


def test_launch_many_single_config_uses_default_worker_count(
    client: ProxmoxClient,
) -> None:
    vms = client.launch_many([VmConfig(name="solo", template_id=9000, start=False)])
    assert [vm.vm_id for vm in vms] == [9001]
    assert vms[0].node == "pve"


def test_launch_many_launches_every_config(client: ProxmoxClient) -> None:
    configs = [
        VmConfig(name="many-a", template_id=9000, start=False),
        VmConfig(name="many-b", template_id=9000, start=False),
    ]
    vms = client.launch_many(configs, max_workers=1)
    assert sorted(vm.vm_id for vm in vms) == [9001, 9002]
    assert all(vm.node == "pve" for vm in vms)


class _SlowFailCloneBackend(FakeBackend):
    """FakeBackend that delays its clone of template 9999 before failing.

    The delay keeps the failing future from being reported before the
    successful ones, so launch_many's rollback path is exercised
    deterministically.
    """

    def post(self, path: str, **data: object) -> object:
        if path.endswith("/qemu/9999/clone"):
            time.sleep(0.25)
        return super().post(path, **data)


def _rollback_backend(backend: FakeBackend) -> ProxmoxClient:
    backend.add_vm(
        9000,
        node="pve",
        name="tmpl",
        status="stopped",
        template=True,
        ide2="local-lvm:vm-9000-cloudinit,media=cdrom",
    )
    return ProxmoxClient(host="x", user="x", node="pve", backend=backend)


_ROLLBACK_CONFIGS = [
    VmConfig(name="good-1", template_id=9000, start=False),
    VmConfig(name="bad", template_id=9999, start=False),
]


def _cloned_vmid(backend: FakeBackend, name: str) -> int:
    clone = next(
        d
        for m, p, d in backend.calls
        if m == "POST" and p.endswith("/clone") and d.get("name") == name
    )
    return int(clone["newid"])


def test_launch_many_deletes_successful_vms_when_one_fails() -> None:
    backend = _SlowFailCloneBackend()
    client = _rollback_backend(backend)

    with pytest.raises(VmNotFoundError) as exc_info:
        client.launch_many(_ROLLBACK_CONFIGS, max_workers=1)

    assert exc_info.value.identifier == 9999
    good_vmid = _cloned_vmid(backend, "good-1")
    backend.assert_called_with("DELETE", f"nodes/pve/qemu/{good_vmid}")
    assert [vm.name for vm in client.list()] == ["tmpl"]


class _FailingDeleteBackend(_SlowFailCloneBackend):
    """Backend whose DELETE always fails, to test rollback error handling."""

    def __init__(self) -> None:
        super().__init__()
        self.delete_attempts: list[str] = []

    def delete(self, path: str, **params: object) -> object:
        self.delete_attempts.append(path)
        raise RuntimeError("delete boom")


def test_launch_many_rollback_failure_does_not_mask_original_error() -> None:
    backend = _FailingDeleteBackend()
    client = _rollback_backend(backend)

    with pytest.raises(VmNotFoundError) as exc_info:
        client.launch_many(_ROLLBACK_CONFIGS, max_workers=1)

    assert exc_info.value.identifier == 9999
    good_vmid = _cloned_vmid(backend, "good-1")
    assert backend.delete_attempts == [f"nodes/pve/qemu/{good_vmid}"]


class _SlowSuccessCloneBackend(FakeBackend):
    """FakeBackend whose clone of the good template is slow.

    Keeps a successful clone in flight while the failing config reports.
    """

    def post(self, path: str, **data: object) -> object:
        if path.endswith("/qemu/9000/clone"):
            time.sleep(0.4)
        return super().post(path, **data)


def test_launch_many_reports_first_error_while_clones_still_in_flight() -> None:
    backend = _SlowSuccessCloneBackend()
    client = _rollback_backend(backend)

    with pytest.raises(VmNotFoundError) as exc_info:
        client.launch_many(_ROLLBACK_CONFIGS, max_workers=3)

    assert exc_info.value.identifier == 9999
    # The good clone really was issued and still running when the error was
    # observed, so this exercises the "result arrives after the failure" path.
    assert _cloned_vmid(backend, "good-1") is not None


# ---------------------------------------------------------------------------
# ensure_running
# ---------------------------------------------------------------------------


def test_ensure_running_returns_running_vm_untouched(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.ensure_running("running-vm", template_id=9000)
    assert vm.vm_id == 101
    assert not any(
        m == "POST" and p.endswith("/status/start") for m, p, _ in fake_backend.calls
    )


def test_ensure_running_starts_stopped_vm(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.ensure_running("stopped-vm", template_id=9000)
    assert vm.vm_id == 100
    assert vm.info().state is VmState.RUNNING
    fake_backend.assert_called_with("POST", "nodes/pve/qemu/100/status/start")


def test_ensure_running_clones_missing_vm_with_options(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.ensure_running("brand-new", 9000, cores=4, memory_mb=2048)

    assert vm.vm_id == 9001
    assert vm.node == "pve"
    assert vm.info().state is VmState.RUNNING
    stored = fake_backend.get("nodes/pve/qemu/9001/config")
    assert stored["name"] == "brand-new"
    assert stored["cores"] == 4
    assert stored["memory"] == 2048
