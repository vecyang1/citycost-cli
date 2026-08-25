# Changelog

## 1.1.0 — 2026-08-25 — a 429 reroutes instead of failing

**Policy reversal, on a measurement.** 1.0.0 argued that a proxy fallback must
stay manual. Re-measured the same day: the concurrent load that earned the
block stopped at ~09:25 +0700, and a direct probe at **10:39 was still 429**,
and was still refused when measurement ended. A remedy of "wait and retry" that
is wrong an hour later is not a remedy. What survives of the old argument is
the part that was actually right — the *hidden signal* — and that is fixed by
being loud, not by refusing to act.

**New — `citycost/fallback.py`, one owner for the whole policy**
- `auto` (default): direct first, reroute on **403 / 429 / 503 only**.
  `never`: direct only. `always`: skip the direct attempt. `--fetch-mode`
  overrides per run.
- Configuration from `~/.config/citycost/fetch.conf` or the environment, so no
  shell export is needed and the repository still holds **no proxy code and no
  credential**. The contract is unchanged: `{url}` in, body on stdout, exit 0
  only on 2xx.
- `CITYCOST_POST_CMD` — the same escape hatch for JSON-RPC, request body on
  **stdin**. A GET-only fallback is half a fallback, and the half that works
  hides that the other half never runs.
- Every reroute prints to stderr and the run ends with a provenance count.
- `doctor` runs a **real request** through the configured fetcher rather than
  reporting that a variable is set, and distinguishes "no config" from "a file
  is there but declares none of our keys".

**Fixed — all measured, all had shipped**
- **`taxi_km` had never been read on a metric-rendered page.** `TARGETS`
  spelled it `Taxi 1km`; Numbeo serves `Taxi 1 km (Standard Tariff)`. From a
  metric country that is every page. The client warned honestly on every run
  and the warning was read as upstream drift for the life of the module. Found
  by reading one city through a US exit and an Asian exit within the same
  minute — a single rendering cannot show you what it is missing.
  `tests/test_units.py` now asserts the two renderings yield an **identical key
  set**, ranging over all of `TARGETS` with the count pinned.
- **A 503 could never open the escape hatch.** After the retries were
  exhausted, `_with_retry` raised a fresh error that dropped the HTTP status,
  so the fallback saw "no status" and declined — for the one failure mode most
  likely to need it. Every 429 test stayed green throughout.
- **The fetcher subprocess inherited citycost's stdin.** `input=None` does not
  mean "no stdin", it means *the caller's*; a fetcher that reads stdin blocked
  until the subprocess timeout and presented as a slow proxy. It also took the
  test suite from 2.9s to 302s, which is how it was caught.
- **The retry decision no longer greps its own error message.** It reads a
  structured `SourceUnavailable.status`. A message is prose that gets reworded;
  the old substring test would have kept passing while production retried every
  rate limit.
- The config parser used `.strip('"').strip("'")`, which eats the closing quote
  of `--header 'Content-Type: application/json'` and leaves `shlex` an
  unbalanced string — i.e. it broke the escape hatch at the only moment it is
  ever used. Now strips a *matched* pair only.
- Removed an unreachable "is a fetcher configured" guard in `net._via_fetcher`:
  `_fetch` refuses first, so it could not fire, and a guard that cannot fire is
  one a reader trusts and a mutation test grades as covered.
- **The cache fingerprint was blind to the fix above.** `SCHEMA` hashed only
  `TARGETS.values()` — the field names — while the thing that decides what a
  cached payload *contains* is the label prefixes, i.e. the keys. So correcting
  `Taxi 1km` changed no fingerprint, every entry parsed under the broken
  spelling stayed "current", and `doctor` still reported 20/21 for Prague after
  the bug was gone. A fingerprint blind to a fix is worse than none: it
  certifies the stale answer. Now covers keys, values and the conversion
  factors, via a `schema_for()` the tests call rather than restate.
- **`doctor` failed the whole transport check on one flaky proxy attempt.**
  Residential exit nodes die mid-request — measured: the probe failed once and
  the identical command succeeded seconds later. The health check now makes two
  attempts and reports *"1 of 2 attempts failed"* alongside `ok`; two failures
  is still a FAIL. The data path is still attempted exactly once, because there
  each retry costs bandwidth and a 559-byte health check does not.
- **A long error destroyed the `doctor` table.** The text renderer pads every
  column to its widest cell, so one 300-character fetcher error pushed the
  other rows off the screen. Detail is capped for the table; `--json` keeps it.
- **A run where every city failed exited 0.** With the source blocked, `--json`
  produced a complete, well-shaped payload of nulls under a success code, which
  a caller reads as "these cities are unpriced" rather than "nothing was read".
  One city failing among several is still a partial answer and still exits 0.
  The test that covered this passed a *single* city and asserted exit 0 under
  the sentence "a sweep must not abort because one of ten names was mistyped" —
  a subject narrower than its own claim; it now carries one good name too.
- The blocked-with-no-fetcher remedy now names the variable for **this verb**.
  A GET-only setup being pointed at `CITYCOST_FETCH_CMD`, which its owner has
  already set and is looking straight at, is a right answer to the wrong
  question.

**Tests** 125 → 176. `tests/_sandbox.py` now also pins `CITYCOST_CONFIG` into
the sandbox and removes the three command variables — a location wants to be
*set somewhere disposable*, a switch wants to be *absent*; without both, a unit
test would have run through the developer's real, paid proxy. Landed in the
same change as the feature, not after it.

**Verified live** Numbeo direct returned 429 throughout this release. Every
`doctor`, `compare`, `rank` and `trend` run below therefore exercised the
automatic reroute end to end, and the resulting figures match the direct
readings taken before the block (control ratios 0.85 / 0.81 / 0.83, unchanged).

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
