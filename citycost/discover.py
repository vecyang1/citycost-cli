"""Nomads.com discovery, spoken over its MCP endpoint but not *registered* as one.

Why a client and not an MCP server entry: a registered server costs context in
every session — its tool schemas are loaded or deferred, and a connection is
held — whether or not the session ever asks about cities. Wrapping the same
JSON-RPC endpoint in a subcommand costs nothing until it is invoked, and the
capability is still discoverable through this CLI's own `--help`.

The endpoint states its own terms in its handshake: *"Results are capped and
rate limited; this is not a bulk data source."* This module is built to that:
one call per invocation, `limit` capped, and no pagination loop. Bulk work
belongs on Numbeo's ranking pages, which are designed to be read whole.
"""

from __future__ import annotations

from .errors import SourceUnavailable
from .net import cached_json, http_post_json

MCP_URL = "https://nomads.com/mcp"
PROTOCOL = "2024-11-05"
SCHEMA = "nomads-mcp-1"

#: The endpoint's stated ceiling for `search_cities`. Asking for more is not an
#: error, it is silently clamped — so the client refuses instead, because a
#: result labelled 500 that contains 100 is worse than a refusal.
MAX_LIMIT = 100

#: The endpoint states its own budget in the 429 body, verbatim:
#:     {"code": -32000, "message": "Rate limited: 60 requests/hour per IP"}
#: Measured 2026-08-25. That is 1 request per minute sustained, which is why
#: `net.MIN_INTERVAL` paces this host at 1.5s and why `discover --verify` does
#: its per-city verification against Numbeo rather than looping back here.
RATE_LIMIT_PER_HOUR = 60


def _rpc(method: str, params: dict, *, request_id: int = 1) -> dict:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method,
               "params": params}
    body = http_post_json(MCP_URL, payload,
                          headers={"Accept": "application/json"})
    if "error" in body:
        err = body["error"]
        raise SourceUnavailable(
            f"nomads.com MCP error {err.get('code')}: {err.get('message')}",
            "the tool name or arguments may have changed; run "
            "`citycost doctor` to re-read the server's tool list")
    return body.get("result") or {}


def _call_tool(name: str, arguments: dict) -> object:
    """Invoke one MCP tool and unwrap its content block to a Python object.

    An MCP tool result carries text, not JSON, even when the text *is* JSON.
    Returning the raw envelope to callers would push that unwrapping into every
    call site, which is exactly how one of them ends up forgetting `isError`.
    """
    result = _rpc("tools/call", {"name": name, "arguments": arguments},
                  request_id=3)
    if result.get("isError"):
        raise SourceUnavailable(
            f"nomads.com tool '{name}' reported an error: "
            f"{_first_text(result)[:200]}",
            # Names a file, not a command: there is no `--tools` flag and
            # never was. A remedy pointing at a command that exits immediately
            # is worse than none, because the reader spends their attention on
            # the tool rather than on the fault.
            "nomads.com owns these argument names and may have changed them; "
            "this client builds them in citycost/discover.py")
    text = _first_text(result)
    if not text:
        raise SourceUnavailable(
            f"nomads.com tool '{name}' returned no content",
            "the response shape may have changed")
    import json
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _first_text(result: dict) -> str:
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") in (None, "text"):
            return block.get("text") or ""
    return ""


def server_info() -> dict:
    """Handshake only. Cheap enough to be the health check."""
    return _rpc("initialize", {
        "protocolVersion": PROTOCOL,
        "capabilities": {},
        "clientInfo": {"name": "citycost", "version": "1.0.0"},
    })


def list_tools() -> list[dict]:
    return (_rpc("tools/list", {}, request_id=2) or {}).get("tools") or []


def search_cities(*, max_cost_usd: float | None = None, region: str | None = None,
                  country: str | None = None, min_internet_mbps: float | None = None,
                  min_safety: float | None = None,
                  min_temperature_c: float | None = None,
                  max_temperature_c: float | None = None,
                  limit: int = 20, max_age: int = 3600) -> dict:
    """Filter the world down to candidates. Returns the server's own envelope.

    `total_matching` is kept and surfaced: it is the difference between "there
    are 4 such cities" and "we showed you 4 of 137", and a caller that drops it
    turns a truncated page into a false complete answer.
    """
    if limit > MAX_LIMIT:
        raise SourceUnavailable(
            f"limit={limit} exceeds the endpoint's cap of {MAX_LIMIT}",
            f"ask for at most {MAX_LIMIT}; for a whole-world sweep use "
            "`citycost rank`, which reads pages built to be read whole")
    args = {k: v for k, v in {
        "max_cost_usd": max_cost_usd, "region": region, "country": country,
        "min_internet_mbps": min_internet_mbps, "min_safety": min_safety,
        "min_temperature_c": min_temperature_c,
        "max_temperature_c": max_temperature_c, "limit": limit,
    }.items() if v is not None}

    key = "nomads:search:" + "&".join(f"{k}={args[k]}" for k in sorted(args))
    payload, age, _ = cached_json(key, SCHEMA, max_age,
                                  lambda: _call_tool("search_cities", args))
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["_age_s"] = age
    return payload or {}


def get_city(slug: str, *, max_age: int = 3600) -> dict:
    payload, age, _ = cached_json(f"nomads:city:{slug}", SCHEMA, max_age,
                                  lambda: _call_tool("get_city", {"slug": slug}))
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["_age_s"] = age
    return payload or {}


def list_meetups(*, city: str | None = None, country: str | None = None,
                 days_ahead: int | None = None, limit: int = 20,
                 max_age: int = 1800) -> dict:
    args = {k: v for k, v in {"city": city, "country": country,
                              "days_ahead": days_ahead, "limit": limit}.items()
            if v is not None}
    key = "nomads:meetups:" + "&".join(f"{k}={args[k]}" for k in sorted(args))
    payload, age, _ = cached_json(key, SCHEMA, max_age,
                                  lambda: _call_tool("list_meetups", args))
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["_age_s"] = age
    return payload or {}


def numbeo_slug_for(city: dict) -> str:
    """Best-effort bridge from a nomads slug to a Numbeo one.

    Deliberately a *guess* that the caller must verify by fetching: nomads uses
    `da-nang-vietnam`, Numbeo uses `Da-Nang`, and neither publishes a mapping.
    Returning a guess the caller checks beats a hardcoded table that is wrong
    for every city nobody has hit yet.
    """
    name = (city.get("name") or "").strip()
    if not name:
        slug = city.get("slug") or ""
        country = (city.get("country") or "").lower().replace(" ", "-")
        if country and slug.endswith("-" + country):
            slug = slug[: -len(country) - 1]
        # Numbeo writes its slugs capitalised (`Chiang-Mai`, not `chiang-mai`).
        # Whether its URLs are case-insensitive is UNVERIFIED, so match the
        # observed form rather than relying on tolerance nobody measured.
        name = " ".join(w.capitalize() for w in slug.replace("-", " ").split())
    return "-".join(part for part in name.replace("'", "").split() if part)
