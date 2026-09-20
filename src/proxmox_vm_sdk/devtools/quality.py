"""Run the project's lint, type and security checks in sequence.

Backs the ``proxmox-quality`` console script: each command in ``CHECKS`` runs
to completion and the ones that exit non-zero are collected and reported. The
list mirrors what pre-commit runs in CI, so the script cannot report success
while the pipeline fails.
"""

from __future__ import annotations

import subprocess
import sys

CHECKS = (
    ("ruff", ["uv", "run", "ruff", "check", "src/", "tests/"]),
    ("basedpyright", ["uv", "run", "basedpyright"]),
    ("bandit", ["uv", "run", "bandit", "-c", "pyproject.toml", "-r", "src", "-q"]),
)


def main() -> None:
    """Run every check in `CHECKS` and fail if any of them did.

    Each command is run with its output left on the terminal. A run in which
    every command exits zero reports success on stdout; otherwise the names of
    the failed checks are raised as a `SystemExit` message so the console
    script exits non-zero.
    """
    failures: list[str] = []
    for name, command in CHECKS:
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            failures.append(name)

    if failures:
        joined = ", ".join(failures)
        raise SystemExit(f"Quality checks failed: {joined}")

    sys.stdout.write("Quality checks passed\n")
