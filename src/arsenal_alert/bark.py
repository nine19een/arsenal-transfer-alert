from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .db import StateStore
from .http_transport import HttpTransport, TransportError, UrllibTransport
from .models import NotificationPayload


class BarkDeliveryError(RuntimeError):
    pass


class BarkRetryableError(BarkDeliveryError):
    """The server explicitly rejected a transient request; retrying is safe."""


class BarkPermanentError(BarkDeliveryError):
    """The server explicitly rejected the notification permanently."""


class BarkDeliveryUncertain(BarkDeliveryError):
    """The request may have reached Bark; automatic retry risks a duplicate."""


class BarkClient:
    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        transport: HttpTransport | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.transport = transport or UrllibTransport()

    def send(self, payload: NotificationPayload) -> None:
        url = f"{self.settings.bark_base_url}/push"
        try:
            response = self.transport.request(
                "POST",
                url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": "arsenal-transfer-alert/0.1",
                },
                json_body=payload.as_bark_json(self.settings.bark_device_key),
                timeout=self.settings.bark_http_timeout_seconds,
            )
        except TransportError as error:
            self.store.record_bark_request("network_uncertain")
            raise BarkDeliveryUncertain(
                "Bark network outcome is uncertain; automatic retry is disabled"
            ) from error
        if response.status == 429 or response.status >= 500:
            self.store.record_bark_request(f"http_{response.status}_retryable")
            raise BarkRetryableError(f"temporary Bark HTTP {response.status}")
        if not 200 <= response.status < 300:
            self.store.record_bark_request(f"http_{response.status}_permanent")
            raise BarkPermanentError(f"Bark returned HTTP {response.status}")
        try:
            body: Any = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self.store.record_bark_request("invalid_response_uncertain")
            raise BarkDeliveryUncertain(
                "Bark accepted HTTP but returned invalid JSON; delivery is uncertain"
            ) from error
        if not isinstance(body, dict) or type(body.get("code")) is not int:
            self.store.record_bark_request("invalid_shape_uncertain")
            raise BarkDeliveryUncertain(
                "Bark response shape is invalid; delivery is uncertain"
            )
        code = body["code"]
        if code == 200:
            self.store.record_bark_request("ok")
            return
        if code == 429 or code >= 500:
            self.store.record_bark_request(f"code_{code}_retryable")
            raise BarkRetryableError(f"temporary Bark response code {code}")
        self.store.record_bark_request(f"code_{code}_permanent")
        raise BarkPermanentError(f"Bark rejected the notification with code {code}")
