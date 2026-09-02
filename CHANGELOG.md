# Changelog

## 1.5.0 — 2026-09-02 — the first harvest actually ran, and what it took to let it

Numbeo's seven-day address ban stated its own deadline — `Retry-After: Tue, 1
Sep 2026 08:00:00 +0200` — and on 2026-09-02 `citycost doctor` came back green
with no provenance line: every read direct, none rerouted. That made `harvest
--execute`, which had refused (correctly) on the only day anyone had tried it,
runnable for the first time. Three things were in its way, none of them a
crash.

### `harvest --resolve` — learn the denominator in one process

The plan reads each vertical's snapshot list from cache only, and a list
expires after a day. So the first live plan said `unknown` seven times and its
remedy was seven separate `citycost snapshots --index <vertical>` commands —
seven processes, each resetting the per-process throttle, which is the
shell-loop shape the harvest module's own docstring warns against. `--resolve`
spends exactly the list requests the plan already priced, under the same
refusals as the run (`--fetch-mode always` refused before request one, a
blocked status stops at that list, a rerouted read refuses to continue), and
prints the plan again with every cell known. It fetches no tables — a test
stands a trap on every table URL — and `--resolve --execute` spends the plan it
just printed. The unknown-denominator note now names this command; the parity
gate proves it parses.

### `CITYCOST_MIN_INTERVAL` — a sweep can be gentler than a read

The 1.1s gap to numbeo.com was a constant. It is the right floor for an
interactive `compare`, and a 176-request sweep against the host that banned
this address a week ago wanted more room than that. The variable raises the
per-host gap for one process; the plan quotes the gap it will actually sleep
and where it came from, because a plan saying 1.1s over a run sleeping 2.0s
under-reports the wait a user is agreeing to by half. It can only *raise*: a
value below the floor has no effect and `pace_source` says so, because a
parameter that is accepted and ignored has no value to read back. Not a number,
negative, `nan`, `inf` — refused, naming the variable. The suite scrubs it at
import in the same change, for the reason `tests/_sandbox.py` gives.

### The transport now closes the `HTTPError` it raises past

Both `except HTTPError` clauses in `net` built a `SourceUnavailable` and raised
`from exc` without closing it. The error wraps the response's file object, so
that handle went to the garbage collector — a socket held until GC, and on
Python 3.13+ a `ResourceWarning: Implicitly cleaning up <HTTPError 429>` at the
moment of collection. The suite had been printing three of those per run and
nobody had read them as a defect. The POST path still reads the body for its
detail first; both close in `finally`. The suite now runs clean under
`-W error::ResourceWarning`.

### Also

- The 429 remedy for a short block now names `CITYCOST_MIN_INTERVAL` alongside
  "lower concurrency", at the moment it is needed.
- README: the pipx copy on `PATH` does not follow the source tree. Measured
  again this release — it answered `1.3.1` to a tree that said `1.4.0`.
- 469 tests (`MIN_TESTS` raised to 465).

### Measured live, 2026-09-02 — the sweep re-earns the ban even paced

The point of the new commands is to make the archive fillable. The run that
exercised them also measured why it usually will not finish from here. `doctor`
was green direct that morning (the 2026-08-25 ban had lifted on schedule), so
`harvest --resolve --execute` ran with `CITYCOST_MIN_INTERVAL=2.0` — nearly
double the interactive floor. It completed **cost-of-living** (31/31, and
`trend Prague` reads those 12 snapshots back from cache as genuine history:
547→218 rows, distinct values, no identical-table alarm), then at **item 43 of
176** Numbeo answered 429 with a fresh `Retry-After` of **2026-10-01** — a new
~30-day address ban, earned by a single, well-paced, sequential client.

So the doctrine the module already carried — *a harvest needs a network that is
not blocked* — is stronger than "do not fan out subagents": **even one polite
client cannot sweep the archive from a banned-prone address; pacing buys items,
not immunity.** The tool behaved exactly as designed under it: the reroute took
that one blocked read (crime 2016-mid is cached), the run aborted at the first
reroute rather than grinding 133 more through the proxy, capped the egress at
one request, printed the provenance line, and exited 2 with 43 new items on
disk that a rerun will skip. Fill the rest a few per day at the interactive
pace, or from an unblocked network.

## 1.4.0 — 2026-08-26 — the panel can be diffed, and the archive can be kept

Three things, and the first two only work together.

### `movers` — which cities moved between two snapshots

`rank` reads one snapshot and `trend` reads one city. Neither answers the
question 17 years of history are actually for, and building it by hand took two
`rank --json` calls and a join.

```bash
citycost movers --from 2019 --to current --top 12
```

The join is over the cities present in **both**, and the denominator prints on
every run: 2019 carries 433 rows, `current` carries 558, **396 join**. The
other 199 are not movers — 162 are new and 37 were delisted — and both counts
print unconditionally with samples, because a symmetric explosion in them is
the only observer for a join that has silently stopped matching. Ranks render
as `33/433 → 386/558`; there is no rank-delta key anywhere, because most of a
rank change across those two panels is the panel growing by 29%.

Its whole design is the answer it must refuse to give. If `?title=` stops being
honoured upstream, both fetches return the current table, all ~550 cities join,
every delta is exactly 0.0% — a complete, well-formed "nothing moved in seven
years" with no error anywhere. So:

- the whole panel is fingerprinted **before** any column is selected, so no
  display flag can narrow the guard;
- a probe against the oldest published snapshot separates upstream drift
  (exit 1, this client needs updating) from two ids naming one published table
  (exit 2, pick different ids) — opposite fixes, so they never share an exit
  code;
- three further observers catch the shapes where the fingerprints differ and
  the answer is still wrong: a diffed column that is 100% exact zeros while a
  control column moves, a delta set with almost no distinct values, and ≥90% of
  cities moving the same way, which is a rebase rather than 400 cities moving
  together.

It also refuses when the overlap falls below half the smaller table, when
nothing carries the column in both snapshots ("nothing was compared" is not
"nothing moved"), and when `--column` resolves to two different labels across
the two headers — `--column rent` matches both `Rent Index` and `Cost of Living
Plus Rent Index`, and a delta between those two is arithmetic on unrelated
series, rendered exactly like a number with a meaning.

`--from` is required and there are no positional snapshot arguments, because
`movers 2019 current` and `movers current 2019` are both parseable and both
produce a complete table in which every sign is inverted. Nothing in the data
can catch a reversed base; only the surface can make the mistake unavailable.

And it renders **no** better/worse verdict — the only table in this tool that
does not. A rent index rising is bad for a renter and good for a landlord; a
salary index falling is bad; `Gross Rental Yield` has no single answer. This
repo already shipped one "lower is better" rule that marked the worst-paying
city as having the best salary.

### The cache can now keep a historical snapshot

`rankings.fetch` applied one TTL to every snapshot, so the immutable 2019
archive expired on the same clock as `current` — while its own docstring
already said "an index snapshot is a published artefact that does not move
between requests". Retention and freshness are now two axes with two
mechanisms:

- **May I delete this file?** Carried by the path
  (`~/.cache/citycost/keep/`). `_schema` is *designed* to change — bumping it
  is how a parser fix invalidates stale entries on purpose — so putting the
  keep-or-delete decision inside a schema-versioned blob would make the one
  event the system plans for the event that un-protects the archive. A path
  also survives a file that no longer parses.
- **May I serve this without asking?** A pure predicate, `is_archival`, keyed
  on the snapshot id alone. Not on `snapshots()`: that sits behind a network
  read, i.e. it is precisely the input that is missing during the seven-day ban
  this exists to survive, and a missing list would then silently *grant* a pin.

Every unknown degrades toward mutable, because the two errors are not
symmetric: under-pinning costs one request and a loud 429, over-pinning
produces a complete, plausible table that never expires and that no future run
can discover to be wrong. So the rule is coarse — the named year must have
ended plus one publication cycle — and on 2026-08-26 it pins `2025-mid` and
older while refusing `2026` and `2026-mid`, the current half-year that nothing
has measured to be finished.

"Immutable" is falsifiable rather than assumed: every archival read records a
fingerprint, and an id ever seen holding two different tables is marked
`_moved` and permanently refused a pin. A check that cannot fail is not a
check, and this one costs no extra request.

`--max-age 0` still forces a live read of a pinned entry, written once in the
probe. A conditionally-honoured 0 would recreate 1.3.0's accepted-and-silently-
ignored defect one layer down, and it is the only non-destructive way to
replace a poisoned pin.

**`citycost cache` no longer deletes anything by default.** Reading is the
default; `--prune` and `--clear` are named; `--clear` spares the archive unless
`--include-archive`. Until now, the command a person runs to *look* at the
cache pruned it.

### `harvest` — take the archive once

The tool already harvested: `trend X --snapshots 31` issued 32 unplanned
requests and threw them away after 3600 seconds. `harvest` gives that sweep a
plan, a consent number, an abort rule and a result that outlives the hour.

It fetches nothing without `--execute`. The plan partitions all 192 targets by
what the cache can already answer, and the partition **sums to the
denominator** — "you have 140 of 192" and "you have 192 files, all under the
previous schema" cost 52 and 192 requests, and a boolean would make them one
sentence. `current` is excluded and the plan says so on its own line: it is the
one URL in the family that legitimately moves, and folding it in is how a
one-time command becomes a weekly cron.

It aborts on the first refusal, holds a lock (pacing is per-process, so two
harvests are two unpaced clients), and has no `--jobs`, no `--out` and no
`--source`.

**It also refuses to run through the fallback fetcher, and did so here.**
Measured 2026-08-26 on a machine Numbeo has banned until 2026-09-01: it stopped
at item 1 of 177 with "the reroute is a repair, not a licence to sweep harder.
Run the harvest from a network that is not blocked." That refusal is the
project's own doctrine enforcing itself, and it is correct. The plan costs zero
requests and is safe to run any time; the sweep needs an unblocked address.

### Also

- **A gate for the interface nothing asserts on.** Every `citycost …` string
  printed by README, the help epilog, or any source literal must still parse,
  and every registered subcommand must be named in README. On its first run it
  found `movers`, `harvest` and `meetups` undocumented, and
  `citycost doctor --tools` — a remedy `discover.py` had printed since 1.0,
  which is `error: unrecognized arguments`. Both directions of its own
  extractor are asserted, after a splitter written to strip `> out.csv` also
  ate `--index <vertical>` and failed the gate on correct documentation.
- **`tools/scan_secrets.py` reported `CLEAN` over zero files.** Pointed at a
  directory that is not a git repository it printed "CLEAN — no
  credential-shaped strings" and exited 0. Its neighbouring test's docstring
  already said "'CLEAN' over zero files is not a pass, it is an unasked
  question" — and asserted only that the count exceeds 20 on the real repo,
  which says nothing about what happens when it is zero. Zero graded is now
  INCONCLUSIVE at exit 2.
- `?title=current` is no longer sent on the country and region views. Numbeo
  answers an unrecognised title with the current table, so it *worked* — while
  making one table reachable under two cache keys.
- `test_every_subcommand_is_reachable` enumerates the parser's own registry
  instead of a retyped tuple, and a new test asserts every `cmd_*` function is
  wired to a subcommand. An implemented command nobody can invoke was ungraded.
- `build_parser` moved to `citycost/parser.py` and the two panel-wide commands
  to `citycost/panel.py`; `fmt_age` joined the other formatters in
  `render.py`. `main()` is still the only entry point.
- No project ships `PROJECT_LINKS.md`; this repo declares its owner surfaces in
  `project_contract.json` instead of creating five empty ones to satisfy an
  audit.

## 1.3.1 — 2026-08-25 — the Apify price was public all along; the run was never the way to read it

Docs only; no code change. The last open item in `docs/DATA-STRATEGY.md` read
"Apify actor pricing: read, not run" — and the verification it was waiting for
was the wrong one. A run tells you what you were *charged*. The **price** is
declared platform metadata: public, machine-readable, free, no account.

```bash
curl -s "https://api.apify.com/v2/store?search=numbeo&limit=25"
```

Two corrections to what this repo claimed:

- **The range was $0.002–0.005/result. It is $0.002–0.010** across the eight
  live Numbeo actors — `lulzasaur/numbeo-scraper` is twice the stated ceiling.
- **The start fee was missing entirely**, and it inverts the ranking for small
  queries: `automation-lab/numbeo-scraper` charges $0.01 to start against
  $0.005 per result, so a one-city question costs three times what its
  per-result price implies.

Prices are also **tiered by the buyer's Apify plan**; the table now records
FREE, the most expensive case, and says so. Read
`eventTieredPricingUsd[<plan>].tieredEventPriceUsd`, **not** `eventPriceUsd` —
5 of the 8 carry no `eventPriceUsd` at all, and the first extractor written
here read them as having no price, which is this document's own "absent is not
zero" rule failing in the hand of the person writing it down.

**The comparison that settles it.** One Numbeo city page is 79 KB (measured
through this client). At $1/GB that is ~$0.000075 per city on the residential
lane, and $0 on the VPS already rented. The cheapest actor is ~27x the paid
lane. Apify stays rejected — with one honest caveat now recorded: it consumes
none of *our* egress, so it is the only option left if both lanes are ever
blocked. A third line, not a fallback.

Still unrun, and a different question from price: whether any of these actors
returns the 55 itemised rows this client needs, and whether the charge matches
the declaration.

## 1.3.0 — 2026-08-25 — a trend could not tell history from one table fetched twelve times

**The last open question in `docs/DATA-STRATEGY.md` was "does every non-cost
vertical actually support `?title=` history?" Measured through the tool's own
transport: all seven do.** Each vertical's oldest snapshot against its newest —
quality-of-life 95 rows in 2014 against 305 in 2026-mid, cost-of-living 41
against 547, and five more — so the planned degrade-to-current-only path is not
needed.

Answering it is what exposed the defect. **`?title=` is a request, not a
guarantee, and it has no error to fail into.** A parameter can be accepted and
silently ignored rather than refused; if Numbeo ever did that, every snapshot
would return the current table, the snapshot `<select>` this client reads its
list from would still be on the page, and `trend` would hand back a complete
series with `found: true` on every point. A flat line, read as a stable city.
Nothing raises, nothing is missing, and the number is wrong in the one way this
repository exists to catch.

- `rankings._is_one_table_repeated` refuses it: three or more found snapshots
  sharing identical metrics **and** an identical table size is one table
  fetched three times. `trend` warns on stderr naming the remedy and exits
  non-zero; `--json` still emits parseable JSON on stdout carrying
  `identical_across_snapshots`, because the run worth catching must not be the
  run that crashes somebody's parser.
- Table size is what makes the predicate safe to assert rather than merely
  plausible. A genuinely unchanging city still sits in tables of *different*
  sizes year to year — Numbeo's snapshots are not monotonic, 2022 carried 578
  cities and 2026-mid carries 547 — so metrics alone would fire on real data,
  and a warning that fires on healthy input gets muted within a week.
- Both directions are pinned, and the false-positive direction is the half
  that rots: a city printing the same figure across three snapshots of
  different sizes must **not** be flagged.

**`python -m citycost` now works.** There was no `__main__.py`, so the package
could not be executed. That is the invocation the README's own
`command not found` advice leaves a reader needing — their `PATH` is wrong,
which is exactly when the console script is the thing they cannot run — and it
is the form a script or an agent should use, because it cannot pick a different
copy. On 2026-08-25 a `pipx` build at 1.1.1 and a source tree at 1.2.0 both
answered to `citycost` and both printed the version they were, which made a
verified fix look like it had not worked.

**Live sweep, every command through the real entry point** while the address
was banned direct, i.e. entirely through the reroute: 15 commands, 14 green
first time. `rank --index quality-of-life` failed once at exit 2 and reproduced
green immediately, then green across all seven verticals — a transient lane
failure, not a broken vertical, and worth recording as such because the first
reading was "six of seven verticals are broken". A blocker is scoped to what
was measured.

## 1.2.0 — 2026-08-25 — `doctor` was grading a cache, not the network

**The health check could not fail.** `cmd_doctor` passed `max_age=args.max_age`
— default 3600 — to both Numbeo checks, so an hour-old cache answered the one
question the command exists to ask. Measured, two ways, with the network made
genuinely unreachable:

```
CITYCOST_FETCH_MODE=always CITYCOST_FETCH_CMD='/usr/bin/false {url}'
  numbeo rankings  ok  558 cities, 6 columns
  numbeo prices    ok  Prague: 21/21 rows priced (metric served)

