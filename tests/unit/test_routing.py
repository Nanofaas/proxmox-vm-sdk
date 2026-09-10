"""Tests for ProxmoxRoutingManager.

All tests use FakeSshBackend — no real SSH connection needed.
The fake backend simulates the Proxmox host filesystem and command output.
"""

from unittest.mock import MagicMock, patch

import paramiko
import pytest

from proxmox_sdk._backend import ParamikoSshBackend
from proxmox_sdk.routing import PortMapping, ProxmoxRoutingManager
from proxmox_sdk.testing import FakeSshBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BLANK_INTERFACES = """\
auto lo
iface lo inet loopback

auto vmbr0
iface vmbr0 inet static
    address 192.168.1.100/24
    gateway 192.168.1.1
    bridge-ports eth0
    bridge-stp off

auto vmbr1
iface vmbr1 inet static
    address 10.0.0.1/24
    bridge-ports none
    bridge-stp off

"""


def make_backend(interfaces_content: str = BLANK_INTERFACES) -> FakeSshBackend:
    fb = FakeSshBackend()
    fb.seed_file("/etc/network/interfaces", interfaces_content)
    # Simulate ss -tln: ports 22 and 80 in use
    fb.seed_response(
        "ss -tln",
        0,
        "State  Recv-Q  Send-Q  Local Address:Port\n"
        "LISTEN 0       128     *:22\n"
        "LISTEN 0       128     *:80\n",
    )
    return fb


def make_manager(backend: FakeSshBackend) -> ProxmoxRoutingManager:
    return ProxmoxRoutingManager(
        backend,
        interfaces_file="/etc/network/interfaces",
        external_iface="vmbr0",
        internal_iface="vmbr1",
        port_range=(20000, 21000),
    )


SAMPLE_MAPPINGS = [
    PortMapping(
        vm_id=100,
        vm_name="node-1",
        vm_ip="10.0.0.10",
        vm_port=22,
        service="SSH",
        vm_user="ubuntu",
    ),
    PortMapping(
        vm_id=100,
        vm_name="node-1",
        vm_ip="10.0.0.10",
        vm_port=6443,
        service="k3s",
    ),
    PortMapping(
        vm_id=101,
        vm_name="node-2",
        vm_ip="10.0.0.11",
        vm_port=22,
        service="SSH",
        vm_user="ubuntu",
    ),
]

# ---------------------------------------------------------------------------
# add_rules
# ---------------------------------------------------------------------------


def test_add_rules_assigns_host_ports() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    assigned = mgr.add_rules(list(SAMPLE_MAPPINGS))

    assert len(assigned) == 3
    assert all(m.host_port is not None for m in assigned)
    host_ports = {m.host_port for m in assigned}
    # All distinct
    assert len(host_ports) == 3


def test_add_rules_ports_in_range() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    assigned = mgr.add_rules(list(SAMPLE_MAPPINGS))
    for m in assigned:
        assert 20000 <= m.host_port < 21000  # type: ignore[operator]


def test_add_rules_ports_not_already_in_use() -> None:
    backend = make_backend()
    # Seed some already-used ports at the start of the range
    backend.seed_response(
        "ss -tln",
        0,
        "State  Recv-Q  Send-Q  Local Address:Port\n"
        "LISTEN 0       128     *:20000\n"
        "LISTEN 0       128     *:20001\n",
    )
    mgr = make_manager(backend)
    assigned = mgr.add_rules(list(SAMPLE_MAPPINGS))
    for m in assigned:
        assert m.host_port not in (20000, 20001)  # type: ignore[operator]


def test_add_rules_writes_post_up_and_post_down() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    assigned = mgr.add_rules(list(SAMPLE_MAPPINGS))

    content = backend.read_file("/etc/network/interfaces")
    for m in assigned:
        assert f"--dport {m.host_port}" in content
        assert f"--to {m.vm_ip}:{m.vm_port}" in content
        assert "post-up iptables -t nat -A PREROUTING" in content
        assert "post-down iptables -t nat -D PREROUTING" in content
        assert m.tag() in content


def test_add_rules_flushes_and_reloads() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    mgr.add_rules(list(SAMPLE_MAPPINGS))
    backend.assert_ran("iptables -t nat -F PREROUTING")
    backend.assert_ran("ifreload --all")


