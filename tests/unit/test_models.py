import pytest

from proxmox_vm_sdk._backend import CommandResult
from proxmox_vm_sdk.models import (
    CloudInitConfig,
    NodeInfo,
    SnapshotInfo,
    TaskInfo,
    TemplateInfo,
    VmConfig,
    VmInfo,
    VmMetrics,
    VmState,
)

# ------------------------------------------------------------------
# VmState
# ------------------------------------------------------------------


def test_vm_state_known_values() -> None:
    assert VmState("running") == VmState.RUNNING
    assert VmState("stopped") == VmState.STOPPED


def test_vm_state_unknown_falls_back() -> None:
    assert VmState("weird-state") == VmState.UNKNOWN


def test_vm_state_all_members_and_values() -> None:
    assert {m.name: m.value for m in VmState} == {
        "RUNNING": "running",
        "STOPPED": "stopped",
        "PAUSED": "paused",
        "SUSPENDED": "suspended",
        "UNKNOWN": "unknown",
    }
    assert VmState("paused") == VmState.PAUSED
    assert VmState("suspended") == VmState.SUSPENDED


def test_vm_state_missing_handles_non_string() -> None:
    # _missing_ must swallow any unhashable/odd value rather than raise.
    assert VmState(42) == VmState.UNKNOWN
    assert VmState(None) == VmState.UNKNOWN


# ------------------------------------------------------------------
# VmInfo
# ------------------------------------------------------------------


def test_vm_info_from_api_maps_fields() -> None:
    raw = {
        "vmid": 100,
        "name": "my-vm",
        "node": "pve",
        "status": "running",
        "cpus": 4,
        "maxmem": 4 * 1024 * 1024 * 1024,
        "uptime": 7200,
        "template": False,
    }
    info = VmInfo.from_api(raw)
    assert info.vm_id == 100
    assert info.name == "my-vm"
    assert info.node == "pve"
    assert info.state == VmState.RUNNING
    assert info.cpu_count == 4
    assert info.memory_mb == 4096
    assert info.uptime_seconds == 7200
    assert info.template is False


def test_vm_info_from_api_prefers_configured_cpu_count() -> None:
    raw = {
        "vmid": 100,
        "name": "my-vm",
        "node": "pve",
        "status": "running",
        "cores": 2,
        "maxcpu": 8,
        "cpus": 4,
        "maxmem": 4 * 1024 * 1024 * 1024,
        "uptime": 7200,
        "template": False,
    }
    info = VmInfo.from_api(raw)
    assert info.cpu_count == 2


def test_vm_info_tags_parsed() -> None:
    raw = {
        "vmid": 1,
        "name": "x",
        "node": "pve",
        "status": "stopped",
        "cpus": 1,
        "maxmem": 1024 * 1024 * 1024,
        "uptime": 0,
        "tags": "k3s;docker",
    }
    info = VmInfo.from_api(raw)
    assert info.tags == ["k3s", "docker"]


def test_vm_info_tags_strips_whitespace_and_drops_empty() -> None:
    raw = {"tags": " k3s ; docker ;; fio "}
    assert VmInfo.from_api(raw).tags == ["k3s", "docker", "fio"]


def test_vm_info_tags_absent_and_null_yield_empty_list() -> None:
    assert VmInfo.from_api({}).tags == []
    assert VmInfo.from_api({"tags": None}).tags == []


def test_vm_info_cpu_count_falls_back_to_maxcpu() -> None:
    assert VmInfo.from_api({"maxcpu": 8, "cpus": 4}).cpu_count == 8


def test_vm_info_cpu_count_falls_back_to_cpu_count_key() -> None:
    assert VmInfo.from_api({"cpu_count": 6}).cpu_count == 6


def test_vm_info_defaults_for_empty_payload() -> None:
    info = VmInfo.from_api({})
    assert info.vm_id == 0
    assert info.name == ""
    assert info.node == ""
    assert info.state == VmState.UNKNOWN
    assert info.cpu_count == 1
    assert info.memory_mb == 0
    assert info.uptime_seconds == 0
    assert info.ipv4 == []
    assert info.template is False


