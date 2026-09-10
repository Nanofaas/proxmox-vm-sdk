# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Pythonic SDK for managing Proxmox VE virtual machines: a higher-level API over
the official REST API, plus SSH-based NAT rule management on the Proxmox host.
It ships real backends, an in-memory fake for tests, cloud-init support,
snapshots and guest-agent exec. It is consumed by `nanolab` and `sonata-tasks`
via a git-pinned dependency, so the public API in `proxmox_sdk.__all__` is a
contract with those projects.

## Setup

```bash
uv sync
```

Requires [uv](https://docs.astral.sh/uv/). A Proxmox server is NOT required for
the unit tests.

The toolchain (ruff, basedpyright, bandit, pytest, pre-commit) lives in
`[dependency-groups].dev`, which a plain `uv sync` installs. It must stay there
and not move to `[project.optional-dependencies]`: extras are skipped by
`uv run`, so the basedpyright pre-commit hook — which runs `uv run --frozen
basedpyright` — would fail in CI with "Failed to spawn: basedpyright".

## Commands

```bash
# Tests (the integration marker is deselected by addopts)
uv run pytest
uv run pytest tests/unit/test_vm.py::test_name -v

# Integration tests — need a real Proxmox server
uv run pytest -m integration -v

# Lint, format, types, security — the same hooks CI runs
uv run ruff check .
uv run ruff check --fix .
uv run ruff format .
uv run basedpyright
uv run bandit -c pyproject.toml -r src
uv run pre-commit run --all-files
```

`proxmox-quality` runs ruff and basedpyright together and exits non-zero if
either fails.

## Architecture

`src/proxmox_sdk/` contains the package:

- `_backend.py` — the `ProxmoxBackend` protocol plus its two real
  implementations: `ProxmoxerBackend` (REST via `proxmoxer`) and
  `ParamikoSshBackend` (SSH). `CommandResult` is the normalised result type.
  All network access goes through a backend, which is what makes the SDK
  testable.
- `_utils.py` — `parse_proxmox_url`, used by `ProxmoxClient.from_url`.
- `models.py` — dataclasses (`VmConfig`, `VmInfo`, `VmMetrics`, `TemplateInfo`,
  `NodeInfo`, `SnapshotInfo`, `CloudInitConfig`, `CommandResult`, …).
- `exceptions.py` — typed hierarchy rooted at `ProxmoxError`, so callers can
  catch a specific failure rather than an opaque API error.
- `vm.py` — `ProxmoxVM`: per-VM operations (info, metrics, start/stop/shutdown/
  restart, delete, clone, snapshot/restore/list_snapshots, resize_disk,
  configure_cloud_init, exec, exec_structured, wait_for_agent, wait_for_ip,
  wait_ready).
- `client.py` — `ProxmoxClient`: cluster-level operations (list, get_vm,
  launch, launch_many, ensure_running, purge, list_nodes, list_templates,
  find_template).
- `routing.py` — `ProxmoxRoutingManager` and `PortMapping`: DNAT rules for
  host→VM port forwarding, written into `/etc/network/interfaces` on the
  Proxmox host over SSH and reloaded with `ifreload --all`.
- `testing.py` — `FakeBackend` and `FakeSshBackend`. `FakeBackend` is an
  in-memory stand-in for the Proxmox REST API; its dispatch chain is annotated
  with the route each branch answers, which is why ERA001 is disabled for that
  file (see the per-file-ignores in `pyproject.toml`).
- `e2e.py` — the `proxmox-vm-e2e` console script: an end-to-end lifecycle
  harness. Not imported by the library.
- `devtools/` — repository tooling exposed as console scripts:
  `proxmox-quality` (ruff + basedpyright), `proxmox-package-report` (import
  graph metrics via `grimp` — this is why grimp is a direct dependency),
  `proxmox-eval` (AST smell detection).

`ProxmoxClient` constructs `ProxmoxVM` instances and passes its backend down to
them. Tests inject `FakeBackend` instead of `ProxmoxerBackend`.

## Callers must not be broken

`nanolab` and `sonata-tasks` consume this package through a git pin, so a change
to a name in `__all__`, to a signature, or to a returned model's attributes is a
breaking change for them even though nothing in this repository fails. Check
those repositories before renaming or removing a public symbol.

## Conventions

- Python 3.11+, `from __future__ import annotations` at the top of every module.
- ruff for lint and format (88 columns, double quotes), basedpyright for types.
- Every module, class and public function carries a real docstring: the summary
  goes on the first line (D212), then a blank line, then any detail.
- Tests live in `tests/unit/`; `tests/integration/` needs a live server and is
  excluded by default.
