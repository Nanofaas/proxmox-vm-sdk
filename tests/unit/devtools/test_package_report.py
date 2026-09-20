"""Tests for the import-graph package report.

``calculate_metrics`` and ``format_metrics_table`` are pure and are exercised
directly. ``main`` is driven through a fake ``grimp`` graph so no real package
scan is needed.
"""

from __future__ import annotations

import runpy
import sys
from dataclasses import FrozenInstanceError

import pytest

from proxmox_vm_sdk.devtools import package_report
from proxmox_vm_sdk.devtools.package_report import (
    ROOT_PACKAGE,
    ModuleMetrics,
    calculate_metrics,
    format_metrics_table,
    main,
)


class FakeGraph:
    """Minimal stand-in for a grimp graph."""

    def __init__(
        self,
        modules: set[str],
        imports: dict[str, set[str]] | None = None,
    ) -> None:
        self.modules = modules
        self._imports = imports or {}

    def find_modules_directly_imported_by(self, module: str) -> set[str]:
        return set(self._imports.get(module, ()))


# ---------------------------------------------------------------------------
# calculate_metrics
# ---------------------------------------------------------------------------


def test_calculate_metrics_ignores_edges_from_modules_outside_the_graph() -> None:
    metrics = calculate_metrics(
        modules=["proxmox_vm_sdk.a"],
        edges=[("proxmox_vm_sdk.unknown", "proxmox_vm_sdk.a")],
    )
    assert len(metrics) == 1
    assert metrics[0] == ModuleMetrics(
        module="a",
        internal_imports=0,
        outgoing_imports=0,
        incoming_imports=0,
        external_imports=0,
        instability=0.0,
    )


def test_calculate_metrics_counts_imports_leaving_the_package_as_external() -> None:
    metrics = calculate_metrics(
        modules=["proxmox_vm_sdk.a"],
        edges=[("proxmox_vm_sdk.a", "requests")],
    )
    assert metrics[0].external_imports == 1
    assert metrics[0].outgoing_imports == 0
    assert metrics[0].incoming_imports == 0


def test_calculate_metrics_counts_a_self_import_as_internal() -> None:
    metrics = calculate_metrics(
        modules=["proxmox_vm_sdk.a"],
        edges=[("proxmox_vm_sdk.a", "proxmox_vm_sdk.a")],
    )
    assert metrics[0].internal_imports == 1
    assert metrics[0].outgoing_imports == 0
    assert metrics[0].incoming_imports == 0


def test_calculate_metrics_records_both_sides_of_an_edge() -> None:
    metrics = calculate_metrics(
        modules=["proxmox_vm_sdk.a", "proxmox_vm_sdk.b"],
        edges=[("proxmox_vm_sdk.a", "proxmox_vm_sdk.b")],
    )
    a, b = metrics
    assert (a.outgoing_imports, a.incoming_imports, a.instability) == (1, 0, 1.0)
    assert (b.outgoing_imports, b.incoming_imports, b.instability) == (0, 1, 0.0)


def test_calculate_metrics_rounds_instability_to_two_decimals() -> None:
    metrics = calculate_metrics(
        modules=[
            "proxmox_vm_sdk.a",
            "proxmox_vm_sdk.b",
            "proxmox_vm_sdk.c",
            "proxmox_vm_sdk.d",
        ],
        edges=[
            ("proxmox_vm_sdk.b", "proxmox_vm_sdk.a"),
            ("proxmox_vm_sdk.c", "proxmox_vm_sdk.a"),
            ("proxmox_vm_sdk.a", "proxmox_vm_sdk.d"),
        ],
    )
    assert metrics[0].instability == 0.33


def test_calculate_metrics_returns_zero_instability_without_coupling() -> None:
    metrics = calculate_metrics(modules=["proxmox_vm_sdk.a"], edges=[])
    assert metrics[0].instability == 0.0


def test_calculate_metrics_strips_the_root_prefix_but_keeps_the_root_name() -> None:
    metrics = calculate_metrics(
        modules=[ROOT_PACKAGE, "proxmox_vm_sdk.dev.tooling"],
        edges=[],
    )
    assert [m.module for m in metrics] == [ROOT_PACKAGE, "dev.tooling"]


def test_calculate_metrics_preserves_the_given_module_order() -> None:
    metrics = calculate_metrics(
        modules=["proxmox_vm_sdk.b", "proxmox_vm_sdk.a"],
        edges=[],
    )
    assert [m.module for m in metrics] == ["b", "a"]


def test_module_metrics_is_frozen() -> None:
    metric = ModuleMetrics(
        module="a",
        internal_imports=0,
        outgoing_imports=0,
        incoming_imports=0,
        external_imports=0,
        instability=0.0,
    )
    with pytest.raises(FrozenInstanceError):
        metric.module = "b"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# format_metrics_table
# ---------------------------------------------------------------------------


DEBUG_ROW = (
    "debug                                 0        1        2        3        0.33"
)


