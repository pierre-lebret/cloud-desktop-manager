"""Contrat du service avec le vrai transport hcloud (HTTP intercepté par requests-mock)."""

from __future__ import annotations

import pytest

from rdpm.hetzner.errors import ReadOnlyError, to_user_error
from rdpm.hetzner.service import HcloudTransport, HetznerService

API = "https://api.hetzner.cloud/v1"


@pytest.fixture
def service():
    return HetznerService(HcloudTransport("test-token"))


def test_pagination_and_auth(service, requests_mock):
    requests_mock.get(f"{API}/volumes?page=1", json={"volumes": [], "meta": {"pagination": {"next_page": 2}}})
    requests_mock.get(f"{API}/volumes?page=2", json={"volumes": [], "meta": {"pagination": {"next_page": None}}})
    assert service._get_all("/volumes", "volumes") == []
    assert requests_mock.call_count == 2
    assert requests_mock.last_request.headers["Authorization"] == "Bearer test-token"


def test_get_actions_falls_back_to_single_lookups(service, requests_mock):
    requests_mock.get(f"{API}/actions", status_code=400,
                      json={"error": {"code": "invalid_input", "message": "nope"}})
    requests_mock.get(f"{API}/actions/7", json={"action": {"id": 7, "status": "running", "progress": 40}})
    states = service.get_actions([7])
    assert states[7].progress == 40


def test_missing_server_returns_none(service, requests_mock):
    requests_mock.get(f"{API}/servers/1", status_code=404,
                      json={"error": {"code": "not_found", "message": "server not found"}})
    assert service.get_server(1) is None


def test_error_translation(service, requests_mock):
    requests_mock.post(f"{API}/servers", status_code=412,
                       json={"error": {"code": "resource_unavailable", "message": "unavailable"}})
    with pytest.raises(Exception) as exc:
        service.create_server(name="rdpm-x", server_type="cpx32", image_id=1, location="nbg1", labels={})
    err = to_user_error(exc.value)
    assert err.code == "resource_unavailable" and "indisponible" in err.message


def test_readonly_blocks_writes(requests_mock):
    svc = HetznerService(HcloudTransport("t"), readonly=True)
    with pytest.raises(ReadOnlyError):
        svc.delete_server(1)
    assert requests_mock.call_count == 0


def test_verify_token_rejects_unauthorized(requests_mock):
    from rdpm.hetzner.errors import UserError
    requests_mock.get(f"{API}/servers", status_code=401,
                      json={"error": {"code": "unauthorized", "message": "unable to authenticate"}})
    with pytest.raises(UserError) as exc:
        HetznerService.verify_token("bad-token")
    assert exc.value.message == "Token refusé par Hetzner"
    assert requests_mock.last_request.headers["Authorization"] == "Bearer bad-token"


def test_set_token_switches_transport(service, requests_mock):
    requests_mock.get(f"{API}/servers", json={"servers": [], "meta": {"pagination": {"next_page": None}}})
    HetznerService.verify_token("good-token")
    service.set_token("new-token")
    service._get_all("/servers", "servers")
    assert requests_mock.last_request.headers["Authorization"] == "Bearer new-token"