def test_vm_info_memory_mb_floor_divides_bytes() -> None:
    # 1_500_000_000 B is not a whole number of MiB: 1_500_000_000 // 1048576 == 1430
    assert VmInfo.from_api({"maxmem": 1_500_000_000}).memory_mb == 1430


def test_vm_info_coerces_string_numbers() -> None:
    info = VmInfo.from_api({"vmid": "101", "uptime": "3600", "maxcpu": "3"})
    assert info.vm_id == 101
    assert info.uptime_seconds == 3600
    assert info.cpu_count == 3


def test_vm_info_template_flag_truthy() -> None:
    assert VmInfo.from_api({"template": 1}).template is True
    assert VmInfo.from_api({"template": 0}).template is False


# ------------------------------------------------------------------
# VmMetrics
# ------------------------------------------------------------------


def test_vm_metrics_cpu_pct_computed() -> None:
    raw = {
        "vmid": 100,
        "cpu": 0.05,
        "mem": 536870912,  # 512 MB
        "maxmem": 1073741824,  # 1 GB
        "netin": 0,
        "netout": 0,
        "diskread": 0,
        "diskwrite": 0,
    }
    m = VmMetrics.from_api(raw)
    assert m.cpu_pct == pytest.approx(5.0)
    assert m.mem_used_pct == pytest.approx(50.0)
    assert m.mem_used_bytes == 536870912
    assert m.mem_total_bytes == 1073741824


def test_vm_metrics_zero_maxmem_safe() -> None:
    raw = {
        "vmid": 100,
        "cpu": 0.0,
        "mem": 0,
        "maxmem": 0,
        "netin": 0,
        "netout": 0,
        "diskread": 0,
        "diskwrite": 0,
    }
    m = VmMetrics.from_api(raw)
    assert m.mem_used_pct == 0.0


def test_vm_metrics_maps_all_io_counters() -> None:
    raw = {
        "vmid": 100,
        "cpu": 0.1235,
        "mem": 1,
        "maxmem": 3,
        "netin": 111,
        "netout": 222,
        "diskread": 333,
        "diskwrite": 444,
    }
    m = VmMetrics.from_api(raw)
    assert m.cpu_pct == 12.35
    assert m.mem_used_pct == 33.33
    assert m.net_in_bytes == 111
    assert m.net_out_bytes == 222
    assert m.disk_read_bytes == 333
    assert m.disk_write_bytes == 444


def test_vm_metrics_defaults_for_empty_payload() -> None:
    m = VmMetrics.from_api({})
    assert m.vm_id == 0
    assert m.cpu_pct == 0.0
    assert m.mem_used_bytes == 0
    assert m.mem_total_bytes == 0
    assert m.mem_used_pct == 0.0
    assert m.net_in_bytes == 0
    assert m.net_out_bytes == 0
    assert m.disk_read_bytes == 0
    assert m.disk_write_bytes == 0


def test_vm_metrics_mem_without_maxmem_reports_zero_percent() -> None:
    m = VmMetrics.from_api({"vmid": 100, "mem": 4096, "cpu": 0.5})
    assert m.mem_used_bytes == 4096
    assert m.mem_total_bytes == 0
    assert m.mem_used_pct == 0.0
    assert m.cpu_pct == 50.0


# ------------------------------------------------------------------
# NodeInfo
# ------------------------------------------------------------------


def test_node_info_from_api() -> None:
    raw = {
        "node": "pve",
        "status": "online",
        "maxcpu": 16,
        "maxmem": 32 * 1024 * 1024 * 1024,
        "mem": 8 * 1024 * 1024 * 1024,
        "uptime": 86400,
    }
    node = NodeInfo.from_api(raw)
    assert node.name == "pve"
    assert node.status == "online"
    assert node.cpu_count == 16


