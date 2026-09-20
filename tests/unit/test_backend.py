from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from proxmox_vm_sdk import (
    CommandResult,
    FakeBackend,
    FakeSshBackend,
    ParamikoSshBackend,
    ProxmoxAPIError,
    ProxmoxBackend,
    ProxmoxerBackend,
    ProxmoxTimeoutError,
    SshBackend,
    TaskFailedError,
    VmNotFoundError,
)


def test_add_vm_seeding() -> None:
    fb = FakeBackend()
    fb.add_vm(100, name="test", status="stopped")
    result = fb.get("nodes/pve/qemu/100/status/current")
    assert result["vmid"] == 100
    assert result["name"] == "test"
    assert result["status"] == "stopped"


def test_list_vms_via_cluster_resources() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.add_vm(101)
    vms = fb.get("cluster/resources", type="vm")
    assert len(vms) == 2
    assert {v["vmid"] for v in vms} == {100, 101}


def test_start_transitions_status() -> None:
    fb = FakeBackend()
    fb.add_vm(100, status="stopped")
    fb.post("nodes/pve/qemu/100/status/start")
    vm = fb.get("nodes/pve/qemu/100/status/current")
    assert vm["status"] == "running"


def test_stop_transitions_status() -> None:
    fb = FakeBackend()
    fb.add_vm(100, status="running")
    fb.post("nodes/pve/qemu/100/status/stop")
    vm = fb.get("nodes/pve/qemu/100/status/current")
    assert vm["status"] == "stopped"


def test_delete_removes_vm() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.delete("nodes/pve/qemu/100")
    with pytest.raises(KeyError):
        fb.get("nodes/pve/qemu/100/status/current")


def test_clone_creates_new_vm() -> None:
    fb = FakeBackend()
    fb.add_vm(9000, name="template")
    fb.post("nodes/pve/qemu/9000/clone", newid=200, name="cloned")
    vms = fb.get("cluster/resources", type="vm")
    assert any(v["vmid"] == 200 for v in vms)


def test_snapshot_round_trip() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.post("nodes/pve/qemu/100/snapshots", snapname="snap-1")
    snaps = fb.get("nodes/pve/qemu/100/snapshots")
    assert any(s["name"] == "snap-1" for s in snaps)


def test_calls_tracking() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.get("nodes/pve/qemu/100/status/current")
    fb.post("nodes/pve/qemu/100/status/start")
    assert len(fb.calls) == 2
    assert fb.calls[0][0] == "GET"
    assert fb.calls[1][0] == "POST"


def test_assert_called_with_passes() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    fb.post("nodes/pve/qemu/100/status/start")
    fb.assert_called_with("POST", "nodes/pve/qemu/100/status/start")


def test_assert_called_with_fails() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    with pytest.raises(AssertionError):
        fb.assert_called_with("POST", "nodes/pve/qemu/100/status/start")


def test_vm_not_found_raises() -> None:
    fb = FakeBackend()
    with pytest.raises(VmNotFoundError):
        fb.post("nodes/pve/qemu/999/status/start")


def test_wait_for_task_resolves_instantly() -> None:
    fb = FakeBackend()
    fb.add_vm(100)
    upid = fb.post("nodes/pve/qemu/100/status/start")
    fb.wait_for_task("pve", upid)  # should not raise or block


# ---------------------------------------------------------------------------
# Models of the backend protocols themselves
# ---------------------------------------------------------------------------


def test_fake_and_real_backends_satisfy_their_protocols() -> None:
    assert isinstance(FakeBackend(), ProxmoxBackend)
    assert isinstance(ProxmoxerBackend(object()), ProxmoxBackend)
    assert isinstance(FakeSshBackend(), SshBackend)
    assert isinstance(ParamikoSshBackend.__new__(ParamikoSshBackend), SshBackend)


def test_object_missing_a_protocol_method_is_not_a_backend() -> None:
    class _Incomplete:
        def get(self, path: str, **params: Any) -> Any: ...
        def post(self, path: str, **data: Any) -> Any: ...
        def put(self, path: str, **data: Any) -> Any: ...
        def delete(self, path: str, **params: Any) -> Any: ...

    assert not isinstance(_Incomplete(), ProxmoxBackend)


