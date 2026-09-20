"""Behavioural tests for the in-memory test doubles in proxmox_vm_sdk.testing."""

import pytest

from proxmox_vm_sdk import VmNotFoundError
from proxmox_vm_sdk.testing import FakeBackend, FakeSshBackend

# ---------------------------------------------------------------------------
# FakeBackend seeding helpers
# ---------------------------------------------------------------------------


def test_add_vm_generates_default_name_and_fields() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    vm = fb.get("nodes/pve/qemu/100/status/current")
    assert vm == {
        "vmid": 100,
        "node": "pve",
        "name": "vm-100",
        "status": "stopped",
        "cpus": 2,
        "maxmem": 2 * 1024 * 1024 * 1024,
        "mem": 512 * 1024 * 1024,
        "cpu": 0.01,
        "uptime": 0,
        "template": False,
        "netin": 0,
        "netout": 0,
        "diskread": 0,
        "diskwrite": 0,
    }


def test_add_vm_extra_kwargs_override_generated_fields() -> None:
    fb = FakeBackend()
    fb.add_vm(9000, status="running", cpus=16, template=True, netin=42)
    vm = fb.get("nodes/pve/qemu/9000/status/current")
    assert vm["status"] == "running"
    assert vm["cpus"] == 16
    assert vm["template"] is True
    assert vm["netin"] == 42


def test_add_vm_creates_its_node_once() -> None:
    fb = FakeBackend()
    fb.add_vm(100, node="pve")
    fb.add_vm(101, node="pve")
    fb.add_vm(102, node="pve2")
    nodes = fb.get("nodes")
    assert [n["node"] for n in nodes] == ["pve", "pve2"]
    assert nodes[0] == {
        "node": "pve",
        "status": "online",
        "maxcpu": 8,
        "maxmem": 16 * 1024 * 1024 * 1024,
        "mem": 4 * 1024 * 1024 * 1024,
        "uptime": 86400,
    }


def test_add_node_seeds_online_node_with_overrides() -> None:
    fb = FakeBackend()
    fb.add_node("pve3")
    fb.add_node("pve4", maxcpu=64, status="offline")
    nodes = {n["node"]: n for n in fb.get("nodes")}
    assert nodes["pve3"]["status"] == "online"
    assert nodes["pve3"]["maxcpu"] == 8
    assert nodes["pve4"]["maxcpu"] == 64
    assert nodes["pve4"]["status"] == "offline"


# ---------------------------------------------------------------------------
# Route dispatch: GET
# ---------------------------------------------------------------------------


def test_cluster_resources_type_vm_returns_only_vms() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.add_node("other")
    result = fb.get("cluster/resources", type="vm")
    assert [r["vmid"] for r in result] == [100]


def test_cluster_resources_type_node_returns_only_nodes() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.add_node("other")
    result = fb.get("cluster/resources", type="node")
    assert [r["node"] for r in result] == ["pve", "other"]


def test_cluster_resources_without_type_returns_vms_and_nodes() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    entries = fb.get("cluster/resources")
    assert len(entries) == 2
    assert {e["node"] for e in entries} == {"pve"}


def test_get_qemu_listing_is_filtered_by_node() -> None:
    fb = FakeBackend()
    fb.add_vm(100, node="pve")
    fb.add_vm(200, node="pve2")
    assert [v["vmid"] for v in fb.get("nodes/pve/qemu")] == [100]
    assert [v["vmid"] for v in fb.get("nodes/pve2/qemu")] == [200]
    assert fb.get("nodes/pve3/qemu") == []


def test_get_snapshots_defaults_to_empty_list() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    assert fb.get("nodes/pve/qemu/100/snapshots") == []


def test_get_agent_interfaces_returns_empty_result() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    assert fb.get("nodes/pve/qemu/100/agent/network-get-interfaces") == {"result": []}


def test_get_status_current_unknown_vm_raises_key_error() -> None:
    fb = FakeBackend()
    with pytest.raises(KeyError) as excinfo:
        fb.get("nodes/pve/qemu/999/status/current")
    assert str(excinfo.value) == "'VM 999 not found'"


def test_get_config_unknown_vm_raises_key_error() -> None:
    fb = FakeBackend()
    with pytest.raises(KeyError) as excinfo:
        fb.get("nodes/pve/qemu/999/config")
    assert str(excinfo.value) == "'VM 999 not found'"


