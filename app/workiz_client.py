"""Thin read-only client for the Workiz REST API (v1).

Workiz puts the API token in the URL path: https://api.workiz.com/api/v1/{token}/job/all/
Paging differs by endpoint (checked against the live API): job/all counts `offset` in pages of `records`,
lead/all counts it in records. `records` is capped at 100. Both return `has_more`. Without only_open=false,
job/all returns open jobs only.

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
    """{market: token} for every WORKIZ_TOKEN_<MARKET> that is set. Known suffixes map to the CTM market names
    above; any other suffix becomes its own market (WORKIZ_TOKEN_NORTH_DALLAS -> "North Dallas")."""
    env = env if env is not None else dict(os.environ)
    names = {s: m for m, s in MARKET_ENV.items()}
    out = {}
    for key, token in sorted(env.items()):
        if key.startswith("WORKIZ_TOKEN_") and token:
            suffix = key.removeprefix("WORKIZ_TOKEN_")
            out[names.get(suffix, suffix.replace("_", " ").title())] = token
    return out


class WorkizClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 5,
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
            if resp.status_code == 429 and attempt < self.max_retries:
                # "Account reached Api Quotas": the window is long, so back off in tens of seconds
                self._sleep(min(15 * 2**attempt, 120))
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
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

    def _paginate(self, path: str, params: dict[str, Any], page_size: int, *, offset_in_pages: bool) -> Iterator[dict]:
        page_size = min(page_size, 100)
        page = 0
        while True:
            offset = page if offset_in_pages else page * page_size
            payload = self._get(path, {**params, "offset": offset, "records": page_size})
            items = self._items(payload)
            yield from items
            has_more = payload.get("has_more") if isinstance(payload, dict) else None
            if not items or has_more is False or (has_more is None and len(items) < page_size):
                return
            page += 1

    def iter_jobs(self, start: date, *, only_open: bool = False, page_size: int = 100) -> Iterator[dict]:
        """Jobs created/scheduled from `start` onward."""
        params = {"start_date": start.isoformat(), "only_open": str(only_open).lower()}
        yield from self._paginate("job/all/", params, page_size, offset_in_pages=True)

    def get_job(self, uuid: str) -> dict:
        items = self._items(self._get(f"job/get/{uuid}/"))
        return items[0] if items else {}

    def iter_leads(self, start: date, *, page_size: int = 100) -> Iterator[dict]:
        yield from self._paginate("lead/all/", {"start_date": start.isoformat()}, page_size, offset_in_pages=False)

    def team(self) -> list[dict]:
        return self._items(self._get("team/all/"))
