"""`citycost harvest` — what the corpus holds, and what completing it costs.

Every question on this side is answerable WITHOUT SENDING A REQUEST, and that
is the seam rather than a coincidence. `plan()` states the cost of a decision
the user has not made yet, so a planner that could fetch would be spending the
consent it was about to ask for. Confining it to one file makes that property
inspectable instead of promised: nothing here calls `rankings.fetch` or
`rankings.snapshots`, the snapshot lists are read out of `net.cache_probe`'s
returned blob, and `rankings.fetch` is only ever handed to `inspect.signature`.
That is a claim a reader can check against one screen of imports, where in a
995-line module it was a paragraph asking to be believed.

Open this file for where a number came from: the cache keys, the target list,
the probe partition, the denominator, the pacing floor, the two read-backs a
finished run is graded by (`_stored`, `_outstanding` — disk questions, asked
with the plan's own predicate), and the repeated-table alarm. The alarm lives
here in BOTH its readings — the tables on disk and the tables a run holds in
hand — because it is one arithmetic and one threshold; splitting it by which
reading called it is how a corpus came to inspect clean while the requests were
spent on one table served five times.

Open `harvest.py` for what happens when this plan is spent, and
`harvest_report.py` for how any of it is printed.
"""

from __future__ import annotations

import inspect
import urllib.parse

from . import net, rankings
from .errors import CitycostError
# `PROBE_STATES` is the column vocabulary and `EXCLUDED_CURRENT` is a sentence
# addressed to a reader; both are defined by the module that prints them, and
# borrowed here to seed the partition's zeros and to state the exclusion in the
# payload. `verify_partition` is called at construction so no consumer is ever
# handed a report whose columns do not add up.
from .harvest_report import EXCLUDED_CURRENT, PROBE_STATES, verify_partition

#: Identical fingerprints below this many snapshots is not evidence: two
#: snapshots can legitimately carry the same table, and `rankings` uses the
#: same floor for the same reason.
REPEAT_ALARM = 3


# -- targets ---------------------------------------------------------------

def _signature_default(func, name: str) -> int:
    """The TTL for a kind of read belongs to the function that performs it.
    Restating `21600` and `86400` here would be two more literals for facts
    `rankings` already owns, agreeing on the day they were written. A signature
    that stops carrying one fails loudly rather than quietly substituting a
    shorter TTL, which would make the plan over-report."""
    param = inspect.signature(func).parameters.get(name)
    default = inspect.Parameter.empty if param is None else param.default
    if isinstance(default, bool) or not isinstance(default, int):
        raise CitycostError(
            f"{getattr(func, '__name__', func)}({name}=) does not carry an "
            f"integer default ({'absent' if param is None else repr(default)})"
            f", so a plan cannot state the freshness rule the fetch applies",
            "this client needs updating")
    return default


def _target_key(url: str) -> str:
    """The cache key `rankings.fetch` writes a ranking table under.

    The one fact this module restates about another, and load-bearing: get it
    wrong and the plan reports an empty corpus while 192 files sit on disk.
    Guarded by a round trip rather than by care — fetch with a stubbed
    transport, then assert `plan()` sees `fresh` — so a changed key prefix goes
    red instead of silently costing 192 requests.
    """
    return f"rank:{url}"


def _list_key(vertical: str) -> str:
    """Likewise for `rankings.snapshots`, and guarded by the same round trip."""
    seg = rankings.VERTICALS.get(vertical, vertical)
    return f"snapshots:{rankings.BASE}/{seg}/rankings.jsp"


def _pace_for(url: str) -> float:
    """The per-host floor, read off `net.MIN_INTERVAL` rather than restated.

    Naming 1.1 here would make the consent number stop tracking the throttle
    the moment somebody tuned it, and the floor is half of what the user agrees
    to: 192 x 1.1s is three and a half minutes of sleep before any response
    time, and a user who does not know that kills the run at ninety seconds.
    """
    host = urllib.parse.urlsplit(url).netloc
    return float(net.MIN_INTERVAL.get(host, net.DEFAULT_INTERVAL))