def test_get_agent_interfaces_unknown_vm_raises_key_error() -> None:
    fb = FakeBackend()
    with pytest.raises(KeyError) as excinfo:
        fb.get("nodes/pve/qemu/999/agent/network-get-interfaces")
    assert str(excinfo.value) == "'VM 999 not found'"


def test_unhandled_get_path_raises_key_error_naming_the_path() -> None:
    fb = FakeBackend()
    with pytest.raises(KeyError) as excinfo:
        fb.get("cluster/status")
    assert str(excinfo.value) == (
        "\"FakeBackend: unhandled GET path: 'cluster/status'\""
    )


# ---------------------------------------------------------------------------
# Route dispatch: POST
# ---------------------------------------------------------------------------


def test_post_creates_vm_with_explicit_vmid_and_returns_upid() -> None:
    fb = FakeBackend()
    upid = fb.post("nodes/pve2/qemu", vmid=400, name="created")
    assert upid == "UPID:pve2:fake-0001"
    assert fb.calls == [("POST", "nodes/pve2/qemu", {"vmid": 400, "name": "created"})]
    vm = fb.get("nodes/pve2/qemu/400/status/current")
    assert vm["name"] == "created"
    assert vm["node"] == "pve2"
    assert vm["status"] == "stopped"


def test_post_creates_vm_with_generated_vmid_and_name() -> None:
    fb = FakeBackend()
    fb.post("nodes/pve/qemu")
    vm = fb.get("nodes/pve/qemu/9000/status/current")
    assert vm["name"] == "vm-9000"


def test_post_start_and_stop_update_uptime() -> None:
    fb = FakeBackend()
    fb.add_vm(100, status="stopped")
    fb.post("nodes/pve/qemu/100/status/start")
    running = fb.get("nodes/pve/qemu/100/status/current")
    assert running["status"] == "running"
    assert running["uptime"] == 1

    fb.post("nodes/pve/qemu/100/status/stop")
    stopped = fb.get("nodes/pve/qemu/100/status/current")
    assert stopped["status"] == "stopped"
    assert stopped["uptime"] == 0


def test_post_shutdown_stops_the_vm() -> None:
    fb = FakeBackend()
    fb.add_vm(100, status="running", uptime=500)
    fb.post("nodes/pve/qemu/100/status/shutdown")
    vm = fb.get("nodes/pve/qemu/100/status/current")
    assert vm["status"] == "stopped"
    assert vm["uptime"] == 0


def test_post_reboot_sets_status_running() -> None:
    fb = FakeBackend()
    fb.add_vm(100, status="stopped")
    upid = fb.post("nodes/pve/qemu/100/status/reboot")
    assert upid == "UPID:pve:fake-0001"
    assert fb.get("nodes/pve/qemu/100/status/current")["status"] == "running"


def test_post_power_operation_unknown_vm_raises_vm_not_found() -> None:
    fb = FakeBackend()
    with pytest.raises(VmNotFoundError) as excinfo:
        fb.post("nodes/pve/qemu/999/status/start")
    assert excinfo.value.identifier == 999


def test_post_clone_uses_target_node_and_name() -> None:
    fb = FakeBackend()
    fb.add_vm(9000, node="pve", name="template")
    upid = fb.post(
        "nodes/pve/qemu/9000/clone", newid=200, name="clone-a", target="pve2"
    )
    assert upid == "UPID:pve:fake-0001"
    clone = fb.get("nodes/pve2/qemu/200/status/current")
    assert clone["name"] == "clone-a"
    assert clone["node"] == "pve2"
    assert clone["status"] == "stopped"


def test_post_clone_defaults_to_source_node_and_generated_name() -> None:
    fb = FakeBackend()
    fb.add_vm(9000, node="pve")
    fb.post("nodes/pve/qemu/9000/clone", newid=200)
    clone = fb.get("nodes/pve/qemu/200/status/current")
    assert clone["name"] == "vm-200"
    assert clone["node"] == "pve"


def test_post_clone_unknown_source_raises_vm_not_found() -> None:
    fb = FakeBackend()
    with pytest.raises(VmNotFoundError) as excinfo:
        fb.post("nodes/pve/qemu/999/clone", newid=200)
    assert excinfo.value.identifier == 999


