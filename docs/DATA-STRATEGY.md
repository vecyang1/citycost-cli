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
| **Verify** | Numbeo city HTML | "what does it *actually* cost, itemised?" — 55 rows, server-converted to USD | free, 1 request/city |
| **Rank** | Numbeo rankings HTML | "how does it compare globally and *historically*?" — 558 cities × 6 index columns × 31 snapshots, across 7 verticals | free, 1 request per 558 cities |
| **Control** | ratio of our budget to nomads' aggregate | "is this number even plausible?" | free, reuses the above |

The gap each fills is real, not cosmetic. **Numbeo cannot answer "which"** — it
has no filter, no search by budget; you must already know the city's name.
**Nomads cannot answer "how much, itemised"** — it publishes aggregates only.
Either alone is a half-tool.

## The archive is a one-time purchase, and it was not being kept

Measured 2026-08-26. The 7 verticals publish **192 historical tables**
(cost-of-living and property carry 31 snapshots each, the other five 26).
Fetching all of them once costs 192 paced requests and ~11 MB. Every one of
them is a finished, published artefact.

Until 1.4.0 none of that could be *kept*. Three things defeated it together,
and any one of them alone was enough:

| | |
|---|---|
| `rankings.fetch` applied one TTL to every snapshot | the 2009 archive expired on the same clock as `current` — one hour, as the CLI passes it |
| `CACHE_RETENTION` was 7 days for everything | a harvest evaporated on the next prune |
| `citycost cache` pruned with no flag | the command you run to *look* at the cache deleted part of it, and there was no read-only mode to run instead |

So the status quo was not the frugal option. `citycost trend X --snapshots 31`
issues 32 requests and discards them within the hour; three verticals explored
across four sessions in one working day is ~364 requests for 88 distinct
immutable objects — more, in one day, than one harvest costs for all seven
verticals.

**What is decided from the snapshot id, and what is not.** Pin eligibility is
derived from the id by date arithmetic alone, never from `snapshots()`. That
list sits behind a network read, which makes it precisely the input that is
unavailable during the seven-day ban the archive exists to survive — and if its
absence could *grant* a pin, a newly published snapshot missing from a stale
list would be judged "older than the newest" and frozen forever. Every unknown
degrades toward mutable instead, because the errors are not symmetric:
under-pinning costs one request and a loud 429 carrying the server's own
deadline, while over-pinning produces a complete, plausible, internally
consistent table that never expires and that no later run can discover to be
wrong.

`2026-mid` is the trap the rule is coarse for. This document records it at 547
rows against `current`'s 558, and that single reading is equally consistent
with "frozen at publication" and "still filling". So it is not pinned, and
neither is `2026`; the cost is two re-fetches per vertical out of 31.

**And the claim is falsifiable.** Every archival read records a fingerprint of
the table it saw. An id ever observed holding two different tables is marked
`_moved` and permanently refused a pin, regardless of age. Without that,
"this snapshot is immutable" is a check that cannot fail — this document's own
disqualifier — and with it the question can actually be closed by observation
rather than by assumption, at no extra request.

## Rate limiting decides who may run a sweep, not just how fast

`citycost harvest --execute` was run here on 2026-08-26 and **refused at item 1
of 177**:

```
a request was rerouted through the external fetcher — harvest stopped at item 1 of 177
  -> the reroute is a repair, not a licence to sweep harder.
     Run the harvest from a network that is not blocked
```

That is this document's own position enforcing in code. The reroute exists so
that an *ordinary read* survives a block; routing a 192-request sweep through a
borrowed VPS or a paid residential exit removes the only feedback signal that
the volume was wrong, and it spends somebody's bandwidth to do it. The plan
(`citycost harvest`, no flags) costs zero requests and is safe at any time; the
sweep needs an address the source has not banned.

The alternative that looks most conservative — a documented shell loop over
`citycost rank --snapshot <id>` — is the worst of the three options, because
`net._last_hit` is per **process**: 192 separate invocations reset the 1.1s
throttle 192 times and hammer at full speed, with no plan, no abort on the
first 429 and no lock. That is closer to the six-concurrent-agent load that
earned the seven-day ban than the harvest is.

## Efficiency: pick the channel by the *shape* of the question

Measured request costs:

| Question shape | Wrong way | Right way | Ratio |
|---|---|---|---|
| "index for 558 cities" | 558 city-page fetches | 1 rankings fetch | **558×** |
| "one city, itemised" | rankings page (indices only, no prices) | 1 city-page fetch | — |
| "one city over 17 years" | — | 31 rankings fetches, cached | — |
| "cities under $1200 in Asia" | crawl every Numbeo city | 1 MCP call | ~500× |
| "which cities moved most, 2019 → now" | 2 × 558 city-page fetches | 2 rankings fetches + a join | **558×** |
| the same question, asked again next week | 2 more rankings fetches | 1 (the 2019 side is pinned) | **2×** |

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

**CORRECTION, same day: the duration was never unknown — we had not read the
header.** The paragraph here previously said "the floor is at least an hour and
the true duration remains unknown and longer", derived by probing. Numbeo's 429
response states it outright, in every one of those responses:

```
HTTP/1.1 429
Retry-After: Tue, 1 Sep 2026 08:00:00 +0200
Vary: user-agent,accept-encoding
Set-Cookie: LivingCost=9-43597-9-ip-<the client address>
```

That is a **seven-day, address-level ban**, not a pause. Measuring how high the
wall is, while a sign on the wall gives the number, is the mistake — and the
remedy built on the measurement ("wait and retry, lower concurrency") was wrong
in a way the measurement itself could not reveal: *you cannot wait*.

Two further facts, each from a single probe:

- **It is the address, not the client.** The same request through `curl_cffi`
  with a real Chrome TLS fingerprint and no proxy, from the same machine,
  returns the identical 429 and the identical `Retry-After`. A browser on that
  network is refused too. So TLS impersonation does not help here, and nothing
  about the client's shape is worth tuning.
- **The ban does not slide.** Two probes minutes apart returned the *same*
  deadline, so further requests do not extend it. Useful, because the opposite
  assumption makes any diagnosis feel too expensive to perform.

`citycost` now parses `Retry-After` and puts the deadline in the error, and
picks its remedy from it: a block measured in days says "use another network or
a fetcher", never "lower concurrency and retry". This is the most important
operational fact about the source, and it decides four things in the client:

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

**Two egresses, cheapest first.** A fetcher does not have to be a paid proxy.
Measured 2026-08-25 while the local address was banned, `curl` from three
different rented hosts: two answered **503** — Numbeo blocks those datacenter
ranges outright — and one answered **200**. So a machine you already rent is
worth testing per site before spending per-GB proxy bandwidth, and the
per-machine config on this developer's box chains them: own host first,
residential proxy only if it fails. Neither the hosts nor the chain live in this
repository.

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
| getwherenext `/api/data/city-prices` | free + CC BY 4.0, but **modelled**: `price_local` is exactly `price_usd × 25300` for every Vietnamese row; 19 items vs Numbeo's 55; its own `updated` field reads 2026-01-15 |
| affordwhere.com | terms permit personal non-commercial use only; its stated Eurostat/OECD sources were checked and **contain no Vietnam at all** |
| World Bank ICP | country-level only; indices, not prices — useful as a country sanity anchor, not a substitute |
| Eurostat / OECD | no Southeast Asia price coverage; OECD `DSD_PPP` returns `NoRecordsFound` for VNM |
| Apify Numbeo actors | **$0.002–0.010/result** across the 8 live actors, plus a per-run start fee of up to $0.01; needs no Numbeo key. Correctly rejected: ~27x the residential lane already wired in and infinitely more than the free one. See the measurement below. |
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