def _snapshot_ids(vertical: str, list_max_age: int) -> tuple[list, dict]:
    """The vertical's snapshot list, read from cache ONLY — never fetched.

    `ids` is None when the denominator is unknown, which differs from `[]` in
    the way this repository is about: a vertical whose list could not be read
    contributes 0 to a naive total, so the plan under-reports and the run
    over-spends. An empty list is unknown too — `0 of 0` reads as "complete",
    a check that cannot return false.
    """
    probe = net.cache_probe(_list_key(vertical), rankings.SCHEMA, list_max_age)
    info = {"list_state": probe["state"], "list_age_s": probe["age_s"]}
    if probe["state"] != "fresh":
        info["denominator_source"] = f"snapshot list {probe['state']}"
        return None, info
    payload = (probe["blob"] or {}).get("payload")
    if not isinstance(payload, list):
        info["denominator_source"] = "cached snapshot list is not a list"
        return None, info
    ids = [str(v) for v in payload if rankings.SNAPSHOT_ID.fullmatch(str(v))]
    if not ids:
        info["denominator_source"] = "cached snapshot list is empty"
        return None, info
    if len(ids) != len(payload):
        # Loud rather than quietly narrower: the difference is snapshots that
        # would never be harvested and never missed.
        info["unrecognised_ids"] = len(payload) - len(ids)
    info["denominator_source"] = "cached snapshot list"
    return ids, info


def _targets_for(vertical: str, ids: list, max_age: int, now: float | None):
    """One row per snapshot id, in the order Numbeo publishes them.

    Publication order is kept rather than sorted, so a run that aborts part-way
    leaves a prefix the user can predict. `current` cannot appear here: the
    `<select>` carries titled ids only — see `EXCLUDED_CURRENT`.
    """
    out = []
    for snap in ids:
        url = rankings._url(vertical, "city", snap, None)
        archival = rankings.is_archival(snap, now=now)
        probe = net.cache_probe(_target_key(url), rankings.SCHEMA, max_age,
                                archival=archival)
        out.append({
            "vertical": vertical, "snapshot": snap, "url": url,
            "state": probe["state"], "age_s": probe["age_s"],
            # Whether an id may be PINNED (no age clock, out of reach of prune)
            # is `rankings.is_archival`'s call, not ours; on 2026-08-25 it
            # refuses the current half-year, so two ids per vertical expire on
            # the ordinary clock. Saying so is a corpus rather than a belief.
            "pinned": archival,
        })
    return out


# -- is the corpus repeating -----------------------------------------------

def _fingerprint_of(probe: dict):
    """The fingerprint of the table a probe found, or None when the disk holds
    nothing comparable.

    None and `""` are two different answers and collapsing them is the bug this
    module already documents one layer down: `""` is what
    `rankings.panel_fingerprint` returns for a table that parsed with NO ROWS,
    precisely so two empty parses do not read as identical. Returning None for
    "no readable file" keeps that distinction reachable here.
    """
    if probe["state"] not in ("fresh", "stale"):
        return None
    payload = (probe["blob"] or {}).get("payload")
    if not isinstance(payload, dict):
        return None
    return rankings.panel_fingerprint(payload)


def _distinct_fingerprints(prints: list) -> dict:
    """Count distinctness over fingerprints. One counter, two callers: the
    tables a run just fetched, and the tables sitting on disk."""
    counts: dict = {}
    blank = 0
    for fp in prints:
        if not fp:
            # `panel_fingerprint` returns "" for a table with no rows, exactly
            # so that two empty parses do not read as identical. Honouring that
            # here matters in the direction that hurts: a vertical whose
            # snapshots legitimately parse empty would otherwise trip the
            # repeat alarm and exit 1 forever, which is the standing-red gate
            # that teaches a reader to ignore it.
            blank += 1
            continue
        counts[fp] = counts.get(fp, 0) + 1
    top = max(counts.values()) if counts else 0
    return {"of": len(prints), "distinct": len(counts), "max_repeat": top,
            "unfingerprintable": blank, "repeated": top >= REPEAT_ALARM}


def _distinct_tables(tables: list) -> dict:
    """Cross-snapshot distinctness for one vertical, over the tables just
    fetched.

    The repository's signature defect at harvest scale. `?title=` is a request,
    not a guarantee, and it has no error path to fall into: if it is ever
    accepted and ignored, harvest stores N copies of the current table and
    reports N successes and N files of the right size, after which every later
    `trend` reads the flat line from cache, offline, forever. The harvest holds
    the whole vertical at once — strictly more evidence than `trend` ever has.

    Keyed on parsed rows, not row count alone, because two snapshots can carry
    the same number of cities. Not shared with
    `rankings._is_one_table_repeated`: that ranges over one city's series and
    keys on `(metrics, of)`, this over whole tables — one function would
    silently narrow one of them.
    """
    return _distinct_fingerprints([rankings.panel_fingerprint(t)
                                   for t in tables])


