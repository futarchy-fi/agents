"""HTTP client for the Futarchy API."""

from __future__ import annotations

import json
import sys

import httpx

DEFAULT_API_URL = "https://api.futarchy.ai"
TIMEOUT = 15.0


class APIError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class Client:
    def __init__(self, api_url: str = DEFAULT_API_URL, api_key: str | None = None):
        self.base = api_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(base_url=self.base, headers=headers, timeout=TIMEOUT)

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.ConnectError:
            print(f"Error: cannot connect to {self.base}", file=sys.stderr)
            sys.exit(1)
        except httpx.TimeoutException:
            print(f"Error: request to {self.base}{path} timed out", file=sys.stderr)
            sys.exit(1)

        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except (json.JSONDecodeError, ValueError):
                detail = resp.text
            raise APIError(resp.status_code, detail)
        return resp.json()

    def get(self, path: str, **params) -> dict | list:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json=body)

    # ── Market endpoints ──

    def list_markets(self) -> list[dict]:
        return self.get("/v1/markets")

    def get_market(self, market_id: int) -> dict:
        return self.get(f"/v1/markets/{market_id}")

    # ── Auth / user endpoints ──

    def device_auth_start(self) -> dict:
        return self.post("/v1/auth/device", body={})

    def device_auth_poll(self, device_code: str) -> dict:
        return self.post("/v1/auth/device/token", body={"device_code": device_code})

    def me(self) -> dict:
        return self.get("/v1/me")

    # ── Trading endpoints ──

    def buy(self, market_id: int, outcome: str, budget: float) -> dict:
        return self.post(f"/v1/markets/{market_id}/buy", body={
            "outcome": outcome,
            "budget": budget,
        })

    def sell(self, market_id: int, outcome: str, amount: float) -> dict:
        return self.post(f"/v1/markets/{market_id}/sell", body={
            "outcome": outcome,
            "amount": amount,
        })
