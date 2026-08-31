import asyncio
import ipaddress

import pytest

from app.security.network_security import NetworkPolicyError, guarded_request, validate_outbound_url


class FakeResponse:
    def __init__(self, status_code: int = 200, *, headers: dict[str, str] | None = None, content: bytes = b"ok") -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls = 0

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)

    async def delete(self, *_args, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_metadata_and_private_addresses_are_denied() -> None:
    with pytest.raises(NetworkPolicyError):
        asyncio.run(validate_outbound_url("http://169.254.169.254/latest", purpose="test"))
    with pytest.raises(NetworkPolicyError):
        asyncio.run(validate_outbound_url("http://127.0.0.1:9000", purpose="test"))


def test_explicit_private_access_only_allows_non_metadata_private_host() -> None:
    result = asyncio.run(validate_outbound_url("http://127.0.0.1:9000", purpose="local_mcp", allow_private=True))
    assert result.host == "127.0.0.1"


def test_private_opt_in_does_not_allow_public_plain_http(monkeypatch) -> None:
    async def resolve(_host: str, _port: int):
        return (ipaddress.ip_address("8.8.8.8"),)

    monkeypatch.setattr("app.security.network_security._resolve_host", resolve)
    with pytest.raises(NetworkPolicyError, match="Plain HTTP"):
        asyncio.run(validate_outbound_url("http://public.example", purpose="local_mcp", allow_private=True))


def test_dns_rebinding_to_private_address_is_denied(monkeypatch) -> None:
    async def resolve(_host: str, _port: int):
        return (ipaddress.ip_address("10.0.0.7"),)

    monkeypatch.setattr("app.security.network_security._resolve_host", resolve)
    with pytest.raises(NetworkPolicyError, match="private"):
        asyncio.run(validate_outbound_url("https://public.example", purpose="test"))


def test_cross_origin_redirect_cannot_forward_credentials(monkeypatch) -> None:
    async def resolve(_host: str, _port: int):
        return (ipaddress.ip_address("8.8.8.8"),)

    monkeypatch.setattr("app.security.network_security._resolve_host", resolve)
    client = FakeClient([FakeResponse(302, headers={"Location": "https://other.example/result"})])
    with pytest.raises(NetworkPolicyError, match="credentials"):
        asyncio.run(guarded_request(client, "GET", "https://first.example", purpose="test", headers={"Authorization": "Bearer secret"}))
    assert client.calls == 1


def test_response_size_and_attachment_are_denied(monkeypatch) -> None:
    async def resolve(_host: str, _port: int):
        return (ipaddress.ip_address("8.8.8.8"),)

    monkeypatch.setattr("app.security.network_security._resolve_host", resolve)
    large = FakeClient([FakeResponse(content=b"x" * 20)])
    with pytest.raises(NetworkPolicyError, match="size"):
        asyncio.run(guarded_request(large, "GET", "https://safe.example", purpose="test", max_response_bytes=10))
    attachment = FakeClient([FakeResponse(headers={"Content-Disposition": "attachment; filename=x.bin"})])
    with pytest.raises(NetworkPolicyError, match="Attachment"):
        asyncio.run(guarded_request(attachment, "GET", "https://safe.example", purpose="test"))


def test_delete_is_limited_to_mcp_session_termination(monkeypatch) -> None:
    async def resolve(_host: str, _port: int):
        return (ipaddress.ip_address("8.8.8.8"),)

    monkeypatch.setattr("app.security.network_security._resolve_host", resolve)
    forbidden = FakeClient([FakeResponse(status_code=204)])
    with pytest.raises(NetworkPolicyError, match="method"):
        asyncio.run(guarded_request(forbidden, "DELETE", "https://safe.example", purpose="test"))
    assert forbidden.calls == 0

    close = FakeClient([FakeResponse(status_code=204)])
    response = asyncio.run(
        guarded_request(close, "DELETE", "https://safe.example", purpose="remote_mcp_close")
    )
    assert response.status_code == 204
    assert close.calls == 1
