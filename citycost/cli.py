"""citycost — one entry point over three sources, each doing what it is good at.

    discover   nomads.com MCP     which cities even qualify
    compare    numbeo city pages  what they actually cost, itemised
    rank       numbeo rankings    how they compare, globally and historically
    trend      numbeo snapshots   one city across 17 years

Every command speaks --json, so an agent consumes it as readily as a human
reads it. Provenance, warnings and data age go to stderr, so --json and --csv
stay pipe-clean.
"""

from __future__ import annotations

import os
import sys

from . import (__version__, budget, discover, fallback, prices, rankings,
               render)
from .errors import CitycostError
from . import net
from .net import clear_cache, fmt_age, prune_cache
# Re-exported, not redefined: `cli.build_parser` is the name every caller
# and test already uses, and the parser itself lives in one module.
from .parser import build_parser
# Imported by NAME, not as a module: `cmd_*` must be attributes of `cli` for
# the parser to resolve them and for the orphaned-command test to see them.
from .panel import cmd_harvest, cmd_movers  # noqa: F401



# ---------------------------------------------------------------- discover --

def cmd_discover(args) -> int:
    res = discover.search_cities(
        max_cost_usd=args.max_cost, region=args.region, country=args.country,
        min_internet_mbps=args.min_internet, min_safety=args.min_safety,
        min_temperature_c=args.min_temp, max_temperature_c=args.max_temp,
        limit=args.limit, max_age=args.max_age)
    cities = res.get("cities") or []
    total = res.get("total_matching")
    returned = res.get("returned", len(cities))

    if args.verify:
        for c in cities:
            slug = discover.numbeo_slug_for(c)
            rec = prices.try_fetch(slug, max_age=args.max_age)
            c["_numbeo_slug"] = slug
            if rec.get("error"):
                c["_numbeo"] = None
                c["_numbeo_error"] = rec["error"]
            else:
                b = budget.budget_breakdown(rec["values"])
                c["_numbeo"] = b["total"]
                c["_numbeo_missing"] = b["missing"]

    if args.json:
        render.emit_json(res)
    else:
        headers = ["#", "City", "Country", "Nomad $/mo", "Score"]
        if args.verify:
            headers += ["Numbeo $/mo", "Numbeo slug"]
        rows = []
        for i, c in enumerate(cities, 1):
            row = [str(i),
                   f"{render.flag(c.get('country',''))} {c.get('name','')}".strip(),
                   c.get("country", ""),
                   render.money(c.get("cost_for_nomad_usd_per_month")),
                   render.number(c.get("overall_score"), 2)]
            if args.verify:
                row += [render.money(c.get("_numbeo")), c.get("_numbeo_slug", "")]
            rows.append(row)
        print(render.text_table(headers, rows, color=not args.no_color))

    # total_matching is the difference between "there are 4" and "here are 4 of
    # 137". Dropping it turns a truncated page into a false complete answer.
    if total is not None and returned is not None and total > returned:
        render.note(f"  showing {returned} of {total} matching cities "
                    f"(--limit up to {discover.MAX_LIMIT})")
    render.age_note(res.get("_age_s"), "nomads.com")
    if res.get("attribution"):
        render.note(f"  {res['attribution']}")
    return 0


def cmd_city(args) -> int:
    data = discover.get_city(args.slug, max_age=args.max_age)
    if args.json:
        render.emit_json(data)
        return 0
    city = data.get("city") if isinstance(data, dict) else None
    body = city if isinstance(city, dict) else data
    rows = [[k, "" if v is None else str(v)]
            for k, v in sorted(body.items()) if not k.startswith("_")]
    print(render.text_table(["Field", "Value"], rows, color=not args.no_color))
    render.age_note(data.get("_age_s"), "nomads.com")
    return 0


