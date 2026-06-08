from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Header

from app.errors import api_error


JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret")


@dataclass(frozen=True)
class CurrentUser:
    user_id: str


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_access_token(claims: dict[str, Any], secret: str | None = None) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    key = (secret or JWT_SECRET).encode("utf-8")
    header_part = _b64url_encode(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    payload_part = _b64url_encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{header_part}.{payload_part}.{_b64url_encode(signature)}"


def decode_access_token(token: str, secret: str | None = None) -> dict[str, Any]:
    try:
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_b64url_decode(header_part))
        payload = json.loads(_b64url_decode(payload_part))
    except (ValueError, json.JSONDecodeError):
        raise api_error(401, "UNAUTHORIZED", "Invalid token")

    if header.get("alg") != "HS256":
        raise api_error(401, "UNAUTHORIZED", "Invalid token")

    key = (secret or JWT_SECRET).encode("utf-8")
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    expected = _b64url_encode(hmac.new(key, signing_input, hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature_part):
        raise api_error(401, "UNAUTHORIZED", "Invalid token")
    return payload


def require_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise api_error(401, "UNAUTHORIZED", "Authorization required")

    payload = decode_access_token(authorization.removeprefix("Bearer ").strip())
    user_id = payload.get("sub")
    if not isinstance(user_id, str):
        raise api_error(401, "UNAUTHORIZED", "sub claim is required")
    try:
        normalized_user_id = str(UUID(user_id))
    except ValueError:
        raise api_error(401, "UNAUTHORIZED", "sub claim must be a valid UUID")
    return CurrentUser(user_id=normalized_user_id)