def test_add_rules_idempotent() -> None:
    """Calling add_rules twice should not duplicate rules."""
    backend = make_backend()
    mgr = make_manager(backend)
    _first = mgr.add_rules(list(SAMPLE_MAPPINGS))
    second = mgr.add_rules(list(SAMPLE_MAPPINGS))

    content = backend.read_file("/etc/network/interfaces")
    # Each tag should appear exactly twice (post-up + post-down)
    for m in second:
        assert content.count(m.tag()) == 2


# ---------------------------------------------------------------------------
# remove_rules
# ---------------------------------------------------------------------------


def test_remove_rules_clears_entries() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    assigned = mgr.add_rules(list(SAMPLE_MAPPINGS))

    mgr.remove_rules(assigned)

    content = backend.read_file("/etc/network/interfaces")
    for m in assigned:
        assert m.tag() not in content


def test_remove_rules_flushes_and_reloads() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    assigned = mgr.add_rules(list(SAMPLE_MAPPINGS))
    backend.commands.clear()  # reset call log

    mgr.remove_rules(assigned)
    backend.assert_ran("iptables -t nat -F PREROUTING")
    backend.assert_ran("ifreload --all")


def test_remove_rules_preserves_unrelated_lines() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    assigned = mgr.add_rules(list(SAMPLE_MAPPINGS))
    mgr.remove_rules(assigned)

    content = backend.read_file("/etc/network/interfaces")
    assert "iface vmbr0 inet static" in content
    assert "iface vmbr1 inet static" in content


# ---------------------------------------------------------------------------
# list_rules
# ---------------------------------------------------------------------------


def test_list_rules_empty_initially() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    assert mgr.list_rules() == []


def test_list_rules_returns_added_rules() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    assigned = mgr.add_rules(list(SAMPLE_MAPPINGS))

    rules = mgr.list_rules()
    assert len(rules) == 3
    rule_services = {r.service for r in rules}
    assert rule_services == {"SSH", "k3s"}

    for rule in rules:
        # Find the matching assigned mapping
        match = next(
            (
                m
                for m in assigned
                if m.vm_id == rule.vm_id and m.service == rule.service
            ),
            None,
        )
        assert match is not None
        assert rule.host_port == match.host_port
        assert rule.vm_ip == match.vm_ip
        assert rule.vm_port == match.vm_port


# ---------------------------------------------------------------------------
# flush_rules
# ---------------------------------------------------------------------------


def test_flush_rules_calls_iptables_and_ifreload() -> None:
    backend = make_backend()
    mgr = make_manager(backend)
    mgr.flush_rules()
    backend.assert_ran("iptables -t nat -F PREROUTING")
    backend.assert_ran("ifreload --all")


# ---------------------------------------------------------------------------
# Port range exhaustion
# ---------------------------------------------------------------------------


def test_not_enough_ports_raises() -> None:
    backend = make_backend()
    mgr = ProxmoxRoutingManager(
        backend,
        interfaces_file="/etc/network/interfaces",
        port_range=(20000, 20002),  # only 2 ports available
    )
    with pytest.raises(RuntimeError, match="Not enough available ports"):
        mgr.add_rules(list(SAMPLE_MAPPINGS))  # needs 3


# ---------------------------------------------------------------------------
# PortMapping dataclass
# ---------------------------------------------------------------------------


def test_port_mapping_tag_format() -> None:
    m = PortMapping(
        vm_id=42, vm_name="my-vm", vm_ip="10.0.0.1", vm_port=22, service="SSH"
    )
    assert m.tag() == "# VM 42 (my-vm) - SSH"


def test_port_mapping_host_port_none_by_default() -> None:
    m = PortMapping(vm_id=1, vm_name="x", vm_ip="1.1.1.1", vm_port=22, service="SSH")
    assert m.host_port is None


# ---------------------------------------------------------------------------
# Existing rules in interfaces file are respected
# ---------------------------------------------------------------------------


def test_existing_rules_not_reassigned_same_port() -> None:
    """Ports already in the interfaces file must not be reused."""
    # Pre-populate file with an existing rule at port 20000
    existing = BLANK_INTERFACES + (
        "    post-up iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 20000 "
        "-j DNAT --to 10.0.0.99:22 # VM 999 (old-vm) - SSH\n"
    )
    backend = make_backend(existing)
    mgr = make_manager(backend)

    assigned = mgr.add_rules([SAMPLE_MAPPINGS[0]])
    assert assigned[0].host_port != 20000


# ---------------------------------------------------------------------------
# SSH constructors
# ---------------------------------------------------------------------------