def cmd_meetups(args) -> int:
    data = discover.list_meetups(city=args.city, country=args.country,
                                 days_ahead=args.days_ahead, limit=args.limit,
                                 max_age=args.max_age)
    if args.json:
        render.emit_json(data)
        return 0
    items = data.get("meetups") or data.get("results") or []
    if not items:
        render.note("  no upcoming meetups matched")
        return 0
    rows = [[str(m.get("city") or m.get("slug") or ""), str(m.get("country") or ""),
             str(m.get("date") or m.get("when") or ""),
             str(m.get("going") or m.get("attendees") or "")] for m in items]
    print(render.text_table(["City", "Country", "When", "Going"], rows,
                            color=not args.no_color))
    render.age_note(data.get("_age_s"), "nomads.com")
    return 0


# ----------------------------------------------------------------- compare --

def cmd_compare(args) -> int:
    currency = "LOCAL" if args.local else "USD"
    records = []
    for slug in args.cities:
        render.note(f"{slug}...")
        rec = prices.try_fetch(slug, currency=currency, units=args.units,
                               max_age=args.max_age)
        if rec.get("error"):
            render.note(f"  {rec['error']}\n    -> {rec.get('remedy','')}")
        else:
            miss = prices.missing_report(rec)
            if miss["unmatched"]:
                render.note(f"  WARNING {slug}: no row matched "
                            f"{miss['unmatched']} — a label likely drifted "
                            f"upstream; update prices.TARGETS")
            if miss["blank"]:
                render.note(f"  NOTE {slug}: Numbeo has no price for "
                            f"{miss['blank']} (row shows '?')")
        rec["_budget"] = budget.budget_breakdown(rec.get("values") or {})
        if args.crosscheck and not rec.get("error"):
            rec["_control"] = _control_for(slug, args.max_age)
        records.append(rec)

    if not records:
        return 1

    # One city failing among several is a partial answer and exits 0 — the
    # table says N/A and stderr says why. *Every* city failing is a failed run
    # and must exit non-zero: otherwise a blocked source hands a `--json`
    # consumer a full-shaped payload of nulls with a success code, which reads
    # as "these cities have no data" rather than "nothing was read".
    status = 0 if any(not r.get("error") for r in records) else 1
    if status:
        render.note("  no city could be read — every figure below is absent, "
                    "not zero")

    keys = [k for k, _ in _display_keys(args)]
    if args.json:
        render.emit_json([_as_json(r, keys) for r in records])
        return status
    if args.csv:
        render.emit_csv([_as_flat(r, keys) for r in records],
                        ["slug", "country", *keys, "budget", "savings",
                         "rent_to_income_pct", "control_local", "ratio",
                         "missing"])
        return status

    _print_compare(records, keys, args)
    return status


def _display_keys(args):
    order = ["cheap_meal", "transport_pass", "utilities", "internet", "mobile",
             "rent_1br_center", "rent_1br_outside", "net_salary"]
    if args.full:
        order = list(prices.LABELS)
    return [(k, prices.LABELS[k]) for k in order if k in prices.LABELS]


def _control_for(slug: str, max_age: int):
    try:
        data = discover.get_city(slug.lower().replace(" ", "-"), max_age=max_age)
    except CitycostError:
        return None
    city = data.get("city") if isinstance(data.get("city"), dict) else data
    local = (city or {}).get("cost_for_local_usd_per_month")
    return {"local_usd": local, "source": "nomads.com"} if local else None


def _as_json(rec: dict, keys: list[str]) -> dict:
    vals = rec.get("values") or {}
    b = rec["_budget"]
    return {
        "slug": rec.get("slug"), "country": rec.get("country") or None,
        "currency": rec.get("currency"), "age_s": rec.get("_age_s"),
        "measurement_system_served": rec.get("measurement_system"),
        "output_units": rec.get("output_units"),
        "converted": rec.get("converted") or {},
        "source": "numbeo-scrape", "url": rec.get("url"),
        "error": rec.get("error"), "remedy": rec.get("remedy"),
        "values": {k: vals.get(k) for k in keys},
        # NOT filtered by `keys`. `--full` is a table-column choice; it was
        # also deciding how much of the parse a machine consumer saw, so
        # `raw["taxi_km"]` read None for a row that parsed fine. `values`
        # still follows the display selection — that one is documented.
        "raw": dict(rec.get("raw") or {}),
        "missing": b["missing"],
        "monthly_budget_usd": b["total"],
        "monthly_savings_usd": budget.savings(vals, b["total"]),
        "rent_to_income_pct": budget.rent_to_income(vals),
        "crosscheck": rec.get("_control"),
    }


