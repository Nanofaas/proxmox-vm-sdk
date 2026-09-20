import subprocess

import pytest

from proxmox_vm_sdk.devtools import quality


def _completed(returncode: int) -> subprocess.CompletedProcess[list[str]]:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def test_main_runs_every_check_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []

    def fake_run(
        command: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[list[str]]:
        calls.append((command, check))
        return _completed(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    quality.main()

    assert calls == [
        (["uv", "run", "ruff", "check", "src/", "tests/"], False),
        (["uv", "run", "basedpyright"], False),
        (["uv", "run", "bandit", "-c", "pyproject.toml", "-r", "src", "-q"], False),
    ]


def test_main_writes_success_line_when_all_checks_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda command, *, check: _completed(0))

    result = quality.main()

    assert result is None
    assert capsys.readouterr().out == "Quality checks passed\n"


def test_main_raises_naming_the_failed_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(
        command: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[list[str]]:
        return _completed(1 if command[2] == "basedpyright" else 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        quality.main()

    assert str(exc_info.value) == "Quality checks failed: basedpyright"
    assert exc_info.value.code == "Quality checks failed: basedpyright"
    # A failed run must not also claim success on stdout.
    assert capsys.readouterr().out == ""


def test_main_lists_multiple_failures_in_check_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[list[str]]:
        calls.append(command)
        return _completed(2 if command[2] in {"ruff", "bandit"} else 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        quality.main()

    assert str(exc_info.value) == "Quality checks failed: ruff, bandit"
    # No short-circuit: a failing check must not stop the ones after it.
    assert calls == [
        ["uv", "run", "ruff", "check", "src/", "tests/"],
        ["uv", "run", "basedpyright"],
        ["uv", "run", "bandit", "-c", "pyproject.toml", "-r", "src", "-q"],
    ]


@pytest.mark.parametrize("returncode", [1, 2, 127])
def test_any_nonzero_exit_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    def fake_run(
        command: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[list[str]]:
        return _completed(returncode if command[2] == "ruff" else 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        quality.main()

    assert str(exc_info.value) == "Quality checks failed: ruff"