def _distinct_on_disk(rows: list, max_age: int) -> dict:
    """`vertical -> distinct summary` over the tables CURRENTLY ON DISK.

    Re-derived rather than persisted, and the choice is the fix. Measured, the
    alarm ranged only over the tables a run had just fetched: run 1 caught a
    poisoned corpus and exited 1, run 2 over the same five files reported clean
    and exited 0 — so a finding about the corpus survived exactly as long as
    the process that made it. Persisting it instead would put a second owner
    beside the cache, which this module refuses a manifest for on the same
    grounds; worse, a stored alarm cannot be cleared by fixing the thing it
    describes, so `--max-age 0` would re-read the ids, repair the files, and
    leave a permanently red gate that teaches its reader to ignore it. Reading
    the files means the alarm stops exactly when the repetition stops.
    """
    out = {}
    for row in rows:
        if row.get("denominator_state") != "known":
            continue
        prints = [_fingerprint_of(net.cache_probe(
            _target_key(t["url"]), rankings.SCHEMA, max_age,
            archival=t["pinned"])) for t in row["targets"]]
        out[row["vertical"]] = _distinct_fingerprints(
            [fp for fp in prints if fp is not None])
    return out


# -- plan ------------------------------------------------------------------

def plan(verticals=None, *, max_age: int | None = None,
         list_max_age: int | None = None, now: float | None = None) -> dict:
    """What a harvest would cost. Read-only, and structurally zero-network.

    Nothing here can reach the transport: the snapshot lists are read out of
    `net.cache_probe`'s returned blob rather than through `rankings.snapshots`,
    which would fetch on a miss. That is why a vertical with no cached list
    reports `unknown` and states the one request it would take to learn — a
    plan that went and found out would spend the consent it was about to ask
    for.
    """
    if max_age is None:
        max_age = _signature_default(rankings.fetch, "max_age")
    if list_max_age is None:
        list_max_age = _signature_default(rankings.snapshots, "max_age")

    # De-duplicated in publication order, because `--index` is `append` with a
    # `choices=` list and argparse accepts the same choice twice: the same
    # vertical asked for twice used to be planned twice, so the denominator,
    # the to-fetch column and the pacing floor all doubled while the run — one
    # target list, one cache key per URL — spent half of it.
    wanted = (sorted(rankings.VERTICALS) if verticals is None
              else list(dict.fromkeys(verticals)))
    unknown_names = [v for v in wanted if v not in rankings.VERTICALS]
    if unknown_names:
        raise CitycostError(
            f"unknown index {', '.join(sorted(unknown_names))}",
            "one of: " + ", ".join(sorted(rankings.VERTICALS)))

    rows, totals = [], dict.fromkeys(PROBE_STATES, 0)
    unknown, ages = [], []
    for vertical in wanted:
        ids, info = _snapshot_ids(vertical, list_max_age)
        if ids is None:
            unknown.append(vertical)
            # `list_request_cost` is stated because the plan is not free
            # either: with no cached lists the real ceiling is 199, not 192.
            rows.append({
                "vertical": vertical, "denominator": None, "targets": [],
                "denominator_state": "unknown", "snapshots": [],
                "states": dict.fromkeys(PROBE_STATES, 0), "to_fetch": None,
                "already_cached": None, "pinned": None, "unpinned_ids": [],
                "list_request_cost": 1, **info})
            continue
        targets = _targets_for(vertical, ids, max_age, now)
        states = _partition(targets)
        for k, v in states.items():
            totals[k] = totals.get(k, 0) + v
        ages += [t["age_s"] for t in targets
                 if t["state"] == "fresh" and t["age_s"] is not None]
        rows.append({
            "vertical": vertical, "denominator": len(targets),
            "denominator_state": "known", "snapshots": list(ids),
            "targets": targets, "states": states, "list_request_cost": 0,
            "to_fetch": len(targets) - states.get("fresh", 0),
            "already_cached": states.get("fresh", 0),
            "pinned": sum(1 for t in targets if t["pinned"]),
            "unpinned_ids": [t["snapshot"] for t in targets if not t["pinned"]],
            **info})

    on_disk = _distinct_on_disk(rows, max_age)
    repeated = sorted(v for v, d in on_disk.items() if d["repeated"])
    known = [r for r in rows if r["denominator_state"] == "known"]
    denominator = sum(r["denominator"] for r in known) if not unknown else None
    # Two different questions, and collapsing them is how the consent number
    # went missing. `to_fetch` is the TOTAL, so it is None the moment any
    # denominator is unknown — a partial sum passing as the total is the thing
    # this command refuses. `to_fetch_known` is what has actually been counted,
    # which is a fact whether or not the rest is readable.
    to_fetch_known = sum(r["to_fetch"] for r in known)
    to_fetch = None if unknown else to_fetch_known
    list_requests = sum(r["list_request_cost"] for r in rows)
    pace = _pace_for(rankings._url("cost-of-living", "city", "2020", None))

    report = {
        # `rows` is printed with the plan so a selector that silently narrows
        # later shows up as a count that dropped rather than as a table that
        # still looks complete. `executed` and `requests_sent` are structural
        # rather than inferred: a consumer scripting `citycost harvest &&
        # next-step` reads a bare exit 0 as "the corpus is here", and a missing
        # key is not the same evidence as a stated `false`.
        "verticals": rows, "rows": len(rows),
        "verticals_available": len(rankings.VERTICALS),
        "scope": "all" if len(rows) == len(rankings.VERTICALS) else "subset",
        "denominator": denominator, "unknown_verticals": unknown,
        "denominator_state": "unknown" if unknown else "known",
        "states": totals, "already_cached": totals.get("fresh", 0),
        "to_fetch": to_fetch, "to_fetch_known": to_fetch_known,
        "list_requests": list_requests,
        # Always a number, because `to_fetch + list_requests` was None in
        # exactly one case — the first run, where `to_fetch` is unknown and
        # `list_requests` is the only non-zero cost — so the one user who has
        # never seen this command was asked to consent to nothing at all. A
        # floor labelled as a floor can be consented to; None cannot, and it
        # printed as no line rather than as a hedge.
        "requests_planned": to_fetch_known + list_requests,
        "requests_planned_state": "floor" if unknown else "exact",
        "pacing_floor_s": round((to_fetch_known + list_requests) * pace, 1),
        "min_interval_s": pace, "pacing_source": "net.MIN_INTERVAL",
        "cached_age_min_s": min(ages) if ages else None,
        "cached_age_max_s": max(ages) if ages else None,
        "pinned": sum(r["pinned"] or 0 for r in known),
        "unpinned": sum(len(r["unpinned_ids"]) for r in known),
        "excluded": [EXCLUDED_CURRENT], "max_age": max_age,
        "list_max_age": list_max_age, "schema": rankings.SCHEMA,
        "executed": False, "requests_sent": 0,
        "distinct_on_disk": on_disk, "repeated_verticals": repeated,
        # A corpus that repeats is not a complete corpus, it is one table
        # cached under N ids — and every later `trend` reads it offline as a
        # flat line, which is the failure this whole alarm exists for.
        "complete": None if unknown else (to_fetch == 0 and not repeated),
    }
    verify_partition(report)
    return report