def _as_flat(rec: dict, keys: list[str]) -> dict:
    j = _as_json(rec, keys)
    flat = {"slug": j["slug"], "country": j["country"]}
    flat.update(j["values"])
    ctrl = j.get("crosscheck") or {}
    total = j["monthly_budget_usd"]
    local = ctrl.get("local_usd")
    flat.update({
        "budget": total,
        "savings": j["monthly_savings_usd"],
        "rent_to_income_pct": j["rent_to_income_pct"],
        "control_local": local,
        "ratio": round(total / local, 3) if (total and local) else None,
        "missing": ";".join(j["missing"]) or None,
    })
    return flat


def _print_compare(records, keys, args) -> None:
    color = not args.no_color and not args.md
    budgets = [r["_budget"]["total"] for r in records]
    order = sorted(range(len(records)),
                   key=lambda i: (budgets[i] is None, budgets[i] or 0))
    records = [records[i] for i in order]
    budgets = [budgets[i] for i in order]

    names = [f"{render.flag(r.get('country',''))} {r.get('slug','')}".strip()
             for r in records]
    body: list[list[str]] = []

    for key in keys:
        vals = [(r.get("values") or {}).get(key) for r in records]
        best = budget.best_of(vals, key)
        fmt = (render.percent_1 if key in prices.NON_MONEY else render.money)
        body.append([prices.LABELS[key]] +
                    [render.mark_best(fmt(v), v, best, color=color)
                     for v in vals])

    best_b = budget.best_of(budgets, "budget")
    body.append(["**Monthly Budget***" if args.md else "Monthly Budget*"] +
                [render.mark_best(render.money(v), v, best_b, color=color)
                 for v in budgets])

    r2i = [budget.rent_to_income(r.get("values") or {}) for r in records]
    body.append(["Rent/Income %"] +
                [render.mark_best(render.percent(v), v,
                                  budget.best_of(r2i, "rent_to_income"),
                                  color=color) for v in r2i])

    sav = [budget.savings(r.get("values") or {}, b)
           for r, b in zip(records, budgets)]
    body.append(["**Monthly Savings**" if args.md else "Monthly Savings"] +
                [render.mark_best(render.money(v), v,
                                  budget.best_of(sav, "monthly_savings"),
                                  color=color) for v in sav])

    if args.crosscheck:
        locals_ = [(r.get("_control") or {}).get("local_usd") for r in records]
        body.append(["_nomads.com local_"] +
                    [render.money(v) for v in locals_])
        ratios = {r.get("slug"): (b / l if (b and l) else None)
                  for r, b, l in zip(records, budgets, locals_)}
        body.append(["_ours / theirs_"] +
                    ["N/A" if ratios[r.get("slug")] is None
                     else f"{ratios[r.get('slug')]:.2f}" for r in records])
        for city, dev in budget.ratio_anomalies(ratios).items():
            render.note(f"  ANOMALY {city}: ratio is {dev}x the median — "
                        f"re-read this scrape before trusting the row")

    headers = [""] + names
    print(render.markdown_table(headers, body) if args.md
          else render.text_table(headers, body, color=color))

    print()
    foot = ("*rent(outside) + utilities + internet + mobile + transport + "
            "meal x 30; strict — one missing component yields N/A*")
    print(foot if args.md else foot.strip("*"))
    served = {r.get("measurement_system") for r in records
              if r.get("measurement_system")}
    if served:
        conv = any(r.get("converted") for r in records)
        render.note(f"  units: numbeo served {'/'.join(sorted(served))} "
                    f"(it picks by your geography); output is "
                    f"{records[0].get('output_units')}"
                    + (" — sq ft and miles converted" if conv else ""))
    ages = [r.get("_age_s") for r in records if r.get("_age_s") is not None]
    if ages:
        render.note(f"  data age {fmt_age(min(ages))}–{fmt_age(max(ages))} "
                    f"· numbeo.com")
    if args.crosscheck:
        render.note("  control column: nomads.com — shown beside, never "
                    "blended into, our figures")


