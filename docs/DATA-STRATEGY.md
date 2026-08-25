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

**Numbeo returns HTTP 429.** Measured 2026-08-25: six concurrent agents reading
numbeo.com earned a 429 within roughly two minutes, and it persisted through at
least the next few minutes on a single probe. This is the most important
operational fact about the source, and it decides three things in the client:

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

**Residential-proxy rotation was considered and rejected as a default.** A
rotating residential IP pool routes around this trivially, but: it costs money
per GB, it is a poor citizen on a site whose `robots.txt` permits ordinary
reading, and — the deciding reason — it *hides the signal* that a sweep is too
aggressive instead of fixing it.

It stays reachable through `CITYCOST_FETCH_CMD`, which takes any external
fetcher and is never enabled by default. That indirection is also why this
repository holds no proxy code and no credentials: the escape hatch is a
contract (`{url}` in, body on stdout, exit 0 on success), so whatever the user
plugs in stays entirely theirs.

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