def _partition(targets: list) -> dict:
    """Count by probe state — see `PROBE_STATES` for why an unheard-of one is
    still counted."""
    counts = dict.fromkeys(PROBE_STATES, 0)
    for t in targets:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    return counts


# -- read-backs ------------------------------------------------------------

def _stored(targets: list, max_age: int) -> int:
    """How many of the tables this run fetched are actually ON DISK.

    A successful fetch is the writer's report of its own work; this is the
    read-back. A read-only home directory or a full disk would otherwise let a
    192-request harvest report success having stored nothing — the one failure
    this command cannot survive, since the requests are not repeatable while
    the address is banned. The predicate is *present and under the current
    schema* (`fresh` OR `stale`): whether an entry may be SERVED depends on a
    TTL, and keying on `fresh` would call a corpus lost after `--max-age 0`.
    """
    return sum(1 for t in targets
               if net.cache_probe(_target_key(t["url"]), rankings.SCHEMA,
                                  max_age, archival=t["pinned"])["state"]
               in ("fresh", "stale"))


def _outstanding(report: dict) -> int:
    """What a rerun would cost, asked with the PLAN's own question.

    Not `total - attempted + failed`: an `empty` outcome never reaches
    `cache_write`, so it is absent again to the next `plan()` — measured, the
    report said a rerun costs 1 request and the next plan said 2. Nor
    `total - ok`, because `ok` is the writer's report of its own work and a
    table that was fetched and not stored is equally absent.

    So the disk is re-probed with `state != "fresh"`, which is the rule
    `plan()` counts to-fetch by. Two owners for "what does a rerun cost" is two
    numbers, both printed as facts, against a source that bans by address.
    """
    return sum(1 for row in report["verticals"] for t in row["targets"]
               if t["state"] != "fresh"
               and net.cache_probe(_target_key(t["url"]), rankings.SCHEMA,
                                   report["max_age"],
                                   archival=t["pinned"])["state"] != "fresh")
