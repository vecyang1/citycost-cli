# Data strategy — what each source is for, and why not the others

Status: living document. Every claim here was measured on 2026-08-25 with a
live request; where a claim is inherited or unverified it says so.

## The first-principles question

Not "which cost-of-living API should we use" but: **how do we get city-level,
reproducible, attributable cost figures at ~zero marginal cost, and know when
one of them is wrong?**

That reframing is what produced the answer. Numbeo's Data API is $260/mo
minimum with no free tier (measured at `numbeo.com/common/api.jsp`), but the
*same figures* are public HTML and `robots.txt` disallows only
`/heavy_crawling.any`. So the problem was never the data source. It was the
channel.

## The three roles

A source should do only what it is uniquely good at. Blending them produces a
number no source stands behind.

| Role | Source | Answers | Cost |
|---|---|---|---|
| **Discover** | nomads.com MCP | "*which* cities even qualify?" — filter by budget, region, internet, safety, temperature | free, rate-limited, ≤100/call |
| **Verify** | Numbeo city HTML | "what does it *actually* cost, itemised?" — 65 rows, server-converted to USD | free, 1 request/city |
| **Rank** | Numbeo rankings HTML | "how does it compare globally and *historically*?" — 558 cities × 6 indices × 31 snapshots | free, 1 request per 558 cities |
| **Control** | ratio of our budget to nomads' aggregate | "is this number even plausible?" | free, reuses the above |

The gap each fills is real, not cosmetic. **Numbeo cannot answer "which"** — it
has no filter, no search by budget; you must already know the city's name.
**Nomads cannot answer "how much, itemised"** — it publishes aggregates only.
Either alone is a half-tool.

## Efficiency: pick the channel by the *shape* of the question

Measured request costs:

| Question shape | Wrong way | Right way | Ratio |
|---|---|---|---|
| "index for 558 cities" | 558 city-page fetches | 1 rankings fetch | **558×** |
| "one city, itemised" | rankings page (indices only, no prices) | 1 city-page fetch | — |
| "one city over 17 years" | — | 31 rankings fetches, cached | — |
| "cities under $1200 in Asia" | crawl every Numbeo city | 1 MCP call | ~500× |

The rule: **the rankings pages are built to be read whole; the MCP endpoint
says of itself "this is not a bulk data source".** Honour both. A sweep that
walks the MCP endpoint city by city is both slower and a terms violation; a
sweep that walks Numbeo city pages to build a ranking is 558 requests for
something one request answers.

## Accuracy: every number needs a way to be caught being wrong

The defects fixed in this tool were, without exception, *silent confident wrong
answers* rather than crashes. So the strategy is not "be careful", it is
"install observers".

1. **Never convert currency locally.** Request `?displayCurrency=USD` and let
   Numbeo convert. The deleted local path hard-mapped `¥`→CNY behind a
   three-city allowlist and reported **Fukuoka rent as $11,095 against a true
   $470** (measured 2026-08-25). An allowlist of names is only ever right for
   the names somebody remembered.
2. **Read the header row; never key on column position.** The seven Numbeo
   verticals have between 4 and 11 columns. A parser pinned to index 3 will one
   day report the pollution index as the crime index.
3. **Absent is not zero.** Numbeo renders `?` for an unpriced row. A budget
   missing one component returns `N/A` for the whole total, never a short sum,
   and says on stderr which row was missing and why.
4. **Two absences, two messages.** "row label drifted upstream" (fix: this
   repo) and "nobody has priced it" (fix: nothing) must not share a sentence.
5. **Keep an independent control.** Our frugal basket runs a stable fraction of
   nomads' local figure — measured 0.81 / 0.98 / 0.83 / 0.81 / 0.85 across
   Tokyo, Fukuoka, Da Nang, Chiang Mai, Hanoi. A city that *breaks rank* is a
   scrape to re-read. The control is shown beside our figures and **never
   blended into them**; under three cities it returns no verdict rather than a
   clean bill.
6. **State the age of everything served.** A cached figure that does not
   announce its age will be read as current.

## Rate limiting — measured, not assumed

**Numbeo returns HTTP 429, and the block is long-lived.** Measured 2026-08-25:
six concurrent agents reading numbeo.com earned a 429 within roughly two
minutes. The concurrent readers were then stopped, and a single probe from the
same IP was **still 429 thirty minutes later**. nomads.com blocked at the same
time and states its budget in the 429 body: `Rate limited: 60 requests/hour per
IP`.

**Re-measured the same day.** The concurrent readers stopped at ~09:25 +0700;
a single direct probe at **10:39 was still 429** — over an hour — and was still
refused when measurement ended. So the floor is at least an hour and the true
duration remains **unknown and longer**. Treat the block as expensive, not
transient. This is the most important operational fact about the source, and it
decides four things in the client:

1. **A per-host minimum interval** (`net.MIN_INTERVAL`, 1.1s for numbeo.com,
   1.5s for nomads.com). Per-process and per-host, so an interactive run never
   notices and a sweep paces itself.
2. **429 is never retried.** Retrying a rate limit is what makes a rate limit
   worse. `net._with_retry` re-raises on 429 and on any 4xx, and retries only
   transient transport failures — measured: the first connect under load timed
   out and the immediate retry succeeded in 1.2s, so no-retry-at-all would have
   been wrong in the other direction.
