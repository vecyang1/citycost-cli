# Changelog

## 1.0.0 — 2026-08-25

First release. Supersedes the single-file `city-cost-compare-nomad` skill
script, which is now a pointer to this package.

**Sources**
- `discover` — nomads.com MCP (`search_cities`, `get_city`, `list_meetups`),
  spoken as JSON-RPC over HTTP rather than registered as an MCP server, so it
  costs nothing until invoked.
- `compare` — Numbeo city pages, 21 itemised rows.
- `rank` / `trend` / `snapshots` — the Numbeo ranking family: 7 verticals,
  city / country / region views, 31 snapshots back to 2009.
- `find` — live slug lookup; `doctor` — reachability of every source.

**Measured facts this release is built on** (all 2026-08-25, live)
- Numbeo Data API: $260 / $480 / $1,250 per month, no free tier.
- Numbeo returns **HTTP 429** under concurrent load; nomads.com states its
  budget in the 429 body — `Rate limited: 60 requests/hour per IP`.
- Numbeo serves **imperial or metric by client geography** from the same URL.
  `measurementSystem`, `measurement`, `units` and `displayMeasurement` were all
  tested as query parameters and all ignored. (An earlier note in the skill
  called this an upstream *rename*; that was wrong and is corrected here.)
- `rankings_current.jsp` returns 558 cities in one request; per-city pages
  would need 558.
- The rank cell is **empty in the HTML** — DataTables fills it in client-side,
  so rank is derived from row order.
- Ten city-name keys resolve to two or more different cities, co-existing in a
  single snapshot with unstable order.
- Snapshot row counts are **not** monotonic (2022 carried 578 cities;
  2026-mid carries 547), so an absence cannot be explained as "the old table
  was smaller".

**Guards** — see README; each corresponds to a wrong number that shipped.
