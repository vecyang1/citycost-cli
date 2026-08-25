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

import argparse
import sys

from . import __version__, budget, discover, prices, rankings, render
from .errors import CitycostError
from .net import DEFAULT_MAX_AGE, clear_cache, fmt_age, prune_cache

EPILOG = """\
examples:
  citycost discover --max-cost 1200 --region Asia --min-internet 30 --verify
  citycost compare Da-Nang Hanoi Chiang-Mai --md --crosscheck
  citycost rank --index quality-of-life --top 15
  citycost rank --by country --match viet
  citycost trend Da-Nang --snapshots 10
  citycost find Vietnam

sources: numbeo.com (public pages) and nomads.com (MCP endpoint).
Numbeo data is proprietary — cite it, do not redistribute it.
"""


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
    _age_note(res.get("_age_s"), "nomads.com")
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
    _age_note(data.get("_age_s"), "nomads.com")
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
    _age_note(data.get("_age_s"), "nomads.com")
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

    keys = [k for k, _ in _display_keys(args)]
    if args.json:
        render.emit_json([_as_json(r, keys) for r in records])
        return 0
    if args.csv:
        render.emit_csv([_as_flat(r, keys) for r in records],
                        ["slug", "country", *keys, "budget", "savings",
                         "rent_to_income_pct", "control_local", "ratio",
                         "missing"])
        return 0

    _print_compare(records, keys, args)
    return 0


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
        "raw": {k: (rec.get("raw") or {}).get(k) for k in keys},
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
    if args.json:
        render.emit_json(data)
        return 0
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
    return 0


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

def cmd_doctor(args) -> int:
    ok = True
    checks = []

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

    try:
        t = rankings.fetch("cost-of-living", max_age=args.max_age)
        checks.append(("numbeo rankings", "ok",
                       f"{len(t['rows'])} cities, {len(t['columns'])} columns"))
    except CitycostError as exc:
        ok = False
        checks.append(("numbeo rankings", "FAIL", exc.message))

    try:
        rec = prices.fetch("Prague", max_age=args.max_age)
        got = sum(1 for v in rec["values"].values() if v is not None)
        # TARGETS holds two spellings for every unit-dependent row, so its raw
        # length is not the metric count. Reporting 21/28 made a complete read
        # look like a 75% one.
        total = len(set(prices.TARGETS.values()))
        checks.append(("numbeo prices", "ok",
                       f"Prague: {got}/{total} rows priced "
                       f"({rec.get('measurement_system')} served)"))
    except CitycostError as exc:
        ok = False
        checks.append(("numbeo prices", "FAIL", exc.message))

    if args.json:
        render.emit_json({"ok": ok, "checks": [
            {"check": c, "status": s, "detail": d} for c, s, d in checks]})
        return 0 if ok else 1
    print(render.text_table(["Check", "Status", "Detail"],
                            [[c, s, d] for c, s, d in checks],
                            color=not args.no_color))
    return 0 if ok else 1


def cmd_cache(args) -> int:
    if args.clear:
        print(f"removed {clear_cache()} cache files")
    else:
        print(f"pruned {prune_cache()} expired cache files")
    return 0


def _age_note(age, source: str) -> None:
    if age is not None:
        render.note(f"  data age {fmt_age(int(age))} · {source}")


# --------------------------------------------------------------- argparse ---

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="citycost", description=__doc__.split("\n")[0], epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"citycost {__version__}")

    def common(sp, *, cache=True):
        sp.add_argument("--json", action="store_true", help="JSON output")
        sp.add_argument("--no-color", action="store_true")
        if cache:
            sp.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE,
                            metavar="SEC",
                            help=f"reuse cached data younger than this "
                                 f"(default {DEFAULT_MAX_AGE}; 0 forces live)")
        return sp

    sub = p.add_subparsers(dest="command", required=True)

    d = common(sub.add_parser("discover", help="which cities qualify (nomads.com)"))
    d.add_argument("--max-cost", type=float, metavar="USD")
    d.add_argument("--region")
    d.add_argument("--country")
    d.add_argument("--min-internet", type=float, metavar="MBPS")
    d.add_argument("--min-safety", type=float)
    d.add_argument("--min-temp", type=float, metavar="C")
    d.add_argument("--max-temp", type=float, metavar="C")
    d.add_argument("--limit", type=int, default=20)
    d.add_argument("--verify", action="store_true",
                   help="also fetch real Numbeo prices for each result")
    d.set_defaults(func=cmd_discover)

    c = common(sub.add_parser("compare", help="itemised prices, side by side (numbeo)"))
    c.add_argument("cities", nargs="+")
    c.add_argument("--md", action="store_true")
    c.add_argument("--csv", action="store_true")
    c.add_argument("--full", action="store_true", help="every scraped row")
    c.add_argument("--local", action="store_true",
                   help="native currency; no conversion requested")
    c.add_argument("--units", default=prices.DEFAULT_UNITS,
                   choices=["metric", "source"],
                   help="metric (default) normalises whatever Numbeo served; "
                        "'source' keeps it, since Numbeo picks by geography")
    c.add_argument("--crosscheck", action="store_true",
                   help="add nomads.com as an independent control column")
    c.set_defaults(func=cmd_compare)

    r = common(sub.add_parser("rank", help="global rankings (numbeo)"))
    r.add_argument("--index", default="cost-of-living",
                   choices=sorted(rankings.VERTICALS))
    r.add_argument("--by", default="city", choices=["city", "country"])
    r.add_argument("--region", choices=sorted(rankings.REGIONS))
    r.add_argument("--snapshot", help="e.g. 2026-mid, 2020, or 'current'")
    r.add_argument("--top", type=int)
    r.add_argument("--match", help="filter by place name")
    r.add_argument("--sort", help="sort by a column label (substring ok)")
    r.add_argument("--md", action="store_true")
    r.add_argument("--csv", action="store_true")
    r.add_argument("--full", action="store_true", help="all columns")
    r.set_defaults(func=cmd_rank)

    t = common(sub.add_parser("trend", help="one city across snapshots"))
    t.add_argument("city")
    t.add_argument("--index", default="cost-of-living",
                   choices=sorted(rankings.VERTICALS))
    t.add_argument("--column", help="which index column to plot")
    t.add_argument("--snapshots", type=int, default=12)
    t.add_argument("--md", action="store_true")
    t.set_defaults(func=cmd_trend)

    s = common(sub.add_parser("snapshots", help="available historical snapshots"))
    s.add_argument("--index", default="cost-of-living",
                   choices=sorted(rankings.VERTICALS))
    s.set_defaults(func=cmd_snapshots)

    f = common(sub.add_parser("find", help="which slugs Numbeo has for a country"),
               cache=False)
    f.add_argument("country")
    f.set_defaults(func=cmd_find)

    ci = common(sub.add_parser("city", help="nomads.com detail for one slug"))
    ci.add_argument("slug")
    ci.set_defaults(func=cmd_city)

    m = common(sub.add_parser("meetups", help="upcoming nomad meetups"))
    m.add_argument("--city")
    m.add_argument("--country")
    m.add_argument("--days-ahead", type=int)
    m.add_argument("--limit", type=int, default=20)
    m.set_defaults(func=cmd_meetups)

    do = common(sub.add_parser("doctor", help="check every source is reachable"))
    do.set_defaults(func=cmd_doctor)

    ca = sub.add_parser("cache", help="prune or clear the local cache")
    ca.add_argument("--clear", action="store_true")
    ca.set_defaults(func=cmd_cache)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CitycostError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