def test_node_info_maps_memory_and_uptime() -> None:
    node = NodeInfo.from_api(
        {
            "node": "pve2",
            "status": "offline",
            "maxcpu": 4,
            "maxmem": 17179869184,
            "mem": 4294967296,
            "uptime": 1234,
        }
    )
    assert node.memory_total_bytes == 17179869184
    assert node.memory_used_bytes == 4294967296
    assert node.uptime_seconds == 1234


def test_node_info_defaults_for_empty_payload() -> None:
    node = NodeInfo.from_api({})
    assert node.name == ""
    assert node.status == "unknown"
    assert node.cpu_count == 0
    assert node.memory_total_bytes == 0
    assert node.memory_used_bytes == 0
    assert node.uptime_seconds == 0


# ------------------------------------------------------------------
# SnapshotInfo
# ------------------------------------------------------------------


def test_snapshot_info_from_api() -> None:
    raw = {
        "name": "snap-1",
        "description": "before upgrade",
        "snaptime": 1700000000,
        "parent": None,
    }
    snap = SnapshotInfo.from_api(raw, vm_id=100)
    assert snap.name == "snap-1"
    assert snap.description == "before upgrade"
    assert snap.vm_id == 100
    assert snap.created == 1700000000


def test_snapshot_info_keeps_parent_name() -> None:
    snap = SnapshotInfo.from_api({"name": "child", "parent": "base"}, vm_id=42)
    assert snap.parent == "base"
    assert snap.vm_id == 42


def test_snapshot_info_empty_parent_becomes_none() -> None:
    # Proxmox sends "" for a root snapshot; the model normalises it to None.
    assert SnapshotInfo.from_api({"parent": ""}).parent is None


def test_snapshot_info_defaults_for_empty_payload() -> None:
    snap = SnapshotInfo.from_api({})
    assert snap.name == ""
    assert snap.vm_id == 0
    assert snap.created == 0
    assert snap.description == ""
    assert snap.parent is None


# ------------------------------------------------------------------
# TemplateInfo
# ------------------------------------------------------------------


def test_template_info_from_api() -> None:
    raw = {"vmid": 9000, "name": "ubuntu-22", "node": "pve"}
    tmpl = TemplateInfo.from_api(raw)
    assert tmpl.vm_id == 9000
    assert tmpl.name == "ubuntu-22"


def test_template_info_maps_description_cores_and_memory() -> None:
    tmpl = TemplateInfo.from_api(
        {
            "vmid": 9001,
            "name": "debian-12",
            "node": "pve2",
            "description": "cloud image",
            "maxcpu": 8,
            "cpus": 8,
            "maxmem": 2147483648,
        }
    )
    assert tmpl.node == "pve2"
    assert tmpl.description == "cloud image"
    assert tmpl.cores == 8
    assert tmpl.memory_mb == 2048


def test_template_info_cores_falls_back_to_cpus() -> None:
    assert TemplateInfo.from_api({"cpus": 3}).cores == 3


def test_template_info_defaults_for_empty_payload() -> None:
    tmpl = TemplateInfo.from_api({})
    assert tmpl.vm_id == 0
    assert tmpl.name == ""
    assert tmpl.node == ""
    assert tmpl.description == ""
    assert tmpl.cores == 0
    assert tmpl.memory_mb == 0


# ------------------------------------------------------------------
# TaskInfo
# ------------------------------------------------------------------


def test_task_info_from_api_maps_all_fields() -> None:
    task = TaskInfo.from_api(
        {
            "upid": "UPID:pve:0000ABCD:00000000:64F00000:qmstart:100:root@pam:",
            "node": "pve",
            "type": "qmstart",
            "status": "stopped",
            "exitstatus": "OK",
        }
    )
    assert task.upid == "UPID:pve:0000ABCD:00000000:64F00000:qmstart:100:root@pam:"
    assert task.node == "pve"
    assert task.type == "qmstart"
    assert task.status == "stopped"
    assert task.exit_status == "OK"


