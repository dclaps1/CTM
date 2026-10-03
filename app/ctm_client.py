"""Thin client for the CallTrackingMetrics REST API (v1).

Auth is HTTP Basic with the account's API access key / secret key.
Docs: https://developers.calltrackingmetrics.com/
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE_URL = "https://api.calltrackingmetrics.com"
TRUSTED_RECORDING_DOMAIN = "calltrackingmetrics.com"


class CTMError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class CTMClient:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        account_id: str | int,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.account_id = str(account_id)
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._sleep = sleep
        # httpx drops the Authorization header when a redirect leaves the origin,
        # so CTM credentials are never forwarded to the storage host recordings live on.
        self._http = httpx.Client(
            base_url=self.base_url,
            auth=httpx.BasicAuth(access_key, secret_key),
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    @classmethod
    def from_settings(cls, settings: Any) -> CTMClient:
        if not settings.ctm_configured:
            raise CTMError("CTM credentials are not configured (CTM_ACCESS_KEY / CTM_SECRET_KEY / CTM_ACCOUNT_ID)")
        return cls(
            settings.ctm_access_key,
            settings.ctm_secret_key,
            settings.ctm_account_id,
            settings.ctm_base_url,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CTMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------

    def _account_path(self, suffix: str) -> str:
        return f"/api/v1/accounts/{self.account_id}/{suffix}"

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise CTMError(f"CTM API {method} {path} failed: {exc}") from exc
                self._sleep(2**attempt)
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else 2**attempt)
                continue
            if resp.status_code >= 400:
                raise CTMError(
                    f"CTM API {method} {path} failed: HTTP {resp.status_code} {resp.text[:200]}",
                    status_code=resp.status_code,
                )
            return resp
        raise CTMError(f"CTM API {method} {path} failed after retries")  # pragma: no cover

    def _paginate(self, path: str, key: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            data = self._request("GET", path, params={**params, "page": page}).json()
            items = data.get(key) or []
            yield from items
            total_pages = int(data.get("total_pages") or 1)
            if not items or page >= total_pages:
                return
            page += 1

    # -- endpoints ---------------------------------------------------------

    def iter_calls(self, start: date, end: date, per_page: int = 100) -> Iterator[dict[str, Any]]:
        """All activity between two dates (inclusive), newest first."""
        yield from self._paginate(
            self._account_path("calls.json"),
            "calls",
            {"start_date": start.isoformat(), "end_date": end.isoformat(), "per_page": per_page},
        )

    def get_call(self, call_id: int | str) -> dict[str, Any]:
        data = self._request("GET", self._account_path(f"calls/{call_id}.json")).json()
        return data.get("call", data) if isinstance(data, dict) else data

    def iter_users(self, per_page: int = 100) -> Iterator[dict[str, Any]]:
        yield from self._paginate(self._account_path("users.json"), "users", {"per_page": per_page})

    def record_sale(
        self,
        call_id: int | str,
        *,
        name: str | None = None,
        score: int | None = None,
        conversion: bool | None = None,
        value: float | None = None,
    ) -> dict[str, Any]:
        """Create/update the call's sale record (CTM's built-in 1-5 score / conversion / value)."""
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if score is not None:
            payload["score"] = max(1, min(5, int(score)))
        if conversion is not None:
            payload["conversion"] = 1 if conversion else 0
        if value is not None:
            payload["value"] = value
        return self._request("POST", self._account_path(f"calls/{call_id}/sale"), data=payload).json()

    def is_trusted_recording_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        allowed = {TRUSTED_RECORDING_DOMAIN, (urlparse(self.base_url).hostname or "").lower()}
        return parsed.scheme == "https" and (
            host in allowed or host.endswith("." + TRUSTED_RECORDING_DOMAIN)
        )

    def open_recording(self, audio_url: str, range_header: str | None = None) -> httpx.Response:
        """Open a streaming response for a call recording. Caller must close() it."""
        if not self.is_trusted_recording_url(audio_url):
            raise CTMError(f"Refusing to fetch recording from untrusted host: {audio_url}")
        headers = {"Accept": "audio/*,*/*"}
        if range_header:
            headers["Range"] = range_header
        resp = self._http.send(self._http.build_request("GET", audio_url, headers=headers), stream=True)
        if resp.status_code >= 400:
            resp.close()
            raise CTMError(f"Recording fetch failed: HTTP {resp.status_code}", status_code=resp.status_code)
        return resp
