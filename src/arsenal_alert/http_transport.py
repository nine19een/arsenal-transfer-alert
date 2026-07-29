from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


class TransportError(RuntimeError):
    """A network failure without a trustworthy application response."""


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        timeout: float,
    ) -> HttpResponse:
        body = None
        request_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(
                json_body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(1_048_577),
                )
        except urllib.error.HTTPError as error:
            error_headers = error.headers or {}
            return HttpResponse(
                status=int(error.code),
                headers={key.lower(): value for key, value in error_headers.items()},
                body=error.read(1_048_577),
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
            raise TransportError(type(error).__name__) from error
