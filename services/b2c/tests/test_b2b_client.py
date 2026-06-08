from typing import Any

from app.b2b_client import HttpB2BClient


class FakeResponse:
    status_code = 200

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def json(self) -> Any:
        return self.payload


def test_http_b2b_client_uses_public_sku_routes(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse({"ok": True} if method == "get" else [])

    monkeypatch.setattr("app.b2b_client.httpx.request", fake_request)
    client = HttpB2BClient(
        base_url="http://b2b:8000/",
        service_key="test-service-key",
    )

    client.get_sku("660e8400-e29b-41d4-a716-446655440001")
    client.batch_skus(["660e8400-e29b-41d4-a716-446655440001"])

    assert calls[0]["method"] == "get"
    assert calls[0]["url"].endswith(
        "/api/v1/public/skus/660e8400-e29b-41d4-a716-446655440001"
    )
    assert calls[1]["method"] == "post"
    assert calls[1]["url"].endswith("/api/v1/public/skus/batch")
    assert calls[1]["json"] == {
        "sku_ids": ["660e8400-e29b-41d4-a716-446655440001"]
    }
    assert all(
        call["headers"] == {"X-Service-Key": "test-service-key"}
        for call in calls
    )