# -------------------------------------------------------------------- rank --

def cmd_rank(args) -> int:
    view = "country" if args.by == "country" else ("region" if args.region else "city")
    table = rankings.fetch(args.index, view=view, snapshot=args.snapshot,
                           region=args.region, max_age=args.max_age)
    rows = table["rows"]
    if args.match:
        rows = rankings.find_place(table, args.match) or [
            r for r in rows if args.match.lower() in r["place"].lower()]

    cols = table["columns"]
    sort_col = None
    if args.sort:
        for c in cols:
            if args.sort.lower() in c.lower():
                sort_col = c
                break
        if sort_col is None:
            render.note(f"  no column matched --sort '{args.sort}'; "
                        f"available: {cols}")
    if sort_col:
        rows = sorted(
            rows,
            key=lambda r: (r["metrics"].get(sort_col) is None,
                           -(r["metrics"].get(sort_col) or 0)
                           if sort_col in budget.HIGHER_IS_BETTER
                           else (r["metrics"].get(sort_col) or 0)))

    if args.top:
        rows = rows[:args.top]

    show = cols if args.full else cols[:3]
    if args.json:
        render.emit_json({"vertical": table["vertical"], "view": table["view"],
                          "snapshot": table["snapshot"], "url": table["url"],
                          "age_s": table.get("_age_s"), "columns": cols,
                          "count": len(rows), "rows": rows})
        return 0
    if args.csv:
        flat = [{"rank": r["rank"], "place": r["place"], "city": r["city"],
                 "country": r["country"], **r["metrics"]} for r in rows]
        render.emit_csv(flat, ["rank", "place", "city", "country", *cols])
        return 0

    headers = ["#", "Place"] + show
    body = [[str(r["rank"]),
             f"{render.flag(r['country'])} {r['place']}".strip()] +
            [render.number(r["metrics"].get(c)) for c in show] for r in rows]
    print(render.markdown_table(headers, body) if args.md
          else render.text_table(headers, body, color=not args.no_color))
    render.note(f"  {len(rows)} of {len(table['rows'])} rows · "
                f"{table['vertical']} · {table['snapshot']} · "
                f"age {fmt_age(table.get('_age_s') or 0)} · {table['url']}")
    if not args.full and len(cols) > len(show):
        render.note(f"  {len(cols) - len(show)} more columns hidden (--full): "
                    f"{cols[len(show):]}")
    return 0


