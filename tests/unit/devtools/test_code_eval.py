"""Tests for the ``proxmox-eval`` static source checker.

``_check_ast`` reads a ``src/proxmox_sdk`` tree relative to the current working
directory, so every test that drives it builds a throwaway package tree and
chdirs into it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from proxmox_sdk.devtools.code_eval import Smell, _check_ast, format_report, main

BARE_EXCEPT_MSG = "Bare except: — catches KeyboardInterrupt and SystemExit"


def _write_tree(tmp_path: Path, files: dict[str, str]) -> None:
    """Create ``files`` (relative paths -> source) under ``src/proxmox_sdk``."""
    root = tmp_path / "src" / "proxmox_sdk"
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)


# ---------------------------------------------------------------------------
# _check_ast: bare except
# ---------------------------------------------------------------------------


def test_check_ast_flags_bare_except(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(
        tmp_path,
        {
            "mod.py": "def f():\n    try:\n        pass\n    except:\n        pass\n",
        },
    )
    monkeypatch.chdir(tmp_path)

    smells = _check_ast()

    assert smells == [
        Smell(
            category="bug",
            severity="high",
            file="src/proxmox_sdk/mod.py",
            line=4,
            message=BARE_EXCEPT_MSG,
        )
    ]


def test_check_ast_reports_one_smell_per_bare_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(
        tmp_path,
        {
            "mod.py": (
                "def f():\n"
                "    try:\n"
                "        pass\n"
                "    except:\n"
                "        pass\n"
                "    try:\n"
                "        pass\n"
                "    except:\n"
                "        pass\n"
            ),
        },
    )
    monkeypatch.chdir(tmp_path)

    smells = _check_ast()

    assert [s.line for s in smells] == [4, 8]
    assert all(s.message == BARE_EXCEPT_MSG for s in smells)


def test_check_ast_does_not_treat_broad_except_as_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(
        tmp_path,
        {
            "mod.py": (
                "def f():\n"
                "    try:\n"
                "        pass\n"
                "    except ValueError:\n"
                "        pass\n"
            )
        },
    )
    monkeypatch.chdir(tmp_path)

    assert _check_ast() == []


# ---------------------------------------------------------------------------
# _check_ast: broad except clauses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caught", ["Exception", "BaseException"])
def test_check_ast_flags_broad_except(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caught: str
) -> None:
    _write_tree(
        tmp_path,
        {
            "mod.py": (
                "def f():\n"
                "    try:\n"
                "        pass\n"
                f"    except {caught}:\n"
                "        pass\n"
            )
        },
    )
    monkeypatch.chdir(tmp_path)

    smells = _check_ast()

    assert smells == [
        Smell(
            category="bug",
            severity="medium",
            file="src/proxmox_sdk/mod.py",
            line=4,
            message=f"Broad except clause catches {caught}",
        )
    ]


def test_check_ast_ignores_narrow_except_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(
        tmp_path,
        {
            "mod.py": (
                "def f():\n"
                "    try:\n"
                "        pass\n"
                "    except (ValueError, KeyError):\n"
                "        pass\n"
            ),
        },
    )
    monkeypatch.chdir(tmp_path)

    assert _check_ast() == []


# ---------------------------------------------------------------------------
# _check_ast: mutable default arguments
# ---------------------------------------------------------------------------


def test_check_ast_flags_list_default_positional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(tmp_path, {"mod.py": "def f(items=[]):\n    return items\n"})
    monkeypatch.chdir(tmp_path)

    assert _check_ast() == [
        Smell(
            category="bug",
            severity="high",
            file="src/proxmox_sdk/mod.py",
            line=1,
            message="Mutable default argument in `f()`",
        )
    ]


def test_check_ast_flags_dict_and_set_keyword_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(
        tmp_path,
        {"mod.py": "def f(cache={}, *, seen={1, 2}):\n    return cache, seen\n"},
    )
    monkeypatch.chdir(tmp_path)

    messages = [s.message for s in _check_ast()]

    assert messages == [
        "Mutable default argument in `f()`",
        "Mutable default argument in `f()`",
    ]


def test_check_ast_ignores_immutable_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(
        tmp_path,
        {"mod.py": "def f(a=1, b='x', c=None, *, d=()):\n    return a, b, c, d\n"},
    )
    monkeypatch.chdir(tmp_path)

    assert _check_ast() == []


# ---------------------------------------------------------------------------
# _check_ast: long functions
# ---------------------------------------------------------------------------


def test_check_ast_flags_function_over_30_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1 def line + 31 body lines = 32 lines, one past the 30-line budget.
    _write_tree(tmp_path, {"mod.py": "def long_one():\n" + "    x = 1\n" * 31})
    monkeypatch.chdir(tmp_path)

    assert _check_ast() == [
        Smell(
            category="simplification",
            severity="medium",
            file="src/proxmox_sdk/mod.py",
            line=1,
            message="Function `long_one()` is 32 lines (max: 30)",
        )
    ]


def test_check_ast_allows_function_of_exactly_30_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1 def line + 29 body lines = 30 lines, exactly at the boundary.
    _write_tree(tmp_path, {"mod.py": "def edge():\n" + "    x = 1\n" * 29})
    monkeypatch.chdir(tmp_path)

    assert _check_ast() == []


def test_check_ast_flags_long_async_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(tmp_path, {"mod.py": "async def long_async():\n" + "    x = 1\n" * 31})
    monkeypatch.chdir(tmp_path)

    assert _check_ast() == [
        Smell(
            category="simplification",
            severity="medium",
            file="src/proxmox_sdk/mod.py",
            line=1,
            message="Function `long_async()` is 32 lines (max: 30)",
        )
    ]


# ---------------------------------------------------------------------------
# _check_ast: tree walking rules
# ---------------------------------------------------------------------------


def test_check_ast_skips_devtools_scripts_but_keeps_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(
        tmp_path,
        {
            "devtools/helper.py": "def f(items=[]):\n    return items\n",
            "devtools/__init__.py": "def f(items=[]):\n    return items\n",
        },
    )
    monkeypatch.chdir(tmp_path)

    smells = _check_ast()

    assert [s.file for s in smells] == ["src/proxmox_sdk/devtools/__init__.py"]


def test_check_ast_walks_nested_packages_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(
        tmp_path,
        {
            "a.py": "def f(items=[]):\n    return items\n",
            "sub/b.py": "def g(items=[]):\n    return items\n",
        },
    )
    monkeypatch.chdir(tmp_path)

    assert [s.file for s in _check_ast()] == [
        "src/proxmox_sdk/a.py",
        "src/proxmox_sdk/sub/b.py",
    ]


def test_check_ast_returns_empty_for_no_python_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src" / "proxmox_sdk").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    assert _check_ast() == []


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


def test_format_report_no_smells() -> None:
    assert format_report([]) == "No issues found."


def test_format_report_exact_layout() -> None:
    smells = [
        Smell("bug", "high", "a.py", 3, "boom"),
        Smell("simplification", "low", "b.py", 7, "meh"),
    ]

    assert format_report(smells) == (
        "1. Possible Bugs\n"
        "----------------\n"
        "  [HIGH] a.py:3 — boom\n"
        "\n"
        "2. Simplification Opportunities\n"
        "-------------------------------\n"
        "  [LOW] b.py:7 — meh\n"
    )


def test_format_report_orders_by_severity_within_two_item_section() -> None:
    smells = [
        Smell("bug", "low", "c.py", 9, "low finding"),
        Smell("bug", "high", "a.py", 1, "high finding"),
        Smell("bug", "medium", "b.py", 5, "medium finding"),
    ]

    lines = format_report(smells).splitlines()

    assert lines[2:5] == [
        "  [HIGH] a.py:1 — high finding",
        "  [MEDIUM] b.py:5 — medium finding",
        "  [LOW] c.py:9 — low finding",
    ]


def test_format_report_marks_empty_simplification_section() -> None:
    report = format_report([Smell("bug", "high", "a.py", 1, "x")])

    assert report == (
        "1. Possible Bugs\n"
        "----------------\n"
        "  [HIGH] a.py:1 — x\n"
        "\n"
        "2. Simplification Opportunities\n"
        "-------------------------------\n"
        "  (none)\n"
    )


def test_format_report_marks_empty_bug_section() -> None:
    report = format_report([Smell("simplification", "medium", "b.py", 2, "long")])

    assert report == (
        "1. Possible Bugs\n"
        "----------------\n"
        "  (none)\n"
        "\n"
        "2. Simplification Opportunities\n"
        "-------------------------------\n"
        "  [MEDIUM] b.py:2 — long\n"
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _findings_tree(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        {"mod.py": "def f():\n    try:\n        pass\n    except:\n        pass\n"},
    )


def test_main_prints_text_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _findings_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["proxmox-eval"])

    main()

    assert capsys.readouterr().out == (
        "1. Possible Bugs\n"
        "----------------\n"
        f"  [HIGH] src/proxmox_sdk/mod.py:4 — {BARE_EXCEPT_MSG}\n"
        "\n"
        "2. Simplification Opportunities\n"
        "-------------------------------\n"
        "  (none)\n"
        "\n"
    )


def test_main_prints_no_issues_message_when_tree_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src" / "proxmox_sdk").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["proxmox-eval"])

    main()

    assert capsys.readouterr().out == "No issues found.\n"


def test_main_json_flag_emits_findings_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _findings_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["proxmox-eval", "--json"])

    main()

    assert json.loads(capsys.readouterr().out) == [
        {
            "category": "bug",
            "severity": "high",
            "file": "src/proxmox_sdk/mod.py",
            "line": 4,
            "message": BARE_EXCEPT_MSG,
        }
    ]


def test_main_json_flag_prints_empty_array_when_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src" / "proxmox_sdk").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["proxmox-eval", "--json"])

    main()

    assert json.loads(capsys.readouterr().out) == []
