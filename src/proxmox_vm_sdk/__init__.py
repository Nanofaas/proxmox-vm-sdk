"""proxmox-vm-sdk: Pythonic SDK for Proxmox VE VM management.

Mirrors the multipass-sdk API design:
  https://github.com/Nanofaas/multipass-vm-sdk
"""

from proxmox_vm_sdk._backend import (
    CommandResult,
    ParamikoSshBackend,
    ProxmoxBackend,
    ProxmoxerBackend,
    SshBackend,
)
from proxmox_vm_sdk.client import ProxmoxClient
from proxmox_vm_sdk.exceptions import (
    NodeNotFoundError,
    ProxmoxAPIError,
    ProxmoxAuthError,
    ProxmoxConnectionError,
    ProxmoxError,
    ProxmoxTimeoutError,
    SnapshotNotFoundError,
    TaskFailedError,
    VmNotFoundError,
    VmStateError,
)
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
from proxmox_vm_sdk.routing import PortMapping, ProxmoxRoutingManager
from proxmox_vm_sdk.testing import FakeBackend, FakeSshBackend
from proxmox_vm_sdk.vm import ProxmoxVM

# Grouped by kind rather than sorted alphabetically: the exception entries are
# in hierarchy order (base first), and losing that to satisfy RUF022 would trade
# real information for a cosmetic ordering guarantee.
__all__ = [  # noqa: RUF022
    # Entry points
    "ProxmoxClient",
    "ProxmoxVM",
    # Models
    "CloudInitConfig",
    "NodeInfo",
    "SnapshotInfo",
    "TaskInfo",
    "TemplateInfo",
    "VmConfig",
    "VmInfo",
    "VmMetrics",
    "VmState",
    # Exceptions
    "ProxmoxError",
    "ProxmoxAuthError",
    "ProxmoxConnectionError",
    "ProxmoxAPIError",
    "VmNotFoundError",
    "VmStateError",
    "NodeNotFoundError",
    "ProxmoxTimeoutError",
    "SnapshotNotFoundError",
    "TaskFailedError",
    # Backends
    "CommandResult",
    "FakeBackend",
    "FakeSshBackend",
    "ParamikoSshBackend",
    "ProxmoxBackend",
    "ProxmoxerBackend",
    "SshBackend",
    # Routing / NAT
    "ProxmoxRoutingManager",
    "PortMapping",
]