def test_task_info_running_task_has_no_exit_status() -> None:
    task = TaskInfo.from_api(
        {"upid": "u", "node": "pve", "type": "qmigrate", "status": "running"}
    )
    assert task.exit_status is None
    assert task.status == "running"


def test_task_info_empty_exit_status_becomes_none() -> None:
    assert TaskInfo.from_api({"exitstatus": ""}).exit_status is None


def test_task_info_defaults_for_empty_payload() -> None:
    task = TaskInfo.from_api({})
    assert task.upid == ""
    assert task.node == ""
    assert task.type == ""
    assert task.status == ""
    assert task.exit_status is None


# ------------------------------------------------------------------
# VmConfig
# ------------------------------------------------------------------


def test_vm_config_defaults() -> None:
    cfg = VmConfig()
    assert cfg.name is None
    assert cfg.template_id is None
    assert cfg.node is None
    assert cfg.cores is None
    assert cfg.memory_mb is None
    assert cfg.disk_gb is None
    assert cfg.cloud_init_config is None
    assert cfg.start is True


def test_vm_config_holds_cloud_init_config() -> None:
    ci = CloudInitConfig(username="ubuntu")
    cfg = VmConfig(
        name="vm1",
        template_id=9000,
        cores=2,
        memory_mb=2048,
        start=False,
        cloud_init_config=ci,
    )
    assert cfg.name == "vm1"
    assert cfg.template_id == 9000
    assert cfg.cores == 2
    assert cfg.memory_mb == 2048
    assert cfg.start is False
    assert cfg.cloud_init_config is ci


# ------------------------------------------------------------------
# CloudInitConfig.to_api_params
# ------------------------------------------------------------------


def test_cloud_init_empty_config_serialises_to_nothing() -> None:
    assert CloudInitConfig().to_api_params() == {}


def test_cloud_init_omits_unset_fields() -> None:
    params = CloudInitConfig(username="ubuntu", ip_config="ip=dhcp").to_api_params()
    assert params == {"ciuser": "ubuntu", "ipconfig0": "ip=dhcp"}
    assert "cipassword" not in params
    assert "sshkeys" not in params
    assert "nameserver" not in params
    assert "searchdomain" not in params


def test_cloud_init_full_config_maps_to_proxmox_keys() -> None:
    cfg = CloudInitConfig(
        username="ubuntu",
        password="s3cret",
        ssh_keys=["ssh-ed25519 KEY"],
        ip_config="ip=10.0.0.5/24,gw=10.0.0.1",
        nameserver="8.8.8.8",
        searchdomain="example.com",
    )
    assert cfg.to_api_params() == {
        "ciuser": "ubuntu",
        "cipassword": "s3cret",
        "sshkeys": "ssh-ed25519%20KEY",
        "ipconfig0": "ip=10.0.0.5/24,gw=10.0.0.1",
        "nameserver": "8.8.8.8",
        "searchdomain": "example.com",
    }


def test_cloud_init_ssh_keys_are_url_encoded() -> None:
    cfg = CloudInitConfig(ssh_keys=["ssh-rsa AAAAB3NzaC1yc2E= user@host"])
    assert (
        cfg.to_api_params()["sshkeys"] == "ssh-rsa%20AAAAB3NzaC1yc2E%3D%20user%40host"
    )


def test_cloud_init_multiple_ssh_keys_joined_with_encoded_newline() -> None:
    cfg = CloudInitConfig(ssh_keys=["key-one", "key two"])
    assert cfg.to_api_params()["sshkeys"] == "key-one%0Akey%20two"


def test_cloud_init_empty_ssh_key_list_is_omitted() -> None:
    assert CloudInitConfig(ssh_keys=[]).to_api_params() == {}


# ------------------------------------------------------------------
# CommandResult
# ------------------------------------------------------------------


def test_command_result_success_property() -> None:
    assert CommandResult(exit_code=0, stdout="ok", stderr="").success is True
    assert CommandResult(exit_code=1, stdout="", stderr="err").success is False