def cmd_trend(args) -> int:
    data = rankings.trend(args.city, vertical=args.index, column=args.column,
                          limit=args.snapshots, max_age=args.max_age)
    flat = data.get("identical_across_snapshots")
    if flat:
        # Before the numbers, not after. The table is the evidence for this
        # warning, and a reader who meets the evidence first has already
        # formed the wrong conclusion from it.
        render.note(f"  ! every snapshot returned an IDENTICAL table for "
                    f"{args.city} — same metrics and the same row count in "
                    f"all of them. That is not a stable city; that is one "
                    f"table fetched {data['found_in']} times.")
        render.note("    -> `?title=` is being ignored upstream. Verify with "
                    "`citycost rank --snapshot 2014 --top 3` against "
                    "`--snapshot current`; if those agree too, this client "
                    "needs updating and the series above is not history.")
    if args.json:
        render.emit_json(data)
        return 1 if flat else 0
    if data.get("ambiguous"):
        # Checked BEFORE rendering: printing a table of N/A rows and *then*
        # explaining why has already told the reader there was no data.
        render.note(f"  AMBIGUOUS '{args.city}' matches {len(data['ambiguous'])} "
                    f"places: {data['ambiguous']}")
        render.note("  re-run with the full label, e.g. "
                    f"citycost trend \"{data['ambiguous'][0]}\" — choosing one "
                    "for you would splice two different cities into one line")
        return 1
    col = args.column or data["column"]
    headers = ["Snapshot", col or "value", "Rank", "Of"]
    body = []
    for s in data["series"]:
        if not s.get("found"):
            body.append([s["snapshot"], "N/A", "—",
                         s.get("note") or s.get("error", "")[:28]])
            continue
        body.append([s["snapshot"], render.number(s["metrics"].get(col)),
                     str(s["rank"]), str(s["of"])])
    print(render.markdown_table(headers, body) if args.md
          else render.text_table(headers, body, color=not args.no_color))
    render.note(f"  {data['city']} found in {data['found_in']} of "
                f"{data['requested']} snapshots · anchor: {data.get('anchor')} "
                f"· {data['vertical']}")
    if data["found_in"] < data["requested"]:
        render.note("  N/A rows mean the city was not ranked in that snapshot "
                    "— an absence, not a zero")
    return 1 if flat else 0


def cmd_snapshots(args) -> int:
    snaps = rankings.snapshots(args.index, max_age=args.max_age)
    if args.json:
        render.emit_json({"index": args.index, "snapshots": snaps})
        return 0
    print("\n".join(snaps))
    render.note(f"  {len(snaps)} snapshots for {args.index}")
    return 0


def cmd_find(args) -> int:
    from .net import http_get
    import re
    url = ("https://www.numbeo.com/cost-of-living/country_result.jsp"
           f"?country={args.country.replace(' ', '+')}")
    html = http_get(url)
    slugs = sorted({m.rstrip("'\"") for m in
                    re.findall(r"/cost-of-living/in/([A-Za-z0-9\-']+)", html)})
    if args.json:
        render.emit_json({"country": args.country, "url": url,
                          "slugs": slugs, "is_subset": True})
        return 0
    if not slugs:
        render.note(f"  no cities listed for '{args.country}' — check the "
                    f"spelling against {url}")
        return 1
    print("\n".join(slugs))
    render.note(f"  {len(slugs)} cities in Numbeo's {args.country} index")
    render.note("  this index is a SUBSET: a city page can exist without being "
                "listed (measured — Da-Lat serves 55 price rows yet is absent "
                "from the Vietnam index). To settle one city, request it "
                "directly; a slug Numbeo really lacks answers "
                "'Cannot find city id'.")
    return 0


# ------------------------------------------------------------------ doctor --

#: A tiny, stable, uncontroversial page. `doctor` must prove the fetcher
#: *works*, not that a variable is set — an enabled-but-broken escape hatch is
#: discovered at the exact moment it was needed, which is the worst time.
PROBE_URL = "https://example.com/"  # nosec: mock (doctor connectivity canary)
PROBE_ATTEMPTS = 2

#: The text table pads every column to its widest cell, so one 300-character
#: error message turns the whole report into a horizontal scroll and hides the
#: rows either side of it. `--json` keeps the full text.
DETAIL_WIDTH = 120


