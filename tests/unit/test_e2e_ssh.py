"""Tests for SSH-related helpers added to e2e.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from proxmox_sdk.e2e import _load_ssh_pubkey, _ssh_exec

# ---------------------------------------------------------------------------
# _load_ssh_pubkey
# ---------------------------------------------------------------------------


def test_load_ssh_pubkey_returns_none_when_pub_file_missing(tmp_path):
    result = _load_ssh_pubkey(str(tmp_path / "id_rsa"))
    assert result is None


def test_load_ssh_pubkey_reads_pub_file_content(tmp_path):
    key = tmp_path / "id_rsa"
    pub = tmp_path / "id_rsa.pub"
    pub.write_text("ssh-rsa AAAA test@host\n")
    result = _load_ssh_pubkey(str(key))
    assert result == "ssh-rsa AAAA test@host"


def test_load_ssh_pubkey_strips_trailing_whitespace(tmp_path):
    key = tmp_path / "id_rsa"
    pub = tmp_path / "id_rsa.pub"
    pub.write_text("ssh-rsa AAAA test@host   \n")
    assert _load_ssh_pubkey(str(key)) == "ssh-rsa AAAA test@host"


def test_load_ssh_pubkey_uses_default_path_when_none_given(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa.pub").write_text("ssh-rsa BBBB default@host")
    result = _load_ssh_pubkey(None)
    assert result == "ssh-rsa BBBB default@host"


def test_load_ssh_pubkey_returns_none_when_default_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    assert _load_ssh_pubkey(None) is None


# ---------------------------------------------------------------------------
# _ssh_exec
# ---------------------------------------------------------------------------


def test_ssh_exec_connects_with_key_and_returns_stdout():
    with patch("paramiko.SSHClient") as mock_ssh_client_cls:
        mock_client = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"test-vm\n"
        mock_client.exec_command.return_value = (None, mock_stdout, None)

        result = _ssh_exec(
            "192.168.1.1", 20001, "ubuntu", "/home/user/.ssh/id_rsa", "hostname"
        )

        assert result == "test-vm"
        mock_client.connect.assert_called_once_with(
            "192.168.1.1",
            port=20001,
            username="ubuntu",
            key_filename="/home/user/.ssh/id_rsa",
            timeout=15,
        )


def test_ssh_exec_closes_connection_after_success():
    with patch("paramiko.SSHClient") as mock_ssh_client_cls:
        mock_client = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"ok"
        mock_client.exec_command.return_value = (None, mock_stdout, None)

        _ssh_exec("host", 22, "root", "/key", "echo ok")

        mock_client.close.assert_called_once()


def test_ssh_exec_closes_connection_even_on_exec_error():
    with patch("paramiko.SSHClient") as mock_ssh_client_cls:
        mock_client = MagicMock()
        mock_ssh_client_cls.return_value = mock_client
        mock_client.exec_command.side_effect = OSError("pipe broken")

        with pytest.raises(OSError, match="pipe broken"):
            _ssh_exec("host", 22, "root", "/key", "hostname")

        mock_client.close.assert_called_once()
