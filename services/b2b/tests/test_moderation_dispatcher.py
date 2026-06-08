from typing import Any

from app.moderation import HttpModerationDispatcher


class FakeResponse:
    def __init__(self) -> None:
        self.raise_for_status_called = False

    def raise_for_status(self) -> None:
        self.raise_for_status_called = True


def test_http_dispatcher_uses_moderation_openapi_route(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    response = FakeResponse()

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return response

    monkeypatch.setattr("app.moderation.httpx.post", fake_post)
    payload = {
        "event_type": "PRODUCT_CREATED",
        "idempotency_key": "44f64783-21f8-4e98-b2ea-ad630efb88b7",
        "occurred_at": "2026-06-08T10:00:00.000Z",
        "payload": {
            "product_id": "79c66425-f714-4602-a65f-34598e391369",
            "seller_id": "71d80472-3ef3-43f8-9427-e97e2581c010",
            "json_after": {},
        },
    }

    dispatcher = HttpModerationDispatcher(
        base_url="http://moderation:8000/",
        service_key="test-service-key",
    )
    dispatcher.send_product_event(payload)

    assert captured["url"] == "http://moderation:8000/api/v1/b2b/events"
    assert captured["json"] == payload
    assert captured["headers"] == {"X-Service-Key": "test-service-key"}
    assert captured["timeout"] == 5.0
    assert response.raise_for_status_called is True
