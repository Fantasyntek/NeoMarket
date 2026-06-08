import hmac
import os


INCOMING_B2B_SERVICE_KEY = os.getenv(
    "B2B_TO_MODERATION_SERVICE_KEY",
    os.getenv("MODERATION_SERVICE_KEY", "dev-service-key"),
)


def is_valid_b2b_service_key(value: str | None) -> bool:
    return bool(value) and hmac.compare_digest(value, INCOMING_B2B_SERVICE_KEY)