def test_post_snapshot_uses_defaults_and_chains_parent() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.post("nodes/pve/qemu/100/snapshots")
    fb.post("nodes/pve/qemu/100/snapshots", snapname="snap-2", description="second")
    snaps = fb.get("nodes/pve/qemu/100/snapshots")
    assert [s["name"] for s in snaps] == ["snap", "snap-2"]
    assert snaps[0]["description"] == ""
    assert snaps[0]["parent"] is None
    assert snaps[1]["description"] == "second"
    assert snaps[1]["parent"] == "snap"
    assert isinstance(snaps[1]["snaptime"], int)


def test_post_snapshot_unknown_vm_raises_vm_not_found() -> None:
    fb = FakeBackend()
    with pytest.raises(VmNotFoundError) as excinfo:
        fb.post("nodes/pve/qemu/999/snapshots", snapname="s")
    assert excinfo.value.identifier == 999


def test_post_snapshot_rollback_returns_upid() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.post("nodes/pve/qemu/100/snapshots", snapname="snap-1")
    upid = fb.post("nodes/pve/qemu/100/snapshots/snap-1/rollback")
    assert upid == "UPID:pve:fake-0002"


def test_post_snapshot_rollback_unknown_vm_raises_vm_not_found() -> None:
    fb = FakeBackend()
    with pytest.raises(VmNotFoundError) as excinfo:
        fb.post("nodes/pve/qemu/999/snapshots/snap-1/rollback")
    assert excinfo.value.identifier == 999


def test_unhandled_post_path_raises_key_error_naming_the_path() -> None:
    fb = FakeBackend()
    with pytest.raises(KeyError) as excinfo:
        fb.post("nodes/pve/qemu/100/migrate")
    assert str(excinfo.value) == (
        "\"FakeBackend: unhandled POST path: 'nodes/pve/qemu/100/migrate'\""
    )


# ---------------------------------------------------------------------------
# Route dispatch: PUT / DELETE
# ---------------------------------------------------------------------------


def test_put_config_updates_vm_fields_and_returns_none() -> None:
    fb = FakeBackend()
    fb.add_vm(100, cpus=2)
    assert fb.put("nodes/pve/qemu/100/config", cores=8, memory=4096) is None
    vm = fb.get("nodes/pve/qemu/100/config")
    assert vm["cores"] == 8
    assert vm["memory"] == 4096
    assert vm["cpus"] == 2


def test_put_resize_returns_none_for_known_vm() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    assert fb.put("nodes/pve/qemu/100/resize", disk="scsi0", size="20G") is None


def test_put_unknown_vm_raises_vm_not_found() -> None:
    fb = FakeBackend()
    with pytest.raises(VmNotFoundError) as excinfo:
        fb.put("nodes/pve/qemu/999/resize", disk="scsi0", size="20G")
    assert excinfo.value.identifier == 999


def test_unhandled_put_path_raises_key_error_naming_the_path() -> None:
    fb = FakeBackend()
    with pytest.raises(KeyError) as excinfo:
        fb.put("nodes/pve/qemu/100/status")
    assert str(excinfo.value) == (
        "\"FakeBackend: unhandled PUT path: 'nodes/pve/qemu/100/status'\""
    )


def test_delete_vm_removes_it_and_its_snapshots() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.add_vm(101)
    fb.post("nodes/pve/qemu/100/snapshots", snapname="snap-1")
    upid = fb.delete("nodes/pve/qemu/100")
    assert upid == "UPID:pve:fake-0002"
    with pytest.raises(KeyError):
        fb.get("nodes/pve/qemu/100/status/current")
    assert fb.get("nodes/pve/qemu/100/snapshots") == []
    assert fb.get("nodes/pve/qemu/101/status/current")["vmid"] == 101


def test_delete_unknown_vm_raises_vm_not_found() -> None:
    fb = FakeBackend()
    with pytest.raises(VmNotFoundError) as excinfo:
        fb.delete("nodes/pve/qemu/999")
    assert excinfo.value.identifier == 999


def test_delete_snapshot_removes_only_that_snapshot() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.post("nodes/pve/qemu/100/snapshots", snapname="snap-1")
    fb.post("nodes/pve/qemu/100/snapshots", snapname="snap-2")
    fb.delete("nodes/pve/qemu/100/snapshots/snap-1")
    assert [s["name"] for s in fb.get("nodes/pve/qemu/100/snapshots")] == ["snap-2"]