def _transport_check() -> tuple[str, str, str]:
    cfg = fallback.settings()
    where = [f"mode={cfg.mode} (from {cfg.mode_source})"]
    if cfg.config_status == "foreign":
        where.append(f"{cfg.config_file} exists but declares none of "
                     f"citycost's keys")
    elif cfg.config_status == "unreadable":
        where.append(f"{cfg.config_file} unreadable")
    elif cfg.config_status == "missing":
        where.append(f"no config at {cfg.config_file}")

    if not cfg.get_cmd:
        return ("fallback fetcher", "off",
                f"{'; '.join(where)} — a 429 will fail rather than reroute. "
                f"Set {fallback.GET_CMD_ENV} (and {fallback.POST_CMD_ENV}) "
                f"there to enable it.")
    if cfg.mode == "never":
        return ("fallback fetcher", "off",
                f"configured (from {cfg.get_source}) but {'; '.join(where)}")
    # Two attempts, and say when the first one failed. A residential exit node
    # dying mid-request is ordinary — measured: this probe failed once and the
    # same command succeeded seconds later — so one flaky attempt is not a
    # verdict on the configuration. Two failures is. The data path is still
    # attempted exactly once, because there each retry costs bandwidth; a 559B
    # health check does not.
    attempts = []
    for _ in range(PROBE_ATTEMPTS):
        try:
            body = fallback.run(PROBE_URL, cfg.get_cmd, 30, method="GET")
            break
        except CitycostError as exc:
            attempts.append(f"{exc.message} | {exc.remedy}")
    else:
        return ("fallback fetcher", "FAIL",
                f"{PROBE_ATTEMPTS} attempts failed: {attempts[-1]}")
    flaky = (f"{len(attempts)} of {len(attempts) + 1} attempts failed "
             f"(residential exits are flaky); " if attempts else "")
    have_post = "POST ok" if cfg.post_cmd else "POST NOT configured"
    return ("fallback fetcher", "ok",
            f"{flaky}GET probe {len(body)}B from {PROBE_URL}; {have_post}; "
            f"{'; '.join(where)}")


def cmd_doctor(args) -> int:
    ok = True
    checks = []

    name, status, detail = _transport_check()
    if status == "FAIL":
        ok = False
    checks.append((name, status, detail))

    try:
        info = discover.server_info()
        checks.append(("nomads.com MCP", "ok",
                       f"{info.get('serverInfo',{}).get('name')} "
                       f"proto={info.get('protocolVersion')}"))
        tools = [t["name"] for t in discover.list_tools()]
        checks.append(("nomads.com tools", "ok", ", ".join(tools)))
    except CitycostError as exc:
        ok = False
        checks.append(("nomads.com MCP", "FAIL", exc.message))

    # `max_age=0`, never `args.max_age`. This command's whole question is *is
    # this source reachable right now*, and a cache can answer it — measured
    # 2026-08-25, doctor reported both Numbeo rows `ok` with the network made
    # unreachable, at exit 0. A reachability check its own cache can satisfy
    # examines zero network, which is the same green light as a type-checker
    # over zero files. The word "live" is on the line for the same reason: `ok`
    # cannot tell a reader which of the two it got.
    try:
        t = rankings.fetch("cost-of-living", max_age=0)
        checks.append(("numbeo rankings", "ok",
                       f"live; {len(t['rows'])} cities, "
                       f"{len(t['columns'])} columns"))
    except CitycostError as exc:
        ok = False
        checks.append(("numbeo rankings", "FAIL", exc.message))

    try:
        rec = prices.fetch("Prague", max_age=0)
        got = sum(1 for v in rec["values"].values() if v is not None)
        # TARGETS holds two spellings for every unit-dependent row, so its raw
        # length is not the metric count. Reporting 21/28 made a complete read
        # look like a 75% one.
        total = len(set(prices.TARGETS.values()))
        checks.append(("numbeo prices", "ok",
                       f"live; Prague: {got}/{total} rows priced "
                       f"({rec.get('measurement_system')} served)"))
    except CitycostError as exc:
        ok = False
        checks.append(("numbeo prices", "FAIL", exc.message))

    if args.json:
        render.emit_json({"ok": ok, "checks": [
            {"check": c, "status": s, "detail": d} for c, s, d in checks]})
        return 0 if ok else 1
    print(render.text_table(["Check", "Status", "Detail"],
                            [[c, s, _short(d)] for c, s, d in checks],
                            color=not args.no_color))
    return 0 if ok else 1