def test_from_key_builds_backend_and_forwards_init_kwargs():
    with patch("paramiko.SSHClient") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        mgr = ProxmoxRoutingManager.from_key(
            "10.0.0.1",
            "root",
            "/home/u/.ssh/id_rsa",
            port=2222,
            interfaces_file="/etc/network/interfaces.test",
            port_range=(1000, 2000),
        )

    assert isinstance(mgr._backend, ParamikoSshBackend)
    mock_client.connect.assert_called_once_with(
        hostname="10.0.0.1",
        username="root",
        port=2222,
        key_filename="/home/u/.ssh/id_rsa",
        password=None,
    )
    # The manager must end up bound to the caller's file / range, not defaults.
    assert mgr.interfaces_file == "/etc/network/interfaces.test"
    assert mgr.port_range == (1000, 2000)


def test_from_key_defaults_port_to_22():
    with patch("paramiko.SSHClient") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        ProxmoxRoutingManager.from_key("host", "root", "/key")

    assert mock_client.connect.call_args.kwargs["port"] == 22


def test_from_key_trusts_unknown_host_key():
    with patch("paramiko.SSHClient") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        ProxmoxRoutingManager.from_key("host", "root", "/key")

    policy = mock_client.set_missing_host_key_policy.call_args.args[0]
    # Trust-on-first-connect is the documented default; RejectPolicy would
    # break every first connection to an unknown host.
    assert isinstance(policy, paramiko.AutoAddPolicy)


def test_from_password_builds_backend_with_password():
    with patch("paramiko.SSHClient") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        mgr = ProxmoxRoutingManager.from_password(
            "10.0.0.2", "root@pam", "s3cret", port=2200
        )

    assert isinstance(mgr._backend, ParamikoSshBackend)
    mock_client.connect.assert_called_once_with(
        hostname="10.0.0.2",
        username="root@pam",
        port=2200,
        key_filename=None,
        password="s3cret",
    )
    assert mgr.interfaces_file == ProxmoxRoutingManager.DEFAULT_INTERFACES_FILE


# ---------------------------------------------------------------------------
# _collect_reserved_ports
# ---------------------------------------------------------------------------


def test_collect_reserved_ports_unions_listening_and_claimed():
    existing = BLANK_INTERFACES + (
        "    post-up iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 20000 "
        "-j DNAT --to 10.0.0.99:22 # VM 999 (old-vm) - SSH\n"
    )
    backend = make_backend(existing)  # ss reports 22 and 80
    mgr = make_manager(backend)

    assert mgr._collect_reserved_ports() == {22, 80, 20000}


def test_collect_reserved_ports_skips_header_rows():
    backend = make_backend()
    backend.seed_response("ss -tln", 0, "State  Recv-Q  Send-Q  Local Address:Port\n")
    mgr = make_manager(backend)

    assert mgr._collect_reserved_ports() == set()


def test_collect_reserved_ports_ignores_non_numeric_port_tokens():
    backend = make_backend()
    backend.seed_response(
        "ss -tln",
        0,
        "State  Recv-Q  Send-Q  Local Address:Port\n"
        "LISTEN 0       128     *:22\n"
        "UNCONN 0       0       *:sunrpc\n"
        "LISTEN 0       128     [::]:80\n",
    )
    mgr = make_manager(backend)

    # A service name in the port column must not be parsed as a port number.
    assert mgr._collect_reserved_ports() == {22, 80}


# ---------------------------------------------------------------------------
# add_rules: assignment order and object identity
# ---------------------------------------------------------------------------


def test_add_rules_assigns_lowest_ports_in_sorted_order():
    backend = make_backend()
    mgr = make_manager(backend)
    k3s = PortMapping(
        vm_id=100, vm_name="node-1", vm_ip="10.0.0.10", vm_port=6443, service="k3s"
    )
    ssh2 = PortMapping(
        vm_id=101, vm_name="node-2", vm_ip="10.0.0.11", vm_port=22, service="SSH"
    )
    ssh1 = PortMapping(
        vm_id=100, vm_name="node-1", vm_ip="10.0.0.10", vm_port=22, service="SSH"
    )

    assigned = mgr.add_rules([k3s, ssh2, ssh1])

    # Sorted by f"{vm_name}_{service}": node-1_SSH, node-1_k3s, node-2_SSH.
    assert assigned == [ssh1, k3s, ssh2]
    assert [m.host_port for m in assigned] == [20000, 20001, 20002]
    # The caller's own objects are mutated and returned, not copies.
    assert assigned[0] is ssh1
    assert assigned[1] is k3s
    assert assigned[2] is ssh2
    assert ssh1.host_port == 20000


