"""The two commands that read the whole ranking panel rather than one row.

Split from `cli.py` when that file passed this project's 800-line ceiling.
`main()` is still the only entry point and these are still `cli.cmd_*`
attributes — they are imported by name there, which is what the parser resolves
and what the orphaned-command test enumerates.
"""

from __future__ import annotations

from . import harvest, movers, render


def _pct(value, *, of=100) -> str:
    """A percentage, or "unknown" — never a number standing in for one.

    `movers` returns `None` for an overlap it could not compute (an empty side
    has no denominator), which is the correct answer. Interpolating that with
    `:.0f` raises TypeError from inside the very branch that exists to REPORT a
    degenerate run — so the honest refusal crashed instead of printing, on
    exactly the input it was written for. Measured end to end.
    """
    if value is None:
        return "unknown"
    return f"{value * of if of != 100 else value:.0f}%" if of == 100 \
        else f"{value:.0%}"


def cmd_movers(args) -> int:
    """Which cities moved most between two snapshots.

    Renders NO better/worse verdict and never calls `render.mark_best`, which
    every other table in this CLI uses. The sign's desirability depends on the
    column AND the reader: a rent index rising is bad for a renter and good for
    a landlord, a crime index falling is good, a salary index falling is bad,
    and `Gross Rental Yield` has no single answer at all. This repo already
    shipped one "lower is better" rule that marked the worst-paying city as
    having the best salary, so a verdict this command cannot justify is one it
    must not render.
    """
    machine = args.json or args.csv
    data = movers.compare_snapshots(
        args.index, frm=args.frm, to=args.to, column=args.column,
        view="region" if args.region else "city", region=args.region,
        max_age=args.max_age)
    direction = ("rising" if args.rising else
                 "falling" if args.falling else "both")
    top = args.top if args.top is not None else (
        None if machine else movers.DEFAULT_TOP_TEXT)
    data = movers.present(data, order=args.order, direction=direction, top=top)
    result = movers.verdict(data)
    frm, to, st = data["from"], data["to"], data["stats"]

    # Warnings BEFORE the numbers, as cmd_trend does. The table is the evidence
    # for them, and a reader who meets the evidence first has already drawn the
    # wrong conclusion from it.
    probe = data.get("probe") or {}
    if data["drift"] == "snapshot_parameter_ignored":
        render.note(f"  ! --from {frm['snapshot']} and --to {to['snapshot']} "
                    f"returned the IDENTICAL table ({frm['rows']} rows) — and "
                    f"so did {probe.get('snapshot')}, the oldest published "
                    f"snapshot that is neither side. That is not a stable "
                    f"world; that is one table fetched three times.")
        render.note(f"    -> `?title=` is being ignored upstream. Check "
                    f"`citycost rank --snapshot {data['probe']['snapshot']} "
                    f"--top 3` against `citycost rank --top 3`; if those agree "
                    f"too, every delta below is 0 for a reason that has "
                    f"nothing to do with these cities.")
    elif data["drift"] == "same_published_table":
        render.note(f"  ! '{frm['snapshot']}' and '{to['snapshot']}' serve the "
                    f"SAME published table ({frm['rows']} rows). The oldest "
                    f"snapshot differs, so the source is behaving — these two "
                    f"ids are one question asked twice.")
        render.note(f"    -> pick two the source distinguishes: "
                    f"`citycost snapshots --index {args.index}`")
    elif data["drift"] == "identical_tables_cause_unknown":
        # A state with no note is a state the reader cannot act on. The run
        # still refuses — the tables ARE identical and every delta is 0 — but
        # the two causes have opposite fixes and neither was observed, so
        # naming one would send the reader to the wrong subsystem.
        render.note(f"  ! --from {frm['snapshot']} and --to {to['snapshot']} "
                    f"returned the IDENTICAL table ({frm['rows']} rows), and "
                    f"every other published id of '{args.index}' is one of "
                    f"those two — so nothing here can tell 'these two ids name "
                    f"one table' from '`?title=` is being ignored', and those "
                    f"have opposite fixes.")
        render.note(f"    -> ask a third id: "
                    f"`citycost snapshots --index {args.index}`")
    for flag in data["partial_drift"]:
        render.note(f"  ! {flag}: '{data['column']}' shows "
                    f"{st['distinct_delta_values']} distinct delta value(s) "
                    f"over {st['with_metric_in_both']} comparable cities. Real "
                    f"history over hundreds of cities produces hundreds.")
    if data["join"]["below_floor"]:
        render.note(f"  ! only {data['joined']} cities joined "
                    f"({_pct(data['join']['overlap_pct_of_smaller'])} of the "
                    f"smaller table; floor "
                    f"{_pct(data['join']['floor_pct'])}) — the place-label "
                    f"format has probably changed upstream, so the key stopped "
                    f"matching and the rows below are an unrepresentative "
                    f"sample of {frm['rows']}/{to['rows']}.")
    if st["with_metric_in_both"] == 0:
        render.note(f"  ! nothing was COMPARED: {data['joined']} cities joined "
                    f"but none carries '{data['column']}' in both snapshots. "
                    f"That is not 'nothing moved'.")
    if data["rebase_suspected"]:
        render.note(f"  ! {_pct(st['same_direction_share'], of=1)} of movers went the "
                    f"same way (median {render.signed(st['median_delta'])}) — "
                    f"that is a rebase or a redefined column, not "
                    f"{st['with_metric_in_both']} cities moving together.")

    if args.json:
        render.emit_json(data)
        return result["exit"]
    if args.csv:
        # Never an empty list: emit_csv returns early on one and writes no
        # header at all, so "no movers" and "nothing was read" would be the
        # same zero-byte file.
        if not data["rows"]:
            render.note("  ! nothing was read: both snapshots parsed to zero "
                        "rows, so there is no union to write")
            return max(result["exit"], 1)
        render.emit_csv(data["rows"], movers.CSV_COLUMNS)
        return result["exit"]

    headers = ["Place", frm["snapshot"], to["snapshot"], "Δ", "%", "Rank"]
    body = [[f"{render.flag(r['country'])} {r['place']}".strip(),
             render.number(r["value_from"]), render.number(r["value_to"]),
             render.signed(r["delta"]), render.signed(r["pct"]),
             f"{r['rank_from']}/{r['of_from']} → {r['rank_to']}/{r['of_to']}"]
            for r in data["movers"]]
    print(render.markdown_table(headers, body) if args.md
          else render.text_table(headers, body, color=not args.no_color))
    print()
    print(data["basis"])
    render.note(f"  {data['joined']} cities in both ({frm['rows']} → "
                f"{to['rows']} rows, "
                f"{_pct(data['join']['overlap_pct_of_smaller'])} of the "
                f"smaller) · {data['column']} · {data['vertical']}")
    render.age_note(frm["age_s"], f"{frm['snapshot']} · {frm['url']}")
    render.age_note(to["age_s"], f"{to['snapshot']} · {to['url']}")
    if data["truncated"]:
        render.note(f"  showing {data['shown']} of "
                    f"{data['matched_direction']} (--top to change)")
    # Unconditional: a symmetric explosion in these two counts is the only
    # observer for a broken join, so no flag suppresses them.
    if data["only_in_to"]:
        render.note(f"  {len(data['only_in_to'])} cities are NEW since "
                    f"{frm['snapshot']} and cannot have moved: "
                    f"{data['only_in_to'][:movers.SAMPLE_N]}")
    if data["only_in_from"]:
        render.note(f"  {len(data['only_in_from'])} were DELISTED and could "
                    f"have moved further before dropping out: "
                    f"{data['only_in_from'][:movers.SAMPLE_N]}")
    if data["duplicate_keys"]:
        render.note(f"  {len(data['duplicate_keys'])} label collisions "
                    f"excluded from the join")
    return result["exit"]


def cmd_harvest(args) -> int:
    """Prefetch every archive ranking snapshot, showing the plan first.

    Every refusal lives in `harvest` — the lock, the unknown denominator, the
    `--fetch-mode always` bar — and `main()` already maps a `CitycostError` to
    exit 2. The `HarvestAborted` catch exists only so that `--json` still
    receives the partial report on the runs most worth inspecting.
    """
    report = harvest.plan(args.indexes or None, max_age=args.max_age)
    if not args.json:
        harvest.render_plan(report, color=not args.no_color)
    if not args.execute:
        if args.json:
            render.emit_json(report)
        return harvest.exit_code(report)
    try:
        report = harvest.run(report)
    except harvest.HarvestAborted as exc:
        if args.json:
            render.emit_json(exc.report)
        render.note(str(exc))
        return 2
    if args.json:
        render.emit_json(report)
    else:
        harvest.render_report(report)
    return harvest.exit_code(report)
