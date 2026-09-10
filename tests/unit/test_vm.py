import time
from typing import Any

import pytest

from proxmox_sdk import (
    CloudInitConfig,
    CommandResult,
    FakeBackend,
    ProxmoxAPIError,
    ProxmoxClient,
    ProxmoxTimeoutError,
    ProxmoxVM,
    SnapshotNotFoundError,
    VmNotFoundError,
)
from proxmox_sdk.models import VmState


def test_info_returns_vm_info(client: ProxmoxClient) -> None:
    from proxmox_sdk import VmInfo

    vm = client.get_vm(100)
    info = vm.info()
    assert isinstance(info, VmInfo)
    assert info.vm_id == 100
    assert info.name == "stopped-vm"
    assert info.state == VmState.STOPPED


def test_start_transitions_to_running(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(100)
    vm.start()
    assert vm.info().state == VmState.RUNNING


def test_stop_transitions_to_stopped(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(101)
    vm.stop()
    assert vm.info().state == VmState.STOPPED


def test_restart_keeps_running(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(101)
    vm.restart()
    assert vm.info().state == VmState.RUNNING


def test_delete_removes_vm(client: ProxmoxClient, fake_backend: FakeBackend) -> None:
    vm = client.get_vm(100)
    vm.delete(purge=True)
    with pytest.raises(VmNotFoundError):
        client.get_vm(100)


def test_clone_creates_new_vm(client: ProxmoxClient, fake_backend: FakeBackend) -> None:
    vm = client.get_vm(100)
    cloned = vm.clone(200, "cloned-vm")
    assert cloned.vm_id == 200
    # New VM should be listed
    vms = client.list()
    assert any(v.vm_id == 200 for v in vms)


def test_snapshot_round_trip(client: ProxmoxClient, fake_backend: FakeBackend) -> None:
    vm = client.get_vm(100)
    snap = vm.snapshot("snap-1", description="test snapshot")
    assert snap.name == "snap-1"
    assert snap.vm_id == 100

    snaps = vm.list_snapshots()
    assert any(s.name == "snap-1" for s in snaps)


def test_restore_known_snapshot(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(100)
    vm.snapshot("before")
    vm.restore("before")  # should not raise


def test_restore_unknown_snapshot_raises(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(100)
    with pytest.raises(SnapshotNotFoundError):
        vm.restore("nonexistent")


def test_wait_for_ip_raises_timeout(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(100)
    with pytest.raises(ProxmoxTimeoutError) as exc_info:
        vm.wait_for_ip(timeout=0.05)
    assert exc_info.value.vm_id == 100
    assert exc_info.value.operation == "wait_for_ip"


def test_metrics_returns_vm_metrics(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    from proxmox_sdk import VmMetrics

    vm = client.get_vm(101)
    m = vm.metrics()
    assert isinstance(m, VmMetrics)
    assert m.vm_id == 101
    assert m.cpu_pct == pytest.approx(5.0, abs=0.1)
    assert m.mem_used_pct == pytest.approx(25.0, abs=0.1)


def test_resize_disk_calls_put(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(100)
    vm.resize_disk("scsi0", "+10G")
    fake_backend.assert_called_with("PUT", "nodes/pve/qemu/100/resize")


# ---------------------------------------------------------------------------
# Test doubles: FakeBackend omits the QEMU guest-agent routes and the
# cluster.resources fallback, so these subclasses add exactly those routes.
# ---------------------------------------------------------------------------


class AgentBackend(FakeBackend):
    """FakeBackend plus the QEMU guest-agent routes it does not model."""

    def __init__(self) -> None:
        super().__init__()
        self.agent_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.waits: list[tuple[str, str, float]] = []
        self.exec_status: list[dict[str, Any]] = []
        self.agent_down: Exception | None = None
        self.interfaces: dict[str, Any] = {"result": []}

    def wait_for_task(self, node: str, upid: str, timeout: float = 60) -> None:
        self.waits.append((node, upid, timeout))

    def get(self, path: str, **params: Any) -> Any:
        if path.endswith("/agent/exec-status"):
            self.agent_calls.append(("GET", path, dict(params)))
            return self.exec_status.pop(0) if self.exec_status else {}
        if path.endswith("/agent/network-get-interfaces"):
            self.agent_calls.append(("GET", path, dict(params)))
            if self.agent_down is not None:
                raise self.agent_down
            return self.interfaces
        return super().get(path, **params)

    def post(self, path: str, **data: Any) -> Any:
        if path.endswith("/agent/ping"):
            self.agent_calls.append(("POST", path, dict(data)))
            if self.agent_down is not None:
                raise self.agent_down
            return {}
        if path.endswith("/agent/exec"):
            self.agent_calls.append(("POST", path, dict(data)))
            return {"pid": 4321}
        return super().post(path, **data)


class ListingBackend(FakeBackend):
    """FakeBackend whose snapshot listing and cluster resources are canned."""

    def __init__(self, snapshots: list[dict[str, Any]], resources: Any = None) -> None:
        super().__init__()
        self.snapshots = snapshots
        self.resources = resources

    def get(self, path: str, **params: Any) -> Any:
        if path == "cluster/resources" and self.resources is not None:
            return self.resources
        if path.endswith("/snapshots"):
            self._record("GET", path, params)
            return list(self.snapshots)
        return super().get(path, **params)

    def _record(self, method: str, path: str, data: dict[str, Any]) -> None:
        self._calls.append((method, path, data))


def agent_vm(
    backend: FakeBackend, vm_id: int = 100, status: str = "running"
) -> ProxmoxVM:
    backend.add_vm(vm_id, node="pve", name=f"vm-{vm_id}", status=status)
    return ProxmoxVM(vm_id, "pve", backend)


# ---------------------------------------------------------------------------
# metrics fallback
# ---------------------------------------------------------------------------


def test_metrics_falls_back_to_zeroes_when_vm_absent_from_cluster() -> None:
    backend = ListingBackend(snapshots=[], resources=[])
    vm = agent_vm(backend, 100)

    m = vm.metrics()

    assert m.vm_id == 100
    assert m.cpu_pct == 0.0
    assert m.mem_used_bytes == 0
    assert m.mem_total_bytes == 0
    assert m.mem_used_pct == 0.0
    assert m.net_in_bytes == 0
    assert m.net_out_bytes == 0
    assert m.disk_read_bytes == 0
    assert m.disk_write_bytes == 0


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_shutdown_sends_acpi_and_stops_vm(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(101)
    vm.shutdown()
    assert vm.info().state == VmState.STOPPED
    fake_backend.assert_called_with("POST", "nodes/pve/qemu/101/status/shutdown")


def test_stop_forwards_timeout_to_task_wait() -> None:
    backend = AgentBackend()
    vm = agent_vm(backend, 101)

    vm.stop(timeout=7)

    assert [(node, timeout) for node, _, timeout in backend.waits] == [("pve", 7.0)]
    assert backend.waits[0][1].startswith("UPID:pve:")


def test_delete_without_purge_sends_no_purge_param(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    client.get_vm(100).delete()
    assert [c for c in fake_backend.calls if c[0] == "DELETE"] == [
        ("DELETE", "nodes/pve/qemu/100", {})
    ]


def test_delete_with_purge_sends_purge_flag(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    client.get_vm(100).delete(purge=True)
    assert [c for c in fake_backend.calls if c[0] == "DELETE"] == [
        ("DELETE", "nodes/pve/qemu/100", {"purge": 1})
    ]


# ---------------------------------------------------------------------------
# clone
# ---------------------------------------------------------------------------


def test_clone_defaults_to_source_node_and_full_copy(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(100)

    cloned = vm.clone(200, "cloned-vm")

    assert [c for c in fake_backend.calls if c[1].endswith("/clone")] == [
        (
            "POST",
            "nodes/pve/qemu/100/clone",
            {"newid": 200, "name": "cloned-vm", "target": "pve", "full": 1},
        )
    ]
    assert cloned.vm_id == 200
    assert cloned.node == "pve"


def test_clone_to_other_node_with_linked_clone(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(100)

    cloned = vm.clone(201, "linked", node="pve2", full=False)

    assert [c for c in fake_backend.calls if c[1].endswith("/clone")] == [
        (
            "POST",
            "nodes/pve/qemu/100/clone",
            {"newid": 201, "name": "linked", "target": "pve2", "full": 0},
        )
    ]
    assert cloned.node == "pve2"
    assert cloned.info().node == "pve2"
    assert cloned.info().name == "linked"


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


def test_snapshot_default_description_is_empty(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    snap = client.get_vm(100).snapshot("plain")
    assert snap.name == "plain"
    assert snap.description == ""


def test_list_snapshots_skips_the_current_pseudo_snapshot() -> None:
    backend = ListingBackend(
        snapshots=[
            {"name": "current", "snaptime": 999},
            {
                "name": "base",
                "description": "first",
                "snaptime": 1234,
                "parent": None,
            },
        ]
    )
    vm = agent_vm(backend, 100)

    snaps = vm.list_snapshots()

    assert [s.name for s in snaps] == ["base"]
    assert snaps[0].vm_id == 100
    assert snaps[0].created == 1234
    assert snaps[0].description == "first"
    assert snaps[0].parent is None


def test_snapshot_returns_the_matching_snapshot_among_several(
    client: ProxmoxClient,
) -> None:
    vm = client.get_vm(100)

    first = vm.snapshot("first")
    second = vm.snapshot("second")

    assert first.name == "first"
    assert second.name == "second"
    assert second.parent == "first"
    assert [s.name for s in vm.list_snapshots()] == ["first", "second"]


def test_snapshot_falls_back_when_listing_lacks_the_new_name() -> None:
    backend = ListingBackend(snapshots=[])
    vm = agent_vm(backend, 100)
    before = int(time.time())

    snap = vm.snapshot("ghost", description="not listed")

    after = int(time.time())
    assert snap.name == "ghost"
    assert snap.vm_id == 100
    assert snap.description == "not listed"
    assert before <= snap.created <= after


def test_restore_sends_rollback_task(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(100)
    vm.snapshot("before")

    vm.restore("before")

    fake_backend.assert_called_with(
        "POST", "nodes/pve/qemu/100/snapshots/before/rollback"
    )


def test_restore_unknown_snapshot_message_names_vm_and_snapshot(
    client: ProxmoxClient,
) -> None:
    vm = client.get_vm(101)
    with pytest.raises(SnapshotNotFoundError) as exc_info:
        vm.restore("nope")
    assert exc_info.value.vm_id == 101
    assert exc_info.value.snapshot == "nope"
    assert str(exc_info.value) == "Snapshot 'nope' not found on VM 101"


# ---------------------------------------------------------------------------
# guest-agent command execution
# ---------------------------------------------------------------------------


def test_exec_returns_command_result_with_exit_code_and_streams() -> None:
    backend = AgentBackend()
    backend.exec_status = [
        {"exited": 1, "exitcode": 7, "out-data": "out\n", "err-data": "err\n"}
    ]
    vm = agent_vm(backend, 100)

    result = vm.exec(["echo", "hi"], timeout=5.0)

    assert isinstance(result, CommandResult)
    assert result.exit_code == 7
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.success is False
    assert backend.agent_calls == [
        ("POST", "nodes/pve/qemu/100/agent/exec", {"command": ["echo", "hi"]}),
        ("GET", "nodes/pve/qemu/100/agent/exec-status", {"pid": 4321}),
    ]


def test_exec_raises_api_error_with_path_and_message() -> None:
    backend = AgentBackend()
    backend.exec_status = [{"error": "no such process"}]
    vm = agent_vm(backend, 100)

    with pytest.raises(ProxmoxAPIError) as exc_info:
        vm.exec(["false"], timeout=5.0)

    assert exc_info.value.status_code == 500
    assert exc_info.value.message == "no such process"
    assert exc_info.value.path == "nodes/pve/qemu/100/agent/exec-status"


def test_exec_times_out_when_status_never_exits() -> None:
    backend = AgentBackend()
    backend.exec_status = [{"pid": 4321}]
    vm = agent_vm(backend, 100)

    with pytest.raises(ProxmoxTimeoutError) as exc_info:
        vm.exec(["sleep", "1"], timeout=0.05)

    assert exc_info.value.vm_id == 100
    assert exc_info.value.operation == "exec"
    assert exc_info.value.timeout == 0.05


def test_exec_structured_quotes_cwd_env_and_argv() -> None:
    backend = AgentBackend()
    backend.exec_status = [{"exited": 1, "exitcode": 0, "out-data": "ok"}]
    vm = agent_vm(backend, 100)

    vm.exec_structured(
        ["echo", "hello world"],
        env={"GREETING": "hi there"},
        cwd="/tmp/my dir",
    )

    command = backend.agent_calls[0][2]["command"]
    assert command == [
        "bash",
        "-lc",
        "cd '/tmp/my dir' && export GREETING='hi there' && echo 'hello world'",
    ]


def test_exec_structured_without_env_or_cwd_is_plain_argv() -> None:
    backend = AgentBackend()
    backend.exec_status = [{"exited": 1, "exitcode": 0}]
    vm = agent_vm(backend, 100)

    vm.exec_structured(["ls", "-la"])

    assert backend.agent_calls[0][2]["command"] == ["bash", "-lc", "ls -la"]


def test_transfer_raises_not_implemented_naming_ssh_and_routing(
    client: ProxmoxClient,
) -> None:
    vm = client.get_vm(100)
    with pytest.raises(NotImplementedError) as exc_info:
        vm.transfer("/local/file", "/remote/file")
    message = str(exc_info.value)
    assert "SSH connection to the VM" in message
    assert "ProxmoxRoutingManager" in message


# ---------------------------------------------------------------------------
# wait helpers
# ---------------------------------------------------------------------------


def test_wait_for_agent_returns_after_one_successful_ping() -> None:
    backend = AgentBackend()
    vm = agent_vm(backend, 101)

    assert vm.wait_for_agent(timeout=5.0, interval=0.01) is None
    assert backend.agent_calls == [("POST", "nodes/pve/qemu/101/agent/ping", {})]


def test_wait_for_agent_swallows_errors_until_timeout() -> None:
    backend = AgentBackend()
    backend.agent_down = KeyError("agent not responding")
    vm = agent_vm(backend, 101)

    with pytest.raises(ProxmoxTimeoutError) as exc_info:
        vm.wait_for_agent(timeout=0.05, interval=0.01)

    assert exc_info.value.operation == "wait_for_agent"
    assert exc_info.value.vm_id == 101
    assert len(backend.agent_calls) > 1


def test_wait_for_ip_returns_first_non_loopback_ipv4() -> None:
    backend = AgentBackend()
    backend.interfaces = {
        "result": [
            {
                "name": "lo",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}
                ],
            },
            {
                "name": "eth0",
                "ip-addresses": [
                    {"ip-address-type": "ipv6", "ip-address": "fe80::1"},
                    {"ip-address-type": "ipv4", "ip-address": "10.0.0.5"},
                ],
            },
        ]
    }
    vm = agent_vm(backend, 101)

    assert vm.wait_for_ip(timeout=5.0, interval=0.01) == "10.0.0.5"
    assert backend.agent_calls == [
        (
            "GET",
            "nodes/pve/qemu/101/agent/network-get-interfaces",
            {},
        )
    ]


def test_wait_for_ip_continues_past_an_interface_without_ipv4() -> None:
    backend = AgentBackend()
    backend.interfaces = {
        "result": [
            {
                "name": "eth0",
                "ip-addresses": [{"ip-address-type": "ipv6", "ip-address": "fe80::1"}],
            },
            {
                "name": "eth1",
                "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.0.0.6"}],
            },
        ]
    }
    vm = agent_vm(backend, 101)

    assert vm.wait_for_ip(timeout=5.0, interval=0.01) == "10.0.0.6"


def test_wait_for_ip_skips_lo0_and_times_out() -> None:
    backend = AgentBackend()
    backend.interfaces = {
        "result": [
            {
                "name": "lo0",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}
                ],
            }
        ]
    }
    vm = agent_vm(backend, 101)

    with pytest.raises(ProxmoxTimeoutError) as exc_info:
        vm.wait_for_ip(timeout=0.05, interval=0.01)

    assert exc_info.value.operation == "wait_for_ip"
    assert exc_info.value.vm_id == 101


def test_wait_ready_returns_when_running_and_agent_answers() -> None:
    backend = AgentBackend()
    vm = agent_vm(backend, 101, status="running")

    assert vm.wait_ready(timeout=5.0, interval=0.01) is None
    assert ("GET", "nodes/pve/qemu/101/status/current", {}) in backend.calls
    assert ("POST", "nodes/pve/qemu/101/agent/ping", {}) in backend.agent_calls


def test_wait_ready_times_out_while_vm_stays_stopped() -> None:
    backend = AgentBackend()
    vm = agent_vm(backend, 100, status="stopped")

    with pytest.raises(ProxmoxTimeoutError) as exc_info:
        vm.wait_ready(timeout=0.05, interval=0.01)

    assert exc_info.value.operation == "wait_ready"
    assert exc_info.value.vm_id == 100
    assert backend.agent_calls == []


# ---------------------------------------------------------------------------
# disk & cloud-init
# ---------------------------------------------------------------------------


def test_resize_disk_sends_delta_and_does_not_wait_for_a_task(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    client.get_vm(100).resize_disk("scsi0", "+10G")
    puts = [c for c in fake_backend.calls if c[0] == "PUT"]
    assert puts == [
        ("PUT", "nodes/pve/qemu/100/resize", {"disk": "scsi0", "size": "+10G"})
    ]


def test_has_cloud_init_drive_true_for_template_with_cloudinit_cdrom(
    client: ProxmoxClient,
) -> None:
    assert client.get_vm(9000).has_cloud_init_drive() is True


def test_has_cloud_init_drive_false_for_plain_vm(client: ProxmoxClient) -> None:
    assert client.get_vm(100).has_cloud_init_drive() is False


def test_configure_cloud_init_puts_url_encoded_params(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    vm = client.get_vm(100)

    vm.configure_cloud_init(
        CloudInitConfig(
            username="ubuntu",
            ssh_keys=["ssh-rsa AAAA"],
            ip_config="ip=dhcp",
            nameserver="8.8.8.8",
        )
    )

    puts = [c for c in fake_backend.calls if c[0] == "PUT"]
    assert puts == [
        (
            "PUT",
            "nodes/pve/qemu/100/config",
            {
                "ciuser": "ubuntu",
                "sshkeys": "ssh-rsa%20AAAA",
                "ipconfig0": "ip=dhcp",
                "nameserver": "8.8.8.8",
            },
        )
    ]


def test_configure_cloud_init_with_no_fields_makes_no_call(
    client: ProxmoxClient, fake_backend: FakeBackend
) -> None:
    client.get_vm(100).configure_cloud_init(CloudInitConfig())
    assert [c for c in fake_backend.calls if c[0] == "PUT"] == []


def test_repr_names_vm_and_node(client: ProxmoxClient) -> None:
    assert repr(client.get_vm(100)) == "ProxmoxVM(vm_id=100, node='pve')"