def _short(text: str, width: int = DETAIL_WIDTH) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 1] + "\u2026"


def cmd_cache(args) -> int:
    """Report the corpus; delete only when explicitly asked.

    Until 1.4.0 a bare `citycost cache` PRUNED — there was no read-only mode at
    all, so the command a person runs to *look* at the cache deleted part of
    it. Harmless while the cache was an hour of convenience; not harmless once
    it holds a historical corpus that took hundreds of throttled requests and
    cannot be re-taken while the address is banned. Reading is now the default
    and every destructive path is named.
    """
    live_before = net.reads()["live"]
    if args.clear:
        res = clear_cache(include_archive=args.include_archive)
        payload = {"action": "cleared", **res}
        line = (f"removed {res['removed']} cache files"
                + (f" · {res['protected']} archived entries kept "
                   f"(--include-archive removes them too)"
                   if res["protected"] else ""))
    elif args.prune:
        res = prune_cache()
        payload = {"action": "pruned", **res}
        line = (f"pruned {res['removed']} of {res['scanned']} expired cache "
                f"files · {res['protected']} archived entries protected "
                f"· {res['dir']}")
    else:
        res = net.cache_status()
        payload = {"action": "status", **res}
        mb = res["bytes"] / 1_048_576
        line = (f"{res['ordinary']} cached · {res['archived']} archived "
                f"(never pruned) · {mb:.1f} MB · {res['dir']}")

    if getattr(args, "json", False):
        render.emit_json(payload)
    else:
        print(line)
        if payload["action"] == "status":
            if res["schemas"]:
                render.note("  schemas: " + ", ".join(
                    f"{k}={v}" for k, v in sorted(res["schemas"].items())))
            if res["unreadable"]:
                render.note(f"  ! {res['unreadable']} entries could not be "
                            f"parsed and are neither served nor counted as data")
            if res["mtime_skew"]:
                # mtime is not a fact about when a figure was read: a corpus
                # copied between machines arrives with every mtime set to now,
                # so a stale figure announces `age 0s`.
                render.note(f"  ! {res['mtime_skew']} entries have an mtime "
                            f"that disagrees with their recorded fetch time; "
                            f"their reported age is not trustworthy")

    # A cache command that issued a request is a bug, not a user error: it
    # would spend proxy bandwidth, or earn a fresh 429, for somebody who ran a
    # read-only command precisely because they are blocked.
    if net.reads()["live"] > live_before:
        render.note("  ! cache commands must not touch the network, and this "
                    "run did — please report it")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The flag is applied by setting the variable the resolver already reads,
    # so there is exactly one place that decides the mode.
    if getattr(args, "fetch_mode", None):
        os.environ[fallback.MODE_ENV] = args.fetch_mode
    try:
        return args.func(args)
    except CitycostError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        used = fallback.events()
        if used:
            render.note(f"  provenance: {len(used)} request(s) served through "
                        f"the external fetcher, not a direct read")
        # Rung 3: the remedy, attached to the moment it is needed. Passing an
        # explicit --fetch-mode asks a question about the *transport*; a run
        # that made zero live reads answered it from disk and proved nothing
        # about the network. Measured 2026-08-25 on this tool: an agent read
        # `--fetch-mode never` + exit 0 + full figures as "the ban lifted",
        # while the address had six more days to go. The age was printed and
        # skipped — a fact that is reachable is not a fact that arrives.
        mode = getattr(args, "fetch_mode", None)
        if mode and mode != fallback.DEFAULT_MODE and net.reads()["live"] == 0:
            render.note(f"  ! --fetch-mode {mode} was not exercised: every "
                        f"figure came from the local cache, so no transport "
                        f"was tested")
            render.note("    -> add --max-age 0 to make this a live check")


if __name__ == "__main__":
    sys.exit(main())