def test_add_rules_writes_both_stanzas_at_the_assigned_port():
    backend = make_backend()
    mgr = make_manager(backend)
    m = PortMapping(
        vm_id=7, vm_name="db", vm_ip="10.0.0.7", vm_port=5432, service="SQL"
    )

    mgr.add_rules([m])

    content = backend.read_file("/etc/network/interfaces")
    assert (
        "    post-up iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 20000"
        " -j DNAT --to 10.0.0.7:5432 # VM 7 (db) - SQL\n" in content
    )
    assert (
        "    post-down iptables -t nat -D PREROUTING -i vmbr0 -p tcp --dport 20000"
        " -j DNAT --to 10.0.0.7:5432 # VM 7 (db) - SQL\n" in content
    )


# ---------------------------------------------------------------------------
# _find_iface_insert_point / stanza placement
# ---------------------------------------------------------------------------

TIGHT_IFACES = (
    "auto lo\n"
    "iface lo inet loopback\n"
    "\n"
    "auto vmbr1\n"
    "iface vmbr1 inet static\n"
    "    address 10.0.0.1/24\n"
    "auto vmbr2\n"
    "iface vmbr2 inet static\n"
    "    address 10.0.1.1/24\n"
)

NO_INTERNAL_IFACE = (
    "auto lo\n"
    "iface lo inet loopback\n"
    "\n"
    "auto vmbr0\n"
    "iface vmbr0 inet static\n"
    "    address 1.2.3.4/24\n"
)


def test_find_iface_insert_point_returns_end_when_iface_absent():
    lines = TIGHT_IFACES.splitlines(keepends=True)

    assert ProxmoxRoutingManager._find_iface_insert_point(lines, "vmbr9") == len(lines)


def test_find_iface_insert_point_stops_at_next_iface_without_blank_line():
    lines = TIGHT_IFACES.splitlines(keepends=True)

    idx = ProxmoxRoutingManager._find_iface_insert_point(lines, "vmbr1")

    assert lines[idx] == "iface vmbr2 inet static\n"


def test_add_rules_appends_at_end_when_internal_iface_missing():
    backend = make_backend(NO_INTERNAL_IFACE)
    mgr = make_manager(backend)
    mgr.internal_iface = "vmbr9"  # not present in the file
    m = PortMapping(
        vm_id=100, vm_name="n1", vm_ip="10.0.0.10", vm_port=22, service="SSH"
    )

    mgr.add_rules([m])

    post_up = (
        "    post-up iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 20000"
        " -j DNAT --to 10.0.0.10:22 # VM 100 (n1) - SSH\n"
    )
    post_down = (
        "    post-down iptables -t nat -D PREROUTING -i vmbr0 -p tcp --dport 20000"
        " -j DNAT --to 10.0.0.10:22 # VM 100 (n1) - SSH\n"
    )
    content = backend.read_file("/etc/network/interfaces")
    assert content == NO_INTERNAL_IFACE + post_up + post_down


def test_add_rules_inserts_before_next_iface_when_no_blank_line():
    backend = make_backend(TIGHT_IFACES)
    mgr = make_manager(backend)
    m = PortMapping(
        vm_id=100, vm_name="n1", vm_ip="10.0.0.10", vm_port=22, service="SSH"
    )

    mgr.add_rules([m])

    post_up = (
        "    post-up iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 20000"
        " -j DNAT --to 10.0.0.10:22 # VM 100 (n1) - SSH\n"
    )
    post_down = (
        "    post-down iptables -t nat -D PREROUTING -i vmbr0 -p tcp --dport 20000"
        " -j DNAT --to 10.0.0.10:22 # VM 100 (n1) - SSH\n"
    )
    expected = (
        "auto lo\n"
        "iface lo inet loopback\n"
        "\n"
        "auto vmbr1\n"
        "iface vmbr1 inet static\n"
        "    address 10.0.0.1/24\n"
        "auto vmbr2\n"
        f"{post_up}{post_down}"
        "iface vmbr2 inet static\n"
        "    address 10.0.1.1/24\n"
    )
    assert backend.read_file("/etc/network/interfaces") == expected


# ---------------------------------------------------------------------------
# remove_rules
# ---------------------------------------------------------------------------


def test_remove_rules_for_unrelated_vm_leaves_file_unchanged():
    backend = make_backend()
    mgr = make_manager(backend)
    mgr.add_rules([SAMPLE_MAPPINGS[0]])
    before = backend.read_file("/etc/network/interfaces")

    mgr.remove_rules(
        [
            PortMapping(
                vm_id=999, vm_name="ghost", vm_ip="10.0.0.1", vm_port=22, service="X"
            )
        ]
    )

    assert backend.read_file("/etc/network/interfaces") == before


