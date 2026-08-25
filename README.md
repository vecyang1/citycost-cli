# citycost

City cost-of-living intelligence from free public sources — **discover** which
cities qualify, **verify** what they actually cost, **rank** them globally and
across 17 years of history, and **cross-check** every figure against an
independent source.

Zero dependencies. Python 3.10+. Every command speaks `--json`, so an agent can
consume it as readily as a human can read it.

```bash
citycost discover --max-cost 1200 --region Asia --min-internet 30 --verify
citycost compare Da-Nang Hanoi Chiang-Mai --md --crosscheck
citycost rank --index quality-of-life --top 15
citycost trend Prague --snapshots 12
citycost find Vietnam
```

## Why this exists

Numbeo's Data API starts at **$260/month with no free tier** (Basic $260,
Professional $480, Enterprise $1,250 — checked 2026-08-25). The identical
figures are served as public HTML, and `robots.txt` disallows only
`/heavy_crawling.any`. So the problem was never the data source. It was the
channel.

But a scraper is the easy half. The hard half is that cost data fails
*silently*: nothing crashes, a plausible number appears, and somebody moves
country on it. Every guard in this tool exists because a specific wrong number
actually shipped:

| What happened | What it produced |
|---|---|
| `¥` hard-mapped to CNY behind a three-city Japanese allowlist | **Fukuoka rent reported as $11,095. The true figure is $470.** |
| Utilities row matched by exact label | Utilities, budget, and savings silently `N/A` for months |
| One "lower is better" rule for every column | The **worst**-paying city marked as having the best salary |
| First-match city lookup | Vancouver-Washington and Vancouver-BC spliced into one 17-year "trend" |
| Cache with no schema version | Entries written by an older version served with the new fields quietly missing |

## What each source is for

Blending sources produces a number none of them stands behind, so each one does
only what it is uniquely good at.

| | Source | Answers | Notes |
|---|---|---|---|
| **discover** | nomads.com MCP | *which* cities qualify — filter by budget, region, internet, safety, temperature | free; **60 requests/hour per IP**, ≤100 results per call |
| **compare** | numbeo.com city pages | what one city *actually* costs, 21 itemised rows | free; 1 request per city |
| **rank** | numbeo.com ranking pages | how cities compare — 7 verticals, 558 cities, 6 index columns each, 31 snapshots back to 2009 | free; **1 request per 558 cities** |
| **crosscheck** | ratio of the two | is this figure even plausible? | free; reuses the above |

Numbeo cannot answer *which* — it has no search by budget, so you must already
know the city's name. nomads.com cannot answer *how much, itemised* — it
publishes aggregates only. Either alone is a half-tool.

## Accuracy guarantees

- **Numbeo converts the currency, not us.** Every request carries
  `?displayCurrency=USD`. The removed local-conversion path is where the
  Fukuoka defect lived, along with a rate-API dependency that failed for
  *every* city whenever it was down.
- **Metric output, always** — Numbeo serves imperial or metric *according to
  your IP*, and no query parameter pins it. This tool detects which arrived,
  converts square feet and miles, and states in the output which system the
  source used. Pass `--units source` to opt out.
- **Absent is never zero.** Numbeo writes `?` for an unpriced row. That becomes
  `null` in JSON and `N/A` in text; a budget missing one component reports
  `N/A` for the whole total rather than a short sum, and names the missing row
  on stderr.
- **A display flag never shapes the JSON parse.** `compare --json` returns
  `raw` — the complete 21-row parse, source text exactly as served — whether or
  not you pass `--full`. `values` is the display selection and does follow it.
  Until 1.2.0 `raw` followed it too, so `raw["taxi_km"]` was `null` for a row
  that had parsed correctly and been dropped by a table option: an absence that
  reads as "unpriced" when it meant "not requested".
- **Two absences, two messages.** "a row label drifted upstream" (fix: this
  repo) and "nobody has priced it" (fix: nothing) never share a sentence.
- **Ambiguity is returned, not resolved.** Ten city names map to two or more
  real cities. `citycost trend Vancouver` exits non-zero and names both rather
  than picking one.
- **Everything states its age.** Cached figures announce how old they are.

## Install

```bash
pipx install git+https://github.com/vecyang1/citycost-cli
# or
pip install git+https://github.com/vecyang1/citycost-cli
```

If `citycost: command not found` afterwards, your installer's bin directory is
not on `PATH` — `pipx ensurepath`, then open a new shell.

## Being a good citizen

