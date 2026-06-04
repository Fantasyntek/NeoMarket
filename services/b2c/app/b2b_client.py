from __future__ import annotations

import os
from typing import Any, Protocol

import httpx


B2B_URL = os.getenv("B2B_URL", "http://b2b:8000")
B2B_SERVICE_KEY = os.getenv("B2B_SERVICE_KEY", "dev-service-key")


class B2BUnavailableError(Exception):
    pass


class B2BResponseError(Exception):
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload


class B2BClient(Protocol):
    def list_products(self, params: dict[str, Any]) -> dict[str, Any]:
        pass

    def get_product(self, product_id: str) -> dict[str, Any]:
        pass


class HttpB2BClient:
    def __init__(
        self,
        base_url: str = B2B_URL,
        service_key: str = B2B_SERVICE_KEY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key

    def list_products(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{self.base_url}/api/v1/products",
                params=params,
                headers={"X-Service-Key": self.service_key},
                timeout=5.0,
            )
        except httpx.RequestError as exc:
            raise B2BUnavailableError from exc

        if response.status_code >= 500:
            raise B2BUnavailableError
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"code": "B2B_ERROR", "message": "B2B request failed"}
            raise B2BResponseError(response.status_code, payload)
        return response.json()

    def get_product(self, product_id: str) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{self.base_url}/api/v1/products/{product_id}",
                headers={"X-Service-Key": self.service_key},
                timeout=5.0,
            )
        except httpx.RequestError as exc:
            raise B2BUnavailableError from exc

        if response.status_code >= 500:
            raise B2BUnavailableError
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"code": "B2B_ERROR", "message": "B2B request failed"}
            raise B2BResponseError(response.status_code, payload)
        return response.json()


def get_b2b_client() -> B2BClient:
    return HttpB2BClient()