def test_format_metrics_table_renders_header_rule_and_rows() -> None:
    table = format_metrics_table(
        [
            ModuleMetrics(
                module="debug",
                internal_imports=0,
                outgoing_imports=1,
                incoming_imports=2,
                external_imports=3,
                instability=0.33,
            )
        ]
    )
    assert table == (
        "module                         internal outgoing incoming external instability"
        + "\n"
        + "-" * 78
        + "\n"
        + DEBUG_ROW
    )


def test_format_metrics_table_without_modules_is_header_and_rule_only() -> None:
    table = format_metrics_table([])
    lines = table.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("module")
    assert lines[1] == "-" * 78


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

WITH_EDGES = {
    "proxmox_vm_sdk": {"proxmox_vm_sdk.core"},
    "proxmox_vm_sdk.core": {"proxmox_vm_sdk.util"},
}
MODULES = {
    "proxmox_vm_sdk",
    "proxmox_vm_sdk.core",
    "proxmox_vm_sdk.util",
}

TABLE = (
    "module                         internal outgoing incoming external instability\n"
    + "-" * 78
    + "\n"
    + "proxmox_vm_sdk                        0        1        0        0        1.00\n"
    + "core                                  0        1        1        0        0.50\n"
    + "util                                  0        0        1        0        0.00"
)


def _install_graph(monkeypatch: pytest.MonkeyPatch, graph: FakeGraph) -> list[tuple]:
    calls: list[tuple] = []

    def fake_build_graph(
        root: str, *, include_external_packages: bool = True
    ) -> FakeGraph:
        calls.append((root, include_external_packages))
        return graph

    monkeypatch.setattr(package_report.grimp, "build_graph", fake_build_graph)
    return calls


def test_main_builds_the_graph_for_the_root_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_graph(monkeypatch, FakeGraph(set()))
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report"])

    main()

    assert calls == [(ROOT_PACKAGE, False)]


def test_main_prints_the_metrics_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_graph(monkeypatch, FakeGraph(MODULES, WITH_EDGES))
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report"])

    main()

    assert capsys.readouterr().out == TABLE + "\n"


def test_main_excludes_the_devtools_modules_from_the_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    modules = MODULES | {
        "proxmox_vm_sdk.devtools.package_report",
        "proxmox_vm_sdk.devtools.quality",
    }
    _install_graph(monkeypatch, FakeGraph(modules, WITH_EDGES))
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report"])

    main()

    out = capsys.readouterr().out
    assert out == TABLE + "\n"
    assert "package_report" not in out
    assert "quality" not in out


def test_main_edges_flag_lists_dependency_edges(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_graph(monkeypatch, FakeGraph(MODULES, WITH_EDGES))
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report", "--edges"])

    main()

    out = capsys.readouterr().out
    assert out == (
        TABLE + "\n\n[Dependency edges]\n  proxmox_vm_sdk -> core\n  core -> util\n"
    )


def test_main_edges_flag_skips_edges_touching_excluded_modules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    modules = MODULES | {"proxmox_vm_sdk.devtools.package_report"}
    imports = dict(WITH_EDGES)
    imports["proxmox_vm_sdk.util"] = {"proxmox_vm_sdk.devtools.package_report"}
    _install_graph(monkeypatch, FakeGraph(modules, imports))
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report", "--edges"])

    main()

    out = capsys.readouterr().out
    assert "  core -> util\n" in out
    assert "package_report" not in out


def test_main_orphans_flag_lists_modules_with_no_dependencies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    modules = MODULES | {"proxmox_vm_sdk.orphan"}
    _install_graph(monkeypatch, FakeGraph(modules, WITH_EDGES))
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report", "--orphans"])

    main()

    out = capsys.readouterr().out
    assert out.endswith("\n[Orphan modules]\n  orphan\n")


def test_main_orphans_flag_prints_the_root_package_unshortened(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    imports = dict(WITH_EDGES)
    imports.pop("proxmox_vm_sdk")
    _install_graph(monkeypatch, FakeGraph(MODULES, imports))
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report", "--orphans"])

    main()

    out = capsys.readouterr().out
    assert out.endswith("\n[Orphan modules]\n  proxmox_vm_sdk\n")


def test_main_orphans_flag_ignores_edges_touching_excluded_modules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    modules = MODULES | {"proxmox_vm_sdk.devtools.package_report"}
    imports = dict(WITH_EDGES)
    imports["proxmox_vm_sdk.util"] = {"proxmox_vm_sdk.devtools.package_report"}
    _install_graph(monkeypatch, FakeGraph(modules, imports))
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report", "--orphans"])

    main()

    out = capsys.readouterr().out
    assert "[Orphan modules]" not in out


def test_main_orphans_flag_omits_the_section_when_nothing_is_orphaned(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_graph(monkeypatch, FakeGraph(MODULES, WITH_EDGES))
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report", "--orphans"])

    main()

    out = capsys.readouterr().out
    assert "[Orphan modules]" not in out
    assert out == TABLE + "\n"


def test_main_runs_when_executed_as_a_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        package_report.grimp, "build_graph", lambda *a, **k: FakeGraph(set())
    )
    monkeypatch.setattr(sys, "argv", ["proxmox-package-report"])

    runpy.run_path(package_report.__file__, run_name="__main__")

    assert capsys.readouterr().out == format_metrics_table([]) + "\n"