CITYCOST_CONFIG=/dev/null CITYCOST_FETCH_MODE=never     # no fetcher, banned IP
  exit 0, every check green
```

The second is the one that would have cost someone a day: it is exactly the
state a new user lands in — Numbeo has banned their address for seven days and
they have configured no fallback — and `doctor`, the command you run *because*
something is wrong, reported nothing wrong. Not a wrong number; a **structurally
impossible failure**, the same green light as a type-checker run over zero
files.

- Both Numbeo checks now fetch with `max_age=0`. `--max-age` still applies to
  every other subcommand; for `doctor` it was never a meaningful knob, because
  a reachability check its own cache can satisfy has examined nothing.
- Each row states `live;`. `ok` alone cannot tell a reader which of the two
  they got, and a health report is read precisely by someone who cannot ask.
- After the fix, same two scenarios: `FAIL` / `FAIL`, exit 1. Normal run still
  exits 0 — and now genuinely proves the whole path, since it takes the real
  429 and reroutes before answering.

**`--json`'s `raw` was being truncated by a display flag.** `--full` selects
table columns; it was also deciding how much of the parse a machine consumer
received — 8 of 21 rows, under a key named `raw`. `raw["taxi_km"]` returned
`None` for a row that had parsed correctly and been discarded by a rendering
option, which reads as "Numbeo does not publish this" rather than "you did not
ask for it". `raw` is now the complete parse in both cases; `values` still
follows `--full`, which is a documented display selection.

## 1.1.1 — 2026-08-25 — the server had been stating the ban length all along

**Correction to 1.1.0's own reasoning.** 1.1.0 justified the automatic reroute
by *measuring* that Numbeo's block outlived its cause by more than an hour, and
recorded the duration as "unknown and longer". It was never unknown. Every one
of those 429 responses carried:

```
Retry-After: Tue, 1 Sep 2026 08:00:00 +0200
```

A **seven-day, address-level ban**. The policy 1.1.0 shipped happens to be
right — more right than the argument for it — but the client was reading the
wall instead of the sign on it, and its remedy string still said "lower
concurrency, widen --max-age and retry", which is a right answer to a different
question when you cannot come back for a week.

- `net.retry_after()` parses both legal forms (HTTP-date and delta-seconds) and
  the error now names the deadline and how far away it is.
- The **remedy is chosen from that number**: a block measured in hours or days
  says "this is an address-level block, not a pause — a real browser on this
  network is refused too; use another network or configure a fetcher". Only a
  short block still says "slow down".
- The reroute note now prints the exception's own message instead of a second,
  shorter summary of the same event — and stopped claiming the reroute "spends
  its bandwidth", which the client cannot know and which is false whenever the
  configured fetcher is free.

Also measured, each with one probe: the same request with a **real Chrome TLS
fingerprint from the same address** gets the identical 429, so this is the
address and not the client shape; and two probes minutes apart return the
*same* deadline, so the ban does not slide when you keep asking.

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
- **A dead proxy exit lost a whole city read.** The fetcher was attempted
  exactly once, on the argument that every attempt costs bandwidth. That is
  true of an *answer* and false of a *transport failure*: a TLS handshake that
  never completes transfers nothing. Measured against a known-good 2xx through
  a residential pool: **6 failures in 24 attempts**, then 0 in the next 10 —
  bursty, not steady. `ultra-low-cost-scraper` now exits **4** for "no HTTP
  answer" as distinct from **1** for "answered, non-2xx", and citycost retries
  only 4, three times, with backoff. A 429 is still never retried; that is the
  whole point of splitting the codes. (A first sample blamed one exit country;
  a larger one did not replicate it, so the fix is the retry, not the geo.)
- **`SourceUnavailable.status` was about to carry an exit code too.** Split
  into `status` (HTTP) and `exit_code` (process), because the first reader to
  compare one against the other's vocabulary gets a plausible answer — `4` is
  not an HTTP status and `403` is not an exit code.
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

**Tests** 125 → 188. `tests/_sandbox.py` now also pins `CITYCOST_CONFIG` into
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
  **Corrected in 1.2.0:** two of the four rows were answerable from an
  hour-old cache until then, so this overstated what `doctor` proved.

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
