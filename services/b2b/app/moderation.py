from __future__ import annotations

import os
from typing import Any, Protocol

import httpx


MODERATION_URL = os.getenv("MODERATION_URL", "http://moderation:8000")
MODERATION_SERVICE_KEY = os.getenv("MODERATION_SERVICE_KEY", "dev-service-key")


class ModerationDispatcher(Protocol):
    def send_product_event(self, payload: dict[str, Any]) -> None:
        pass


class HttpModerationDispatcher:
    def __init__(
        self,
        base_url: str = MODERATION_URL,
        service_key: str = MODERATION_SERVICE_KEY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key

    def send_product_event(self, payload: dict[str, Any]) -> None:
        response = httpx.post(
            f"{self.base_url}/api/v1/events/product",
            json=payload,
            headers={"X-Service-Key": self.service_key},
            timeout=5.0,
        )
        response.raise_for_status()


def get_moderation_dispatcher() -> ModerationDispatcher:
    return HttpModerationDispatcher()

