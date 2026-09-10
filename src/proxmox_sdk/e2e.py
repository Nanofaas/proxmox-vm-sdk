"""End-to-end verification: create VM(s), verify readiness, delete VM(s).

Prerequisites:
    PROXMOX_HOST, PROXMOX_USER, PROXMOX_PASSWORD env vars set
    PROXMOX_NODE env var (or --node)

Usage:
    uv run proxmox-vm-e2e --node pve --template-id 9000
    uv run proxmox-vm-e2e --name test-vm --template-id 9000 --cores 2 --memory-mb 2048
    uv run proxmox-vm-e2e --count 3 --template-id 9000
    uv run proxmox-vm-e2e --configs '[{"name":"web","template_id":9000,"cores":2}]'
    uv run proxmox-vm-e2e --list-templates

    # With SSH verification (injects ~/.ssh/id_rsa.pub via cloud-init, opens NAT rule,
    # then connects via paramiko):
    uv run proxmox-vm-e2e --template-id 9000 --ssh-user ubuntu
    uv run proxmox-vm-e2e --template-id 9000 --ssh-key ~/.ssh/mykey --ssh-user ubuntu
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .client import ProxmoxClient
from .exceptions import ProxmoxError
from .models import CloudInitConfig, VmConfig
from .routing import PortMapping, ProxmoxRoutingManager
from .vm import ProxmoxVM


def _env_or_raise(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def _load_ssh_pubkey(key_path: str | None) -> str | None:
    """Return content of <key_path>.pub, or None if the file is absent."""
    # PTH111 wants Path.expanduser() here, but the two are not equivalent: for
    # an unresolvable `~user` os.path.expanduser returns the string unchanged
    # while Path.expanduser() raises RuntimeError, and Path also collapses the
    # doubled separators that os leaves alone. key_path comes from the
    # environment, so a typo would turn a clean "no pubkey" into a crash.
    # Verified on this interpreter rather than assumed.
    path = os.path.expanduser(key_path or "~/.ssh/id_rsa")  # noqa: PTH111
    pub = Path(path + ".pub")
    if pub.exists():
        return pub.read_text().strip()
    return None


def _ssh_exec(host: str, port: int, user: str, key_path: str, command: str) -> str:
    """Connect via SSH and run a single command. Returns stripped stdout."""
    import paramiko

    client = paramiko.SSHClient()
    # Same trade-off as ParamikoSshBackend: this verification step runs against
    # a host the caller already administers, and it is the only way to reach
    # the NAT-forwarded port of a VM that was just created.
    auto_add_policy = paramiko.AutoAddPolicy()
    client.set_missing_host_key_policy(auto_add_policy)  # nosec B507
    client.connect(host, port=port, username=user, key_filename=key_path, timeout=15)
    try:
        _, stdout, _ = client.exec_command(command)
        return stdout.read().decode().strip()
    finally:
        client.close()


def _verify_vm(
    vm: ProxmoxVM, idx: int, total: int, timeout: float
) -> tuple[int, str | None]:
    """Guest-agent verification. Returns (exit_code, vm_ip_or_None)."""
    label = f"  [{idx}/{total}] {vm.vm_id}"
    exit_code = 0
    vm_ip: str | None = None

    print(f"{label}: waiting for guest agent ...")
    t0 = time.monotonic()
    try:
        vm.wait_for_agent(timeout=timeout)
    except ProxmoxError as exc:
        print(f"{label}: agent timeout — {exc}", file=sys.stderr)
        return 1, None
    print(f"{label}: agent ready in {time.monotonic() - t0:.1f}s")

    print(f"{label}: waiting for IP ...")
    t0 = time.monotonic()
    try:
        vm_ip = vm.wait_for_ip(timeout=timeout)
    except ProxmoxError as exc:
        print(f"{label}: IP timeout — {exc}", file=sys.stderr)
        return 1, None
    print(f"{label}: got IP {vm_ip} in {time.monotonic() - t0:.1f}s")

    print(f"{label}: running verification command ...")
    result = vm.exec(["hostname"])
    print(f"{label}: exit={result.exit_code}  stdout={result.stdout.strip()}")
    if result.stderr:
        print(f"{label}: stderr={result.stderr.strip()}")
    if not result.success:
        print(f"{label}: FAILED", file=sys.stderr)
        exit_code = 1

    info = vm.info()
    print(
        f"{label}: state={info.state.value}  node={info.node}  "
        f"cores={info.cpu_count}  mem={info.memory_mb}MB"
    )
    return exit_code, vm_ip


def _verify_ssh(
    vm: ProxmoxVM,
    vm_ip: str,
    proxmox_host: str,
    proxmox_ssh_user: str,
    ssh_user: str,
    ssh_key_path: str,
    proxmox_key_path: str,
    idx: int,
    total: int,
) -> tuple[int, PortMapping | None]:
    """Add a NAT rule and verify SSH connectivity.

    Returns (exit_code, mapping_or_None); the mapping is returned even when
    the SSH check fails so the caller can tear the rule down again.
    """
    label = f"  [{idx}/{total}] {vm.vm_id}"

    mapping = PortMapping(
        vm_id=vm.vm_id,
        vm_name=str(vm.vm_id),
        vm_ip=vm_ip,
        vm_port=22,
        service="SSH",
        vm_user=ssh_user,
    )
    try:
        mgr = ProxmoxRoutingManager.from_key(
            proxmox_host, proxmox_ssh_user, proxmox_key_path
        )
        assigned = mgr.add_rules([mapping])
        mapping = assigned[0]
    except Exception as exc:
        print(f"{label}: NAT rule failed — {exc}", file=sys.stderr)
        print(
            f"       Hint: use --proxmox-ssh-key to specify a key authorized on "
            f"{proxmox_host}",
            file=sys.stderr,
        )
        return 1, None

    host_port = mapping.host_port
    if host_port is None:
        # add_rules() fills host_port on every mapping it returns, so reaching
        # this means that invariant broke. Say so rather than connecting to
        # port None.
        print(f"{label}: NAT rule came back without a host port", file=sys.stderr)
        return 1, mapping

    print(f"{label}: SSH {proxmox_host}:{host_port} -> {vm_ip}:22 ...")
    try:
        out = _ssh_exec(proxmox_host, host_port, ssh_user, ssh_key_path, "hostname")
        print(f"{label}: SSH ok  hostname={out}")
        return 0, mapping
    except Exception as exc:
        print(f"{label}: SSH FAILED — {exc}", file=sys.stderr)
        return 1, mapping


def main() -> None:
    """Run the end-to-end VM lifecycle check from the command line.

    Launches the requested VMs, verifies each one through the guest agent and,
    when an SSH key is available, through an injected NAT rule, then deletes
    them again. Exits with status 0 on success and a non-zero status when a
    launch, verification, or cleanup step fails.
    """
    parser = argparse.ArgumentParser(
        description="End-to-end Proxmox VM lifecycle test (create, verify, delete)."
    )
    parser.add_argument("--name", default=None, help="VM name prefix.")
    parser.add_argument(
        "--template-id", type=int, default=None, help="Template VMID to clone."
    )
    parser.add_argument(
        "--node", default=None, help="Proxmox node (default: PROXMOX_NODE env)."
    )
    parser.add_argument("--cores", type=int, default=None)
    parser.add_argument("--memory-mb", type=int, default=None)
    parser.add_argument("--disk-gb", type=int, default=None)
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Max wait seconds for launch and verification.",
    )
    parser.add_argument("--count", type=int, default=1, help="Number of VMs.")
    parser.add_argument(
        "--configs",
        default=None,
        help="JSON array of VmConfig objects. Mutually exclusive with --count.",
    )
    parser.add_argument(
        "--list-templates", action="store_true", help="List templates and exit."
    )
    parser.add_argument(
        "--ssh-key",
        default=None,
        help="Path to SSH private key (default: ~/.ssh/id_rsa). "
        "The corresponding .pub is injected into the VM via cloud-init.",
    )
    parser.add_argument(
        "--ssh-user",
        default="ubuntu",
        help="SSH user inside the VM (default: ubuntu).",
    )
    parser.add_argument(
        "--proxmox-ssh-key",
        default=None,
        help="Path to SSH private key authorized on the Proxmox host itself "
        "(default: same as --ssh-key). Required for NAT rule management via SSH.",
    )
    args = parser.parse_args()

    if args.configs and args.count != 1:
        raise SystemExit("--configs and --count are mutually exclusive")
    if args.count < 1:
        raise SystemExit("--count must be at least 1")

    host = _env_or_raise("PROXMOX_HOST")
    user = _env_or_raise("PROXMOX_USER")
    password = os.environ.get("PROXMOX_PASSWORD")
    token_name = os.environ.get("PROXMOX_TOKEN_NAME")
    token_value = os.environ.get("PROXMOX_TOKEN_VALUE")
    node = args.node or os.environ.get("PROXMOX_NODE")

    # SSH setup — optional; enabled only when the .pub key file is found.
    # Same PTH111 reasoning as in _load_ssh_pubkey: the value is user-supplied.
    ssh_key_path = args.ssh_key or os.path.expanduser("~/.ssh/id_rsa")  # noqa: PTH111
    proxmox_key_path = args.proxmox_ssh_key or ssh_key_path
    ssh_pubkey = _load_ssh_pubkey(args.ssh_key)
    ssh_enabled = ssh_pubkey is not None and Path(ssh_key_path).exists()
    proxmox_ssh_user = user.split("@")[0]  # "root@pam" -> "root"

    client = ProxmoxClient(
        host=host,
        user=user,
        password=password,
        token_name=token_name,
        token_value=token_value,
        node=node,
        verify_ssl=False,
    )

    if args.list_templates:
        templates = client.list_templates()
        print(f"{'VMID':>6}  {'Name':<30}  {'Node':<10}  {'Cores':>6}  {'Memory':>10}")
        print("-" * 75)
        for t in templates:
            print(
                f"{t.vm_id:>6}  {t.name:<30}  {t.node:<10}  "
                f"{t.cores:>6}  {t.memory_mb:>8}MB"
            )
        raise SystemExit(0)

    ci_config: CloudInitConfig | None = None
    if ssh_enabled and ssh_pubkey is not None:
        # ssh_enabled already implies ssh_pubkey is not None; restating it is
        # what lets the type checker narrow the value to str here.
        ci_config = CloudInitConfig(username=args.ssh_user, ssh_keys=[ssh_pubkey])

    if args.configs:
        try:
            raw = json.loads(args.configs)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--configs: invalid JSON — {exc}") from exc
        if not isinstance(raw, list) or not raw:
            raise SystemExit("--configs: expected a non-empty JSON array")
        configs = [VmConfig(**item) for item in raw]
    else:
        if not args.template_id:
            raise SystemExit("--template-id is required (or use --configs)")
        prefix = args.name or f"e2e-{int(time.time())}"
        if args.count == 1:
            configs = [
                VmConfig(
                    name=prefix,
                    template_id=args.template_id,
                    node=node,
                    cores=args.cores,
                    memory_mb=args.memory_mb,
                    disk_gb=args.disk_gb,
                    cloud_init_config=ci_config,
                )
            ]
        else:
            configs = [
                VmConfig(
                    name=f"{prefix}-{i}",
                    template_id=args.template_id,
                    node=node,
                    cores=args.cores,
                    memory_mb=args.memory_mb,
                    disk_gb=args.disk_gb,
                    cloud_init_config=ci_config,
                )
                for i in range(args.count)
            ]

    n = len(configs)
    vms: list[ProxmoxVM] = []
    ips: list[str | None] = []
    nat_mappings: list[PortMapping] = []
    exit_code = 0

    try:
        if n == 1:
            print(f"[1/3] Launching VM '{configs[0].name}' ...")
            if ssh_enabled:
                print(f"       cloud-init: injecting SSH key for '{args.ssh_user}'")
        else:
            print(f"[1/3] Launching {n} VMs in parallel ...")
            for cfg in configs:
                print(f"       - {cfg.name}")
        t0 = time.monotonic()
        vms = client.launch_many(configs, timeout=args.timeout)
        dt = time.monotonic() - t0
        print(
            f"       {'launch' if n == 1 else f'all {n} launches'} "
            f"completed in {dt:.1f}s"
        )

        print(f"[2/3] Verifying {n} VM(s) ...")
        for i, vm in enumerate(vms, start=1):
            rc, ip = _verify_vm(vm, i, n, args.timeout)
            ips.append(ip)
            if rc != 0:
                exit_code = 1

            if ssh_enabled:
                if ip is None:
                    print(
                        f"  [{i}/{n}] {vm.vm_id}: skipping SSH (no IP)", file=sys.stderr
                    )
                    exit_code = 1
                else:
                    rc2, mapping = _verify_ssh(
                        vm,
                        ip,
                        host,
                        proxmox_ssh_user,
                        args.ssh_user,
                        ssh_key_path,
                        proxmox_key_path,
                        i,
                        n,
                    )
                    if mapping is not None:
                        nat_mappings.append(mapping)
                    if rc2 != 0:
                        exit_code = 1

    except ProxmoxError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        exit_code = 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        exit_code = 130

    if vms:
        if n == 1:
            print(f"[3/3] Deleting VM '{vms[0].vm_id}' ...")
        else:
            print(f"[3/3] Deleting {len(vms)} VM(s) ...")
        try:
            if nat_mappings:
                print("       removing NAT rules ...")
                mgr = ProxmoxRoutingManager.from_key(
                    host, proxmox_ssh_user, proxmox_key_path
                )
                mgr.remove_rules(nat_mappings)
            for vm in vms:
                vm.stop()
                vm.delete(purge=True)
            print("       done.")
        except ProxmoxError as exc:
            print(f"       cleanup failed: {exc}", file=sys.stderr)
            exit_code = 3

    print()
    if exit_code == 0:
        print(f"SUCCESS — {n} VM(s) completed full lifecycle.")
    else:
        print(f"FAILURE (exit code {exit_code})")

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
