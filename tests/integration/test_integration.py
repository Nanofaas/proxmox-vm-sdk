"""Integration tests -- require a real Proxmox server.

Set env vars: PROXMOX_HOST, PROXMOX_USER, PROXMOX_PASSWORD, PROXMOX_NODE
Skip by default: pytest -m "not integration"
"""

import os
import uuid

import pytest

from proxmox_sdk import ProxmoxClient

pytestmark = pytest.mark.integration


def _env_or_skip(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    return value


@pytest.fixture
def client() -> ProxmoxClient:
    return ProxmoxClient(
        host=_env_or_skip("PROXMOX_HOST"),
        user=_env_or_skip("PROXMOX_USER"),
        password=os.environ.get("PROXMOX_PASSWORD"),
        node=os.environ.get("PROXMOX_NODE"),
        verify_ssl=False,
    )


def test_launch_and_delete(client: ProxmoxClient) -> None:
    name = f"test-{uuid.uuid4().hex[:8]}"
    templates = client.list_templates()
    if not templates:
        pytest.skip("no templates available")
    template_id = templates[0].vm_id

    vm = client.launch(name, template_id=template_id, start=True)
    try:
        vm.wait_for_agent(timeout=120)
        info = vm.info()
        assert info.name == name
        assert info.state.value == "running"
    finally:
        vm.delete(purge=True)