# ---------------------------------------------------------------------------
# ProxmoxerBackend — path walking and proxmoxer exception translation
# ---------------------------------------------------------------------------

_HTTP_METHODS = ("get", "post", "put", "delete")


class _FakeProxmoxChain:
    """Shared state for one fake proxmoxer resource tree."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any]]] = []


class _FakeProxmoxResource:
    """Mimics proxmoxer's attribute/call chain, recording the path walked.

    Real proxmoxer extends the request URL through *both* attribute access
    (``api.nodes``) and calling (``api.nodes("pve")``), so this fake supports
    both and records the leaf path each HTTP verb is invoked on.
    """

    def __init__(self, chain: _FakeProxmoxChain, path: str = "") -> None:
        self._chain = chain
        self._path = path

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        if item in _HTTP_METHODS:

            def call(**kwargs: Any) -> Any:
                self._chain.calls.append((item.upper(), self._path, dict(kwargs)))
                if self._chain.error is not None:
                    raise self._chain.error
                return self._chain.response

            return call
        return _FakeProxmoxResource(self._chain, f"{self._path}/{item}".strip("/"))

    def __call__(self, resource_id: Any) -> _FakeProxmoxResource:
        return _FakeProxmoxResource(
            self._chain, f"{self._path}/{resource_id}".strip("/")
        )


def _fake_api(
    response: Any = None, error: Exception | None = None
) -> tuple[ProxmoxerBackend, _FakeProxmoxChain]:
    chain = _FakeProxmoxChain(response=response, error=error)
    return ProxmoxerBackend(_FakeProxmoxResource(chain)), chain


def test_proxmoxer_get_walks_the_path_and_passes_params() -> None:
    backend, chain = _fake_api(response={"status": "running"})
    result = backend.get("nodes/pve/qemu/100/status/current", foo="bar")
    assert result == {"status": "running"}
    assert chain.calls == [("GET", "nodes/pve/qemu/100/status/current", {"foo": "bar"})]


def test_proxmoxer_strips_leading_and_trailing_slashes() -> None:
    backend, chain = _fake_api(response=None)
    backend.get("/nodes/pve/")
    assert chain.calls == [("GET", "nodes/pve", {})]


def test_proxmoxer_post_put_delete_use_their_own_verbs() -> None:
    backend, chain = _fake_api(response=None)
    backend.post("nodes/pve/qemu/100/status/start", a=1)
    backend.put("nodes/pve/qemu/100/config", b=2)
    backend.delete("nodes/pve/qemu/100", c=3)
    assert chain.calls == [
        ("POST", "nodes/pve/qemu/100/status/start", {"a": 1}),
        ("PUT", "nodes/pve/qemu/100/config", {"b": 2}),
        ("DELETE", "nodes/pve/qemu/100", {"c": 3}),
    ]


class ResourceException(Exception):  # noqa: N818 - name is load-bearing
    """Stand-in for proxmoxer.core.ResourceException, matched by class name.

    ProxmoxerBackend._translate_exception dispatches on ``type(exc).__name__``
    so this double must keep proxmoxer's exact name, not an ...Error one.
    """

    def __init__(self, status_code: int, content: str) -> None:
        self.status_code = status_code
        self.content = content
        super().__init__(content)


def test_proxmoxer_404_becomes_vm_not_found() -> None:
    backend, _ = _fake_api(error=ResourceException(404, "no such thing"))
    with pytest.raises(VmNotFoundError) as excinfo:
        backend.get("nodes/pve/qemu/999/status/current")
    assert excinfo.value.identifier == "nodes/pve/qemu/999/status/current"


def test_proxmoxer_does_not_exist_message_becomes_vm_not_found() -> None:
    backend, _ = _fake_api(error=ResourceException(500, "VM 999 does not exist"))
    with pytest.raises(VmNotFoundError) as excinfo:
        backend.post("nodes/pve/qemu/999/status/start")
    assert excinfo.value.identifier == "nodes/pve/qemu/999/status/start"


def test_proxmoxer_other_api_error_carries_status_message_and_path() -> None:
    backend, _ = _fake_api(error=ResourceException(500, "internal error"))
    with pytest.raises(ProxmoxAPIError) as excinfo:
        backend.get("nodes/pve/version")
    assert excinfo.value.status_code == 500
    assert excinfo.value.path == "nodes/pve/version"
    assert excinfo.value.message == "internal error"
    assert str(excinfo.value) == (
        "Proxmox API error 500 at 'nodes/pve/version': internal error"
    )


def test_proxmoxer_resource_exception_without_content_uses_str() -> None:
    exc_type = type("ResourceException", (Exception,), {})
    backend, _ = _fake_api(error=exc_type("plain failure"))
    with pytest.raises(ProxmoxAPIError) as excinfo:
        backend.get("nodes/pve/version")
    assert excinfo.value.status_code == 0
    assert excinfo.value.message == "plain failure"


def test_proxmoxer_non_proxmoxer_exception_propagates_unchanged() -> None:
    backend, _ = _fake_api(error=ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        backend.get("nodes/pve/version")


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    clock = {"now": 0.0}
    monkeypatch.setattr("proxmox_vm_sdk._backend.time.monotonic", lambda: clock["now"])

    def advance(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr("proxmox_vm_sdk._backend.time.sleep", advance)
    return clock


def test_wait_for_task_returns_once_the_task_stops_ok() -> None:
    backend, chain = _fake_api(response={"status": "stopped", "exitstatus": "OK"})
    assert backend.wait_for_task("pve", "UPID:abc") is None
    assert chain.calls == [("GET", "nodes/pve/tasks/UPID:abc/status", {})]


def test_wait_for_task_raises_when_the_task_exit_status_is_not_ok() -> None:
    backend, _ = _fake_api(response={"status": "stopped", "exitstatus": "exit code 1"})
    with pytest.raises(TaskFailedError) as excinfo:
        backend.wait_for_task("pve", "UPID:abc")
    assert excinfo.value.upid == "UPID:abc"
    assert excinfo.value.exit_status == "exit code 1"


def test_wait_for_task_polls_until_the_deadline_then_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_clock(monkeypatch)
    backend, chain = _fake_api(response={"status": "running"})
    with pytest.raises(ProxmoxTimeoutError) as excinfo:
        backend.wait_for_task("pve", "UPID:abc", timeout=2)
    assert excinfo.value.timeout == 2
    assert excinfo.value.operation == "wait_for_task"
    assert str(excinfo.value) == "VM 0: 'wait_for_task' timed out after 2s"
    assert len(chain.calls) == 2


def test_wait_for_task_swallows_a_malformed_status_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_clock(monkeypatch)
    backend, chain = _fake_api(response=["not", "a", "dict"])
    with pytest.raises(ProxmoxTimeoutError):
        backend.wait_for_task("pve", "UPID:abc", timeout=1)
    assert len(chain.calls) == 1


# ---------------------------------------------------------------------------
# ParamikoSshBackend — paramiko is the true boundary, so it is mocked
# ---------------------------------------------------------------------------


def test_paramiko_backend_connects_with_every_credential() -> None:
    with patch("paramiko.SSHClient") as ssh_client_cls:
        client = ssh_client_cls.return_value
        ParamikoSshBackend(
            "pve.example.com",
            "root",
            ssh_key_path="/home/u/.ssh/id_rsa",
            password="secret",
            port=2222,
        )
    client.connect.assert_called_once_with(
        hostname="pve.example.com",
        username="root",
        port=2222,
        key_filename="/home/u/.ssh/id_rsa",
        password="secret",
    )
    policy = client.set_missing_host_key_policy.call_args[0][0]
    assert isinstance(policy, paramiko.AutoAddPolicy)


def test_paramiko_backend_treats_empty_key_and_password_as_absent() -> None:
    with patch("paramiko.SSHClient") as ssh_client_cls:
        client = ssh_client_cls.return_value
        ParamikoSshBackend("host", "root", ssh_key_path="", password="")
    assert client.connect.call_args.kwargs["key_filename"] is None
    assert client.connect.call_args.kwargs["password"] is None
    assert client.connect.call_args.kwargs["port"] == 22


def test_paramiko_backend_run_returns_exit_code_stdout_and_stderr() -> None:
    stdout = MagicMock()
    stdout.channel.recv_exit_status.return_value = 7
    stdout.read.return_value = b"out\n"
    stderr = MagicMock()
    stderr.read.return_value = b"boom\n"
    with patch("paramiko.SSHClient") as ssh_client_cls:
        client = ssh_client_cls.return_value
        client.exec_command.return_value = (None, stdout, stderr)
        backend = ParamikoSshBackend("host", "root")
        result = backend.run("systemctl restart pveproxy")
    assert result == (7, "out\n", "boom\n")
    client.exec_command.assert_called_once_with("systemctl restart pveproxy")


def test_paramiko_backend_read_file_reads_over_sftp() -> None:
    sftp = MagicMock()
    sftp.file.return_value.__enter__.return_value.read.return_value = b"contents"
    with patch("paramiko.SSHClient") as ssh_client_cls:
        client = ssh_client_cls.return_value
        client.open_sftp.return_value = sftp
        backend = ParamikoSshBackend("host", "root")
        content = backend.read_file("/etc/network/interfaces")
    assert content == "contents"
    sftp.file.assert_called_once_with("/etc/network/interfaces", "r")


def test_paramiko_backend_write_file_stages_then_moves() -> None:
    sftp = MagicMock()
    handle = sftp.file.return_value.__enter__.return_value
    stdout = MagicMock()
    stdout.channel.recv_exit_status.return_value = 0
    stdout.read.return_value = b""
    stderr = MagicMock()
    stderr.read.return_value = b""
    with patch("paramiko.SSHClient") as ssh_client_cls:
        client = ssh_client_cls.return_value
        client.open_sftp.return_value = sftp
        client.exec_command.return_value = (None, stdout, stderr)
        backend = ParamikoSshBackend("host", "root")
        backend.write_file("/etc/pve/x.cfg", "body")
    sftp.file.assert_called_once_with("/etc/pve/x.cfg.proxmox_vm_sdk_tmp", "w")
    handle.write.assert_called_once_with("body")
    sftp.close.assert_called_once()
    client.exec_command.assert_called_once_with(
        "mv /etc/pve/x.cfg.proxmox_vm_sdk_tmp /etc/pve/x.cfg"
    )


def test_paramiko_backend_close_closes_the_ssh_client() -> None:
    with patch("paramiko.SSHClient") as ssh_client_cls:
        client = ssh_client_cls.return_value
        backend = ParamikoSshBackend("host", "root")
        backend.close()
    client.close.assert_called_once()


def test_paramiko_backend_context_manager_returns_self_and_closes() -> None:
    with patch("paramiko.SSHClient") as ssh_client_cls:
        client = ssh_client_cls.return_value
        backend = ParamikoSshBackend("host", "root")
        with backend as entered:
            assert entered is backend
            client.close.assert_not_called()
        client.close.assert_called_once()


def test_paramiko_backend_without_paramiko_raises_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "paramiko", None)
    with pytest.raises(ImportError, match="paramiko is required for SSH operations"):
        ParamikoSshBackend("host", "root")


# ---------------------------------------------------------------------------
# CommandResult
# ---------------------------------------------------------------------------


def test_command_result_defaults_to_empty_output_and_success() -> None:
    result = CommandResult(0, "hello\n")
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.args is None
    assert result.success is True


def test_command_result_success_is_false_for_nonzero_exit() -> None:
    result = CommandResult(1, "", "boom", ["ls", "-l"])
    assert result.success is False
    assert result.args == ["ls", "-l"]
    assert result.stderr == "boom"


def test_command_result_repr_names_every_field() -> None:
    result = CommandResult(2, "a", "b", ["x"])
    assert (
        repr(result) == "CommandResult(exit_code=2, stdout='a', stderr='b', args=['x'])"
    )