def test_delete_unknown_snapshot_leaves_others_untouched() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.post("nodes/pve/qemu/100/snapshots", snapname="snap-1")
    fb.delete("nodes/pve/qemu/100/snapshots/missing")
    assert [s["name"] for s in fb.get("nodes/pve/qemu/100/snapshots")] == ["snap-1"]


def test_unhandled_delete_path_raises_key_error_naming_the_path() -> None:
    fb = FakeBackend()
    with pytest.raises(KeyError) as excinfo:
        fb.delete("nodes/pve/qemu/100/status")
    assert str(excinfo.value) == (
        "\"FakeBackend: unhandled DELETE path: 'nodes/pve/qemu/100/status'\""
    )


# ---------------------------------------------------------------------------
# Call recording / task waiting
# ---------------------------------------------------------------------------


def test_calls_property_returns_a_copy() -> None:
    fb = FakeBackend()
    fb.get("nodes")
    snapshot = fb.calls
    snapshot.append(("GET", "tampered", {}))
    assert fb.calls == [("GET", "nodes", {})]


def test_assert_called_with_matches_method_and_path_only() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.get("nodes")
    fb.post("nodes/pve/qemu/100/status/start", timeout=30)
    assert fb.assert_called_with("POST", "nodes/pve/qemu/100/status/start") is None
    assert fb.assert_called_with("GET", "nodes") is None


def test_assert_called_with_raises_with_expected_and_recorded_calls() -> None:
    fb = FakeBackend()
    fb.get("nodes")
    with pytest.raises(AssertionError) as excinfo:
        fb.assert_called_with("POST", "nodes/pve/qemu/100/status/start")
    assert str(excinfo.value) == (
        "Expected POST 'nodes/pve/qemu/100/status/start' "
        "but got: [('GET', 'nodes', {})]"
    )


def test_wait_for_task_returns_none_without_recording_a_call() -> None:
    fb = FakeBackend()
    assert fb.wait_for_task("pve", "UPID:pve:fake-0001", timeout=5) is None
    assert fb.calls == []


# ---------------------------------------------------------------------------
# FakeSshBackend
# ---------------------------------------------------------------------------


def test_ssh_run_defaults_to_success_with_no_output() -> None:
    ssh = FakeSshBackend()
    assert ssh.run("ls /") == (0, "", "")
    assert ssh.commands == ["ls /"]


def test_ssh_seed_response_returns_code_stdout_and_stderr() -> None:
    ssh = FakeSshBackend()
    ssh.seed_response("pvesh get", 1, "out", "err")
    assert ssh.run("pvesh get /nodes --output-format json") == (1, "out", "err")


def test_ssh_seed_response_stderr_defaults_to_empty() -> None:
    ssh = FakeSshBackend()
    ssh.seed_response("hostname", 0, "pve\n")
    assert ssh.run("hostname -f") == (0, "pve\n", "")


def test_ssh_first_matching_prefix_wins() -> None:
    ssh = FakeSshBackend()
    ssh.seed_response("pvesh get", 0, "specific")
    ssh.seed_response("pvesh", 1, "generic")
    assert ssh.run("pvesh get /nodes") == (0, "specific", "")
    assert ssh.run("pvesh set /nodes") == (1, "generic", "")


def test_ssh_file_round_trip_and_missing_file() -> None:
    ssh = FakeSshBackend()
    assert ssh.read_file("/etc/network/interfaces") == ""
    ssh.seed_file("/etc/hostname", "pve\n")
    assert ssh.read_file("/etc/hostname") == "pve\n"
    ssh.write_file("/etc/network/interfaces", "auto lo\n")
    assert ssh.read_file("/etc/network/interfaces") == "auto lo\n"
    assert ssh.read_file("/etc/hostname") == "pve\n"


def test_ssh_assert_ran_passes_on_substring_match() -> None:
    ssh = FakeSshBackend()
    ssh.run("iptables -t nat -L PREROUTING")
    assert ssh.assert_ran("iptables -t nat") is None


def test_ssh_assert_ran_failure_names_substring_and_commands() -> None:
    ssh = FakeSshBackend()
    ssh.run("hostname")
    with pytest.raises(AssertionError) as excinfo:
        ssh.assert_ran("ifreload --all")
    assert str(excinfo.value) == (
        "Expected a command containing 'ifreload --all'. Ran: ['hostname']"
    )