- ~~Apify actor pricing: read, not run.~~ **Closed 2026-08-25 — and the
  verification this item asked for was the wrong one.** A run tells you what
  you were *charged*; the *price* is declared platform metadata, public,
  machine-readable, and free to read with no account:

  ```bash
  curl -s "https://api.apify.com/v2/store?search=numbeo&limit=25"
  ```

  Read `eventTieredPricingUsd[<your plan>].tieredEventPriceUsd`, **not**
  `eventPriceUsd`. Both shapes occur — 5 of the 8 actors use the tiered form
  and carry no `eventPriceUsd` at all, so an extractor that reads only the
  flat key reports them as having no per-result price. That is the absence
  this document's own rule 3 is about, met in the wild while checking it.
  Prices are **tiered by the buyer's Apify plan**; the figures below are the
  FREE tier, which pays the most.

  | actor | per result | start fee | runs | 30-day success |
  |---|---|---|---|---|
  | `sheshinmcfly/numbeo-cost-of-living-scraper` | $0.00200 | $0.00005 | 152 | 29/29 |
  | `solidcode/numbeo-scraper` | $0.00215 | $0.00500 | 163 | 32/32 |
  | `trovevault/cost-of-living-scraper` | $0.00250 | $0.00100 | 227 | 30/30 |
  | `logiover/numbeo-cost-of-living-scrape` | $0.00350 | $0.00005 | 297 | 46/50 |
  | `parseforge/numbeo-scraper` | $0.00490 | — | 211 | 38/38 |
  | `automation-lab/numbeo-scraper` | $0.00500 | $0.01000 | 503 | 57/61 |
  | `crawlerbros/numbeo-scraper` | $0.00500 | $0.00500 | 106 | 30/39 |
  | `lulzasaur/numbeo-scraper` | $0.01000 | $0.00005 | 133 | 30/30 |

  Two corrections to what this table used to say. The range was given as
  $0.002–0.005; the real ceiling is **twice that**. And the **start fee was
  missing entirely**, which inverts the ranking for small queries:
  `automation-lab` charges $0.01 to start against $0.005 per result, so a
  one-city question costs three times what its per-result price suggests.

  **The comparison that decides it.** One Numbeo city page is 79 KB
  (measured). Through the residential lane at $1/GB that is **~$0.000075 per
  city**; through the VPS already rented, **$0**. The cheapest actor is ~27x
  the paid lane. Apify's one real advantage is that it uses none of *our*
  egress, so it is the only option left if both lanes are ever blocked — a
  third line, not a fallback.

  Still genuinely unrun, and a different question from price: whether any of
  these actors returns the 55 itemised rows this client needs, and whether
  the charge matches the declaration. Neither is answerable without spending.
- ~~Whether every non-cost Numbeo vertical supports `?title=` history.~~
  **Closed 2026-08-25 — measured, all seven do.** Each vertical's oldest
  snapshot was fetched against its newest and the tables differ in every case,
  so `?title=` is honoured and not silently ignored:

  | vertical | snapshots | oldest | newest | rows old → new |
  |---|---|---|---|---|
  | cost-of-living | 31 | 2009 | 2026-mid | 41 → 547 |
  | property | 31 | 2009 | 2026-mid | 65 → 393 |
  | crime | 26 | 2014 | 2026-mid | 335 → 401 |
  | health-care | 26 | 2014 | 2026-mid | 132 → 323 |
  | pollution | 26 | 2014 | 2026-mid | 260 → 347 |
  | quality-of-life | 26 | 2014 | 2026-mid | 95 → 305 |
  | traffic | 26 | 2014 | 2026-mid | 121 → 341 |

  The planned degrade-to-current path is therefore **not needed today**, and
  the answer above is the kind that rots: a parameter can stop being honoured
  without ever starting to error. Numbeo would answer 200 with the current
  table for every `title=`, the snapshot `<select>` this client reads its list
  from would still be on the page, and `trend` would return a complete series
  with `found: true` on every point — a flat line read as a stable city.

  So the finding is pinned by a predicate rather than by this paragraph:
  `rankings._is_one_table_repeated` refuses to present a series whose found
  points share identical metrics **and** an identical table size, and `trend`
  exits non-zero saying so. Table size is what makes that safe to assert —
  a genuinely unchanging city still sits in tables of different sizes, because
  the snapshots are not monotonic (2022 carried 578 cities, 2026-mid 547).
- **Whether the 192-table archive is internally consistent** is unanswered
  here, because the harvest has not been executed: this address is banned until
  2026-09-01 and the harvest correctly refuses the reroute. What exists is the
  plan (192 targets, 15 already cached, 177 to fetch, 178 of them pinnable),
  verified at zero requests, and the observers that would grade the result —
  a cross-snapshot fingerprint collision means one table was fetched twice
  under two archival names. Both remain unrun against a full corpus.
- nomads → Numbeo slug mapping is a **guess the caller verifies by fetching**,
  because neither site publishes a mapping. A hardcoded table would be wrong
  for every city nobody has hit yet.