# ---------------------------------------------------------------------------
# flush_rules
# ---------------------------------------------------------------------------


def test_flush_rules_leaves_interfaces_file_untouched():
    backend = make_backend()
    mgr = make_manager(backend)
    mgr.add_rules(list(SAMPLE_MAPPINGS))
    before = backend.read_file("/etc/network/interfaces")

    mgr.flush_rules()

    # flush_rules only drops the in-memory rules; the file is what re-applies them.
    assert backend.read_file("/etc/network/interfaces") == before


# ---------------------------------------------------------------------------
# list_rules parsing
# ---------------------------------------------------------------------------


def test_list_rules_collapses_post_up_and_post_down_into_one_mapping():
    backend = make_backend()
    backend.seed_file(
        "/etc/network/interfaces",
        BLANK_INTERFACES
        + "    post-up iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 25000"
        " -j DNAT --to 10.0.0.5:8080 # VM 55 (web one) - HTTP\n"
        + "    post-down iptables -t nat -D PREROUTING -i vmbr0 -p tcp --dport 25000"
        " -j DNAT --to 10.0.0.5:8080 # VM 55 (web one) - HTTP\n",
    )
    mgr = make_manager(backend)

    rules = mgr.list_rules()

    assert rules == [
        PortMapping(
            vm_id=55,
            vm_name="web one",
            vm_ip="10.0.0.5",
            vm_port=8080,
            service="HTTP",
            host_port=25000,
        )
    ]


# ---------------------------------------------------------------------------
# PortMapping
# ---------------------------------------------------------------------------


def test_port_mapping_optional_fields_default_to_none():
    m = PortMapping(vm_id=1, vm_name="a", vm_ip="10.0.0.1", vm_port=22, service="SSH")

    assert (m.host_port, m.vm_user, m.vm_role) == (None, None, None)


def test_port_mapping_keeps_all_supplied_fields():
    m = PortMapping(
        vm_id=3,
        vm_name="a",
        vm_ip="10.0.0.3",
        vm_port=443,
        service="HTTPS",
        host_port=21000,
        vm_user="ubuntu",
        vm_role="worker",
    )

    assert m.tag() == "# VM 3 (a) - HTTPS"
    assert (m.host_port, m.vm_user, m.vm_role) == (21000, "ubuntu", "worker")


# ---------------------------------------------------------------------------
# Regressions found while writing these tests
#
# Both were real defects in routing.py, originally recorded here as
# xfail markers with their reproductions. They are fixed, so the markers are
# gone and these now assert the correct behaviour outright.
# ---------------------------------------------------------------------------


def test_add_rules_empty_mapping_list_is_a_noop():
    """_available_ports(count=0) used to run the whole range and raise."""
    backend = make_backend()
    mgr = make_manager(backend)

    assert mgr.add_rules([]) == []


def test_add_rules_empty_list_does_not_touch_the_interfaces_file():
    """A no-op must not bounce the host's network config either."""
    backend = make_backend()
    before = backend.read_file("/etc/network/interfaces")
    mgr = make_manager(backend)

    mgr.add_rules([])

    assert backend.read_file("/etc/network/interfaces") == before
    assert backend.commands == []


def test_available_ports_zero_is_satisfiable():
    """The helper is correct on its own, not only via add_rules' early return.

    add_rules() short-circuits an empty list before reaching this, so without a
    direct test the guard here would be unreachable and any future caller
    passing zero would hit the old "Need 0, found 10000" RuntimeError again.
    """
    mgr = make_manager(make_backend())

    assert mgr._available_ports(set(), 0) == []
    assert mgr._available_ports({20000, 20001}, 0) == []


def test_remove_rules_does_not_delete_prefix_colliding_service():
    """The tag for "SSH" is a prefix of the tag for "SSH2"."""
    backend = make_backend()
    mgr = make_manager(backend)
    ssh = PortMapping(
        vm_id=100, vm_name="node-1", vm_ip="10.0.0.10", vm_port=22, service="SSH"
    )
    ssh2 = PortMapping(
        vm_id=100, vm_name="node-1", vm_ip="10.0.0.10", vm_port=222, service="SSH2"
    )
    mgr.add_rules([ssh, ssh2])

    mgr.remove_rules([ssh])

    remaining = {(r.vm_id, r.service) for r in mgr.list_rules()}
    assert remaining == {(100, "SSH2")}