Both sources are free and neither owes you access. The client paces itself
(1.1s between numbeo.com requests, 1.5s for nomads.com), caches aggressively,
and **never retries a 429** — retrying a rate limit is what makes a rate limit
worse. Widening `--max-age` turns a repeat sweep into zero requests.

### When a source refuses you anyway

Numbeo's 429 is not a pause. It states its own length:

```
Retry-After: Tue, 1 Sep 2026 08:00:00 +0200      # a seven-day ban
```

and it is keyed on your **address**, not your client — the same request with a
real Chrome TLS fingerprint from the same machine gets the identical response,
so a browser on that network is refused too. citycost reads that header, puts
the deadline in the error, and picks its advice from it: "lower concurrency and
retry" is useless when you cannot come back for a week.

So citycost will **reroute automatically** — but only through a fetcher you
name, and only after the free direct path was tried and refused:

```ini
# ~/.config/citycost/fetch.conf   (or the same names as environment variables)
CITYCOST_FETCH_MODE=auto
CITYCOST_FETCH_CMD=my-fetcher {url} --raw
CITYCOST_POST_CMD=my-fetcher {url} --raw --data @- --header 'Content-Type: application/json'
```

citycost runs the command, takes stdout as the body, and requires exit 0. The
contract has one more rule, and it exists because the two failures need
opposite policies:

| fetcher exit | meaning | citycost does |
|---|---|---|
| `0` | 2xx | uses the body |
| `4` | no HTTP answer — dead exit node, failed handshake | retries, 3x with backoff |
| `3` | the fetcher's own environment is wrong | fails; retrying never helps |
| any other non-zero | answered, non-2xx | fails; retrying a 429 is what makes it worse |

Only `4` is retried, and only because it costs nothing: a handshake that never
completed transferred no bytes. Everything else is either an answer or a
misconfiguration, and repeating it just spends more of whatever the fetcher
spends.

That is the entire contract, which is the point: **this repository ships no proxy
code and no credentials, and cannot leak what it never holds.** Anything that
can fetch a URL works — a proxy CLI, a corporate egress, an SSH tunnel, a
cache, a friend's VPS.

| | |
|---|---|
| `auto` (default) | direct first; reroute on 403 / 429 / 503 only |
| `never` | direct only — a 429 fails and you see it |
| `always` | skip the direct attempt, when you already know you are blocked |

Rules the policy keeps, so that "bold" does not become "rude":

- **A 404 never reroutes.** A wrong slug through a residential proxy is the
  same wrong slug, bought.
- **Nothing is silent.** Every reroute prints to stderr, and the run ends with
  a provenance line saying how many requests did not come down the ordinary
  path — a client that quietly routes around a rate limit is a client whose
  owner never learns their sweep was too aggressive.
- **Nothing happens without configuration.** With no fetcher set, `auto` is
  exactly the old direct-only behaviour, and the 429 error tells you how to
  change that instead of telling you to wait.

`citycost doctor` proves the fetcher *works* — it runs a real request through
it — rather than reporting that a variable is set. Its two Numbeo rows are
**always read live** (`max_age=0`) and say so on the line, whatever `--max-age`
you pass; until 1.2.0 they honoured it, so an hour-old cache could report a
banned address as `ok` at exit 0. A reachability check its own cache can answer
has examined nothing, and `ok` alone gives the reader no way to tell which of
the two they got — hence the `live;` label rather than a comment in the source.

Because it reads live, a green `doctor` on a blocked address is real evidence
the *whole* path works: it takes the actual 429 and comes back through the
fetcher before reporting.

## Data, licence, and what you may do with the output

The **code** is AGPL-3.0-or-later. The **data** is not ours to license:

- Numbeo's figures are proprietary, read here from public pages for personal
  and research use. Cite Numbeo when you publish a figure obtained through this
  tool, and do not commit its output to a public repository. If you are
  building something commercial on cost-of-living data, buy their API licence —
  this tool exists so that a person comparing a handful of cities does not have
  to.
- nomads.com asks for attribution and states that it is not a bulk data source.
  Its own attribution string is carried through to output.

See [NOTICE](NOTICE) for the full terms and
[docs/DATA-STRATEGY.md](docs/DATA-STRATEGY.md) for every source that was
evaluated and why the rejected ones were rejected.

## Development

```bash
python -m unittest discover -t . -s tests -v   # no network, sandboxed
# -t . is not optional: without it the relative imports in tests/ cannot
# resolve, and the run reports errors that are not defects.
```

The suite is offline by design. A green CI that depended on numbeo.com being up
would be reporting "the site answered", not "the code is right".