3. **The cache is the real defence.** Widening `--max-age` converts a sweep
   into zero requests. This is why every payload carries its age.

4. **An automatic reroute, when — and only when — the user has named a
   fetcher.** Revised 2026-08-25 after the measurement above. The earlier
   position was that a proxy fallback must stay manual, on three arguments:
   cost, citizenship, and that it hides the signal that a sweep is too
   aggressive. The measurement retired the first two and the design answers
   the third:

   - *Cost* — the fetcher is only ever reached **after** a free direct attempt
     was made and refused, so an unblocked run spends nothing. `always` exists
     for the case where you already know, and must be asked for.
   - *Citizenship* — a block that outlives its cause by more than an hour is
     not a request to slow down that a client can honour by slowing down. The
     paced, cached, never-retried direct path is still what every run tries
     first; rerouting is what happens when that has already been refused.
   - *The hidden signal* — is the real objection, and it is fixed by making it
     loud rather than by refusing to act. Every reroute prints to stderr, the
     run ends with a provenance count, and `--json` consumers can read the
     events. A **404 never reroutes**: a wrong slug through a residential proxy
     is the same wrong slug, bought.

**The client still ships no proxy.** `CITYCOST_FETCH_CMD` / `CITYCOST_POST_CMD`
name an external command: `{url}` in, request body on stdin for POST, response
body on stdout, exit 0 only on 2xx. That contract is why this repository holds
no proxy code and no credentials — whatever the user plugs in stays entirely
theirs, and a public repository cannot leak what it never held.

### Measurement systems, re-measured through three exits

Numbeo picks imperial or metric from the **client IP**, and this was confirmed
again on 2026-08-25 by fetching one city (Da Nang) through three exits within
the same minute:

| exit | rows | rendering |
|---|---|---|
| US (Los Angeles) | 55 | `915 Square Feet`, `per Square Feet`, `Taxi 1 mile`, `1 lb` |
| Vietnam | 55 | `85 m2`, `per Square Meter`, `Taxi 1 km`, `1 kg` |
| Singapore | 55 | metric, as Vietnam |

Same 55 rows, same prices, four labels different. That pair of readings is also
a free correctness check on the conversion itself: `$354.72/sq ft × 10.7639 =
$3,818` against the metric page's `$3,818.15/m²`, and `$1.01/mile ÷ 1.60934 =
$0.63` against its `$0.63/km`. The factors are right, verified against the
source rather than against a constant table.

It also exposed a bug that a single rendering **cannot** show you: `TARGETS`
spelled the taxi row `Taxi 1km`, which is a prefix of nothing Numbeo serves, so
that row had never once been read on a metric page — i.e. never, from here. The
client warned honestly on every run and the warning was read as upstream drift.
`tests/test_units.py` now asserts the two renderings yield an **identical key
set**, which is the assertion that fails rather than shrugs.

The configured fetcher therefore requests a **metric exit** on this machine, so
the fallback path serves the same units the direct path would have.

## Sources evaluated and rejected (all probed live, 2026-08-25)

| Source | Why not |
|---|---|
| Numbeo Data API | $260 / $480 / $1,250 per month, no free tier |
| getwherenext `/api/data/city-prices` | free + CC BY 4.0, but **modelled**: `price_local` is exactly `price_usd × 25300` for every Vietnamese row; 19 items vs Numbeo's 65; its own `updated` field reads 2026-01-15 |
| affordwhere.com | terms permit personal non-commercial use only; its stated Eurostat/OECD sources were checked and **contain no Vietnam at all** |
| World Bank ICP | country-level only; indices, not prices — useful as a country sanity anchor, not a substitute |
| Eurostat / OECD | no Southeast Asia price coverage; OECD `DSD_PPP` returns `NoRecordsFound` for VNM |
| Apify Numbeo actors | ~$0.002–0.005/result, needs no Numbeo key — a reasonable **paid fallback**, not a primary. Pricing read from listing pages; **not verified by running one** (would cost money) |
| teleport.org | dead — `api.teleport.org` is NXDOMAIN |
| expatistan.com | Cloudflare 403 on every path including `/robots.txt`; whether an API exists is **UNKNOWN** |

## Terms and attribution

- Numbeo data is **proprietary**. This tool reads public pages for personal and
  research use, links back, and **redistributes no dataset**. Do not commit
  scraped output to a public repository.
- `robots.txt` (checked 2026-08-25) disallows only `/heavy_crawling.any`. The
  client is single-threaded per invocation and caches, so a normal session
  issues fewer requests than a human browsing the same pages.
- nomads.com asks for attribution and states results are capped and rate
  limited. Its own `attribution` string is carried through to output.

## Open / unverified

- Apify actor pricing: read, not run.
- Whether every non-cost Numbeo vertical supports `?title=` history — under
  verification; the client must degrade to "current only" where it does not.
- nomads → Numbeo slug mapping is a **guess the caller verifies by fetching**,
  because neither site publishes a mapping. A hardcoded table would be wrong
  for every city nobody has hit yet.
