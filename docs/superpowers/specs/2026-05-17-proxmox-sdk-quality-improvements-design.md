# proxmox-sdk: Quality Improvements Design

Date: 2026-05-17
Status: approved

## Goal

Improve code quality, type safety, and CI/CD for `proxmox-sdk` without breaking the public API.

## Constraints

- Zero breaking changes to the public API
- All existing tests must continue to pass
- No new mandatory dependencies beyond `paramiko`

---

## Section 1: pyproject.toml — Dependencies & Coverage

**Changes to `pyproject.toml`:**

- Add `paramiko>=3.0` to `dependencies` (currently imported conditionally in `ParamikoSshBackend` but not declared)
- Add `[tool.coverage.report]` section with `fail_under = 80` to enforce minimum coverage
- No other structural changes

**Rationale:** `ParamikoSshBackend` is exported from `__init__.py` and referenced in `ProxmoxRoutingManager`. It's a real dependency, not optional. The coverage threshold prevents regression without blocking development.

---

## Section 2: Code Quality Fixes

### 2a — Ruff warnings

Two trivial fixes:
- `src/proxmox_sdk/routing.py:33`: remove unused `field` from `from dataclasses import dataclass, field`
- `tests/test_routing.py:154`: rename `first` to `_first` (unused variable)

### 2b — Use `urllib.parse.urlparse` in `_utils.py`

Replace string manipulation in `parse_proxmox_url` with `urllib.parse.urlparse`:

```python
from urllib.parse import urlparse


def parse_proxmox_url(api_url: str) -> tuple[str, int]:
    if "://" not in api_url:
        api_url = f"https://{api_url}"
    parsed = urlparse(api_url)
    return parsed.hostname or "localhost", parsed.port or 8006
```

Handles edge cases (path, query string, auth in URL) correctly and is more readable.

### 2c — `ProxmoxVM.exec()` improvements

- Make timeout configurable via parameter (was hardcoded `range(30)`)
- Add exponential backoff on polling interval
- Raise dedicated errors for guest agent failures (not silent timeout)
- Return non-zero exit code from API instead of defaulting to 0

### 2d — Fix `ParamikoSshBackend` ImportError message

Remove reference to `proxmox-sdk[ssh]` (which doesn't exist). Simple message: `"paramiko is required for SSH operations. Install it with: pip install paramiko"`.

---

## Section 3: CI/CD — GitHub Actions

New file `.github/workflows/ci.yml`:

- **Trigger:** push/PR to `master`
- **Lint job:** ruff check + mypy (strict mode)
- **Test job:** pytest with coverage on Python 3.11, 3.12, 3.13 matrix
- **Coverage gate:** `--cov-fail-under=80`

Uses `astral-sh/setup-uv@v5` for fast uv-based installs.

---

## Section 4: PEP 561 Marker

Add empty `src/proxmox_sdk/py.typed` so downstream projects inherit type annotations automatically.

---

## Section 5: README Badge

Add CI status badge at the top of README.md:

```markdown
[![CI](https://github.com/miciav/proxmox-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/miciav/proxmox-sdk/actions/workflows/ci.yml)
```

---

## Files Changed Summary

| File | Change |
|---|---|
| `pyproject.toml` | +paramiko dependency, +coverage config |
| `.github/workflows/ci.yml` | **New** — CI pipeline |
| `src/proxmox_sdk/_utils.py` | urlparse-based URL parsing |
| `src/proxmox_sdk/vm.py` | Configurable timeout + backoff in exec() |
| `src/proxmox_sdk/routing.py` | Remove unused import |
| `src/proxmox_sdk/backends/ssh.py` | Fix ImportError message |
| `src/proxmox_sdk/py.typed` | **New** — PEP 561 marker |
| `tests/test_routing.py` | Rename unused variable |
| `README.md` | Add CI badge |
