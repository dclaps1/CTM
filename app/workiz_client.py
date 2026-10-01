"""Thin read-only client for the Workiz REST API (v1).

Workiz puts the API token in the URL path: https://api.workiz.com/api/v1/{token}/job/all/
Each franchise has its own Workiz account, so tokens are read per market from environment variables:

    WORKIZ_TOKEN_BOSTON, WORKIZ_TOKEN_CHARLESTON, ...   (and optional WORKIZ_SECRET_<MARKET>)
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import httpx

BASE_URL = "https://api.workiz.com/api/v1"

# market display name (as used in app.brief) -> environment variable suffix
MARKET_ENV = {
    "Greater Boston": "BOSTON",
    "Charleston": "CHARLESTON",
    "South Atlanta": "SOUTH_ATLANTA",
    "Lehigh Valley-Poconos": "LEHIGH_VALLEY",
    "Grand Rapids": "GRAND_RAPIDS",
    "Richmond": "RICHMOND",
    "Greenville": "GREENVILLE",
    "Ann Arbor": "ANN_ARBOR",
    "South Kansas City": "SOUTH_KANSAS_CITY",
    "Martinsburg & Winchester": "MARTINSBURG",
}


class WorkizError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def configured_markets(env: dict[str, str] | None = None) -> dict[str, str]:
    """{market: token} for every market that has WORKIZ_TOKEN_<MARKET> set."""
    env = env if env is not None else dict(os.environ)
    return {m: env[f"WORKIZ_TOKEN_{s}"] for m, s in MARKET_ENV.items() if env.get(f"WORKIZ_TOKEN_{s}")}


class WorkizClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not token:
            raise WorkizError("Workiz API token is empty")
        self._prefix = f"/{token}"
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                  headers={"Accept": "application/json"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WorkizClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._prefix}/{path.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise WorkizError(f"Workiz GET {path} failed: {exc}") from exc
                self._sleep(2**attempt)
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < self.max_retries:
                self._sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                # never echo the URL: it contains the token
                raise WorkizError(f"Workiz GET {path} failed: HTTP {resp.status_code}", status_code=resp.status_code)
            return resp.json()
        raise WorkizError(f"Workiz GET {path} failed after retries")  # pragma: no cover

    @staticmethod
    def _items(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "jobs", "leads", "results"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return []

    def _paginate(self, path: str, params: dict[str, Any], page_size: int) -> Iterator[dict]:
        offset = 0
        while True:
            items = self._items(self._get(path, {**params, "offset": offset, "records": page_size}))
            yield from items
            if len(items) < page_size:
                return
            offset += page_size

    def iter_jobs(self, start: date, *, only_open: bool = False, page_size: int = 100) -> Iterator[dict]:
        """Jobs created/scheduled from `start` onward."""
        params = {"start_date": start.isoformat(), "only_open": str(only_open).lower()}
        yield from self._paginate("job/all/", params, page_size)

    def get_job(self, uuid: str) -> dict:
        items = self._items(self._get(f"job/get/{uuid}/"))
        return items[0] if items else {}

    def iter_leads(self, start: date, *, page_size: int = 100) -> Iterator[dict]:
        yield from self._paginate("lead/all/", {"start_date": start.isoformat()}, page_size)

    def team(self) -> list[dict]:
        return self._items(self._get("team/all/"))
