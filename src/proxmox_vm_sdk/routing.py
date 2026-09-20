"""Proxmox host NAT / port-forwarding management.

Mirrors the functionality of the add_nat_rules.yml and remove_nat_rules.yml
Ansible playbooks from proxmox-stack-deployer, implemented in pure Python
via SSH.

Usage::

    from proxmox_vm_sdk.routing import ProxmoxRoutingManager, PortMapping

    mgr = ProxmoxRoutingManager.from_key(
        host="192.168.1.100",
        user="root",
        ssh_key_path="~/.ssh/id_rsa",
    )

    mappings = [
        PortMapping(
            vm_id=100, vm_name="node-1", vm_ip="10.0.0.10", vm_port=22, service="SSH"
        ),
        PortMapping(
            vm_id=100, vm_name="node-1", vm_ip="10.0.0.10", vm_port=6443, service="k3s"
        ),
    ]
    assigned = mgr.add_rules(mappings)
    for m in assigned:
        print(f"{m.service}: host:{m.host_port} -> {m.vm_ip}:{m.vm_port}")

    # Later, to remove:
    mgr.remove_rules(assigned)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proxmox_vm_sdk._backend import SshBackend

# Comment tag embedded in every iptables rule we write.
# Used to identify and remove our rules without touching others.
_RULE_TAG = "# VM {vm_id} ({vm_name}) - {service}"

# Regex to parse an existing rule line from the interfaces file
_RULE_RE = re.compile(
    r"iptables -t nat -[AD] PREROUTING -i (\S+) -p tcp --dport (\d+) "
    r"-j DNAT --to (\S+):(\d+) # VM (\d+) \(([^)]+)\) - (\S+)"
)


@dataclass
class PortMapping:
    """Describes a single port-forwarding rule: host_port → vm_ip:vm_port.

    ``host_port`` is ``None`` before ``add_rules()`` assigns it dynamically.
    """

    vm_id: int
    vm_name: str
    vm_ip: str
    vm_port: int
    service: str
    host_port: int | None = None
    vm_user: str | None = None
    vm_role: str | None = None

    def tag(self) -> str:
        """Return the comment tag embedded in this mapping's iptables rules.

        The tag records the VM id, VM name and service so a rule can be matched
        back to the mapping that wrote it.
        """
        return _RULE_TAG.format(
            vm_id=self.vm_id, vm_name=self.vm_name, service=self.service
        )


class ProxmoxRoutingManager:
    """Manages NAT port-forwarding rules on a Proxmox host.

    All rule state lives in the host's ``interfaces_file``
    (default: ``/etc/network/interfaces``). Rules survive reboots because
    ``ifreload`` re-applies them via the ``post-up`` stanzas.

    Port assignment is deterministic: existing ports (from ``ss -tln``) and
    ports already in the interfaces file are excluded; the lowest available
    port in ``port_range`` is assigned to each mapping in sorted order.
    """

    DEFAULT_INTERFACES_FILE = "/etc/network/interfaces"
    DEFAULT_EXTERNAL_IFACE = "vmbr0"
    DEFAULT_INTERNAL_IFACE = "vmbr1"
    DEFAULT_PORT_RANGE = (20000, 30000)

    def __init__(
        self,
        backend: SshBackend,
        *,
        interfaces_file: str = DEFAULT_INTERFACES_FILE,
        external_iface: str = DEFAULT_EXTERNAL_IFACE,
        internal_iface: str = DEFAULT_INTERNAL_IFACE,
        port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
    ) -> None:
        """Bind a routing manager to an SSH backend.

        `interfaces_file` is the file the rules are read from and written to,
        `external_iface` the interface the DNAT rules are bound to, and
        `internal_iface` the stanza new rules are inserted after. Host ports
        are assigned from `port_range`.
        """
        self._backend = backend
        self.interfaces_file = interfaces_file
        self.external_iface = external_iface
        self.internal_iface = internal_iface
        self.port_range = port_range

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_key(
        cls,
        host: str,
        user: str,
        ssh_key_path: str,
        *,
        port: int = 22,
        **kwargs: object,
    ) -> ProxmoxRoutingManager:
        """Connect with SSH key authentication."""
        from proxmox_vm_sdk._backend import ParamikoSshBackend

        backend = ParamikoSshBackend(host, user, ssh_key_path=ssh_key_path, port=port)
        return cls(backend, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def from_password(
        cls,
        host: str,
        user: str,
        password: str,
        *,
        port: int = 22,
        **kwargs: object,
    ) -> ProxmoxRoutingManager:
        """Connect with password authentication."""
        from proxmox_vm_sdk._backend import ParamikoSshBackend

        backend = ParamikoSshBackend(host, user, password=password, port=port)
        return cls(backend, **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_rules(self, mappings: list[PortMapping]) -> list[PortMapping]:
        """Assign host ports and add PREROUTING DNAT rules to the interfaces file.

        Steps (mirrors add_nat_rules.yml):
        1. Collect currently-used ports from ``ss -tln``
        2. Collect ports already claimed in the interfaces file
        3. Remove any existing rules for the given VMs (idempotent)
        4. Assign the next available ports from ``port_range``
        5. Append ``post-up`` and ``post-down`` stanzas
        6. Flush iptables PREROUTING chain
        7. Reload interfaces (``ifreload --all``)

        Returns the input list with ``host_port`` filled in on each mapping.

        An empty ``mappings`` is a no-op that returns an empty list, without
        touching the interfaces file or reloading iptables.
        """
        if not mappings:
            return []

        reserved = self._collect_reserved_ports()
        available = self._available_ports(reserved, count=len(mappings))

        # First remove any existing rules for the same VMs (idempotent)
        self._remove_from_file(mappings)

        content = self._backend.read_file(self.interfaces_file)
        lines = content.splitlines(keepends=True)

        assigned: list[PortMapping] = []
        for mapping, host_port in zip(
            sorted(mappings, key=lambda m: f"{m.vm_name}_{m.service}"),
            available,
            strict=True,
        ):
            mapping.host_port = host_port
            assigned.append(mapping)
            tag = mapping.tag()
            post_up = (
                f"    post-up iptables -t nat -A PREROUTING"
                f" -i {self.external_iface} -p tcp --dport {host_port}"
                f" -j DNAT --to {mapping.vm_ip}:{mapping.vm_port} {tag}\n"
            )
            post_down = (
                f"    post-down iptables -t nat -D PREROUTING"
                f" -i {self.external_iface} -p tcp --dport {host_port}"
                f" -j DNAT --to {mapping.vm_ip}:{mapping.vm_port} {tag}\n"
            )
            # Insert before the first blank line after the target interface
            # stanza, or append at end if the interface block is not found.
            insert_idx = self._find_iface_insert_point(lines, self.internal_iface)
            lines.insert(insert_idx, post_down)
            lines.insert(insert_idx, post_up)

        self._backend.write_file(self.interfaces_file, "".join(lines))
        self._flush_and_reload()
        return assigned

    def remove_rules(self, mappings: list[PortMapping]) -> None:
        """Remove PREROUTING DNAT rules for the given VMs from the interfaces file.

        Steps (mirrors remove_nat_rules.yml):
        1. Remove matching lines from the interfaces file
        2. Flush iptables PREROUTING chain
        3. Reload interfaces
        """
        self._remove_from_file(mappings)
        self._flush_and_reload()

    def list_rules(self) -> list[PortMapping]:
        """Parse the interfaces file and return all rules written by this manager."""
        content = self._backend.read_file(self.interfaces_file)
        return self._parse_rules(content)

    def flush_rules(self) -> None:
        """Flush all iptables PREROUTING rules and reload interfaces.

        This removes active (in-memory) rules without modifying the
        interfaces file. Rules will be re-applied on the next ``ifreload``.
        """
        self._backend.run("iptables -t nat -F PREROUTING")
        self._backend.run("ifreload --all")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_reserved_ports(self) -> set[int]:
        """Return the union of ports in use and ports already in the file."""
        # Active listening ports from ss (parse raw output in Python so
        # the FakeSshBackend doesn't need to run awk)
        _, stdout, _ = self._backend.run("ss -tln")
        active: set[int] = set()
        for line in stdout.splitlines():
            parts = line.split()
            if not parts or parts[0] in ("State", "Netid"):
                continue
            # Column 3 (0-indexed) is Local Address:Port, e.g. "*:22" or "[::]:22"
            addr = parts[3] if len(parts) > 3 else ""
            port_str = addr.rsplit(":", 1)[-1]
            if port_str.isdigit():
                active.add(int(port_str))

        # Ports already claimed in interfaces file
        content = self._backend.read_file(self.interfaces_file)
        claimed: set[int] = set()
        for m in _RULE_RE.finditer(content):
            claimed.add(int(m.group(2)))

        return active | claimed

    def _available_ports(self, reserved: set[int], count: int) -> list[int]:
        # Asking for zero ports is satisfiable, but the loop below can never
        # notice: it only returns once `len(available) == count`, which is
        # already true before the first iteration. Without this the search ran
        # the whole range and raised "Need 0, found 10000".
        if count <= 0:
            return []

        start, end = self.port_range
        available: list[int] = []
        for port in range(start, end):
            if port not in reserved:
                available.append(port)
                if len(available) == count:
                    return available
        raise RuntimeError(
            f"Not enough available ports in range {self.port_range}. "
            f"Need {count}, found {len(available)}."
        )

    def _remove_from_file(self, mappings: list[PortMapping]) -> None:
        content = self._backend.read_file(self.interfaces_file)
        lines = content.splitlines(keepends=True)
        tags = {m.tag() for m in mappings}
        # The tag is written as the trailing comment on a rule line, so the line
        # must END with it. Matching on substring deleted unrelated rules: the
        # tag for service "SSH" is a prefix of the tag for "SSH2", so removing
        # the SSH rule took the SSH2 rule with it.
        filtered = [
            line for line in lines if not any(line.rstrip().endswith(t) for t in tags)
        ]
        self._backend.write_file(self.interfaces_file, "".join(filtered))

    def _flush_and_reload(self) -> None:
        self._backend.run("iptables -t nat -F PREROUTING")
        self._backend.run("ifreload --all")

    @staticmethod
    def _find_iface_insert_point(lines: list[str], iface: str) -> int:
        """Find the line index just before the end of the given iface stanza.

        Looks for ``iface <name>`` and returns the index of the first blank
        line or next ``iface`` line after it (to append post-up before them).
        Falls back to len(lines) if not found.
        """
        in_block = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(f"iface {iface}"):
                in_block = True
                continue
            if in_block and (
                stripped == ""
                or (
                    stripped.startswith("iface ")
                    and not stripped.startswith(f"iface {iface}")
                )
            ):
                return i
        return len(lines)

    @staticmethod
    def _parse_rules(content: str) -> list[PortMapping]:
        seen: dict[tuple[int, str], PortMapping] = {}
        for m in _RULE_RE.finditer(content):
            host_port = int(m.group(2))
            vm_ip = m.group(3)
            vm_port = int(m.group(4))
            vm_id = int(m.group(5))
            vm_name = m.group(6)
            service = m.group(7)
            key = (vm_id, service)
            if key not in seen:
                seen[key] = PortMapping(
                    vm_id=vm_id,
                    vm_name=vm_name,
                    vm_ip=vm_ip,
                    vm_port=vm_port,
                    service=service,
                    host_port=host_port,
                )
        return list(seen.values())
