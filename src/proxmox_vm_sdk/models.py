"""Typed models for the data the SDK returns.

Each model is a plain dataclass mirroring one Proxmox API payload, and every
model that has a corresponding endpoint exposes a `from_api` classmethod that
builds it from the raw response dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import quote


class VmState(Enum):
    """Power state of a VM as reported by Proxmox."""

    RUNNING = "running"
    STOPPED = "stopped"
    PAUSED = "paused"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> VmState:
        return cls.UNKNOWN


@dataclass
class VmInfo:
    """Configuration and current state of a single VM."""

    vm_id: int
    name: str
    node: str
    state: VmState
    cpu_count: int
    memory_mb: int
    uptime_seconds: int
    ipv4: list[str] = field(default_factory=list)
    template: bool = False
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> VmInfo:
        """Map from cluster.resources or nodes/{n}/qemu/{id}/status/current response."""
        tags_raw = data.get("tags", "") or ""
        tags = [t.strip() for t in tags_raw.split(";") if t.strip()]
        maxmem = data.get("maxmem") or 0
        return cls(
            vm_id=int(data.get("vmid", 0)),
            name=data.get("name", ""),
            node=data.get("node", ""),
            state=VmState(data.get("status", "unknown")),
            cpu_count=int(
                data.get(
                    "cores",
                    data.get("maxcpu", data.get("cpus", data.get("cpu_count", 1))),
                )
            ),
            memory_mb=maxmem // (1024 * 1024) if maxmem else 0,
            uptime_seconds=int(data.get("uptime", 0)),
            ipv4=[],
            template=bool(data.get("template", False)),
            tags=tags,
        )


@dataclass
class VmMetrics:
    """Runtime CPU, memory, network and disk metrics for a single VM."""

    vm_id: int
    cpu_pct: float
    mem_used_bytes: int
    mem_total_bytes: int
    mem_used_pct: float
    net_in_bytes: int
    net_out_bytes: int
    disk_read_bytes: int
    disk_write_bytes: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> VmMetrics:
        """Map from cluster.resources VM entry (same shape as app.py vm_metrics)."""
        maxmem = data.get("maxmem") or 0
        mem = data.get("mem") or 0
        mem_pct = round(mem / maxmem * 100, 2) if maxmem else 0.0
        cpu_pct = round((data.get("cpu") or 0) * 100, 2)
        return cls(
            vm_id=int(data.get("vmid", 0)),
            cpu_pct=cpu_pct,
            mem_used_bytes=int(mem),
            mem_total_bytes=int(maxmem),
            mem_used_pct=mem_pct,
            net_in_bytes=int(data.get("netin", 0)),
            net_out_bytes=int(data.get("netout", 0)),
            disk_read_bytes=int(data.get("diskread", 0)),
            disk_write_bytes=int(data.get("diskwrite", 0)),
        )


@dataclass
class NodeInfo:
    """Status and capacity of one node in the cluster."""

    name: str
    status: str
    cpu_count: int
    memory_total_bytes: int
    memory_used_bytes: int
    uptime_seconds: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> NodeInfo:
        """Map from a `nodes` cluster resources entry."""
        return cls(
            name=data.get("node", ""),
            status=data.get("status", "unknown"),
            cpu_count=int(data.get("maxcpu", 0)),
            memory_total_bytes=int(data.get("maxmem", 0)),
            memory_used_bytes=int(data.get("mem", 0)),
            uptime_seconds=int(data.get("uptime", 0)),
        )


@dataclass
class SnapshotInfo:
    """Metadata for one snapshot of a VM."""

    name: str
    vm_id: int
    created: int
    description: str = ""
    parent: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any], vm_id: int = 0) -> SnapshotInfo:
        """Map from a `snapshots` listing entry.

        The listing omits the owning VM, so callers pass `vm_id` in.
        """
        return cls(
            name=data.get("name", ""),
            vm_id=vm_id,
            created=int(data.get("snaptime", 0)),
            description=data.get("description", ""),
            parent=data.get("parent") or None,
        )


@dataclass
class TemplateInfo:
    """A VM flagged as a template, with its hardware metadata."""

    vm_id: int
    name: str
    node: str
    description: str = ""
    cores: int = 0
    memory_mb: int = 0

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TemplateInfo:
        """Map from a cluster.resources entry whose template flag is set."""
        maxmem = data.get("maxmem") or 0
        return cls(
            vm_id=int(data.get("vmid", 0)),
            name=data.get("name", ""),
            node=data.get("node", ""),
            description=data.get("description", ""),
            cores=int(data.get("maxcpu", data.get("cpus", 0))),
            memory_mb=maxmem // (1024 * 1024) if maxmem else 0,
        )


@dataclass
class TaskInfo:
    """Status of an asynchronous Proxmox task, identified by its UPID."""

    upid: str
    node: str
    type: str
    status: str
    exit_status: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TaskInfo:
        """Map from a `nodes/{node}/tasks/{upid}/status` response."""
        return cls(
            upid=data.get("upid", ""),
            node=data.get("node", ""),
            type=data.get("type", ""),
            status=data.get("status", ""),
            exit_status=data.get("exitstatus") or None,
        )


@dataclass
class VmConfig:
    """Reusable VM launch configuration — mirrors azure-vm-sdk VmConfig."""

    name: str | None = None
    template_id: int | None = None
    node: str | None = None
    cores: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    cloud_init_config: CloudInitConfig | None = None
    start: bool = True


@dataclass
class CloudInitConfig:
    """Cloud-init configuration to apply to a VM after cloning.

    Maps to the Proxmox PUT /nodes/{node}/qemu/{vmid}/config endpoint.
    SSH keys are URL-encoded per Proxmox API requirements.

    Example — DHCP with SSH key::

        CloudInitConfig(
            username="ubuntu",
            ssh_keys=["ssh-rsa AAAA..."],
            ip_config="ip=dhcp",
        )

    Example — static IP::

        CloudInitConfig(
            username="ubuntu",
            ip_config="ip=10.0.0.5/24,gw=10.0.0.1",
            nameserver="8.8.8.8",
        )
    """

    username: str | None = None
    password: str | None = None
    ssh_keys: list[str] = field(default_factory=list)
    ip_config: str | None = None
    nameserver: str | None = None
    searchdomain: str | None = None

    def to_api_params(self) -> dict[str, Any]:
        """Serialize to Proxmox PUT /config keyword arguments."""
        params: dict[str, Any] = {}
        if self.username:
            params["ciuser"] = self.username
        if self.password:
            params["cipassword"] = self.password
        if self.ssh_keys:
            params["sshkeys"] = quote("\n".join(self.ssh_keys), safe="")
        if self.ip_config:
            params["ipconfig0"] = self.ip_config
        if self.nameserver:
            params["nameserver"] = self.nameserver
        if self.searchdomain:
            params["searchdomain"] = self.searchdomain
        return params
