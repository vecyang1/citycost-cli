"""The CLI surface: every subcommand, flag and default, in one place.

Split out of `cli.py` when that file reached this project's 800-line ceiling and
`movers` would have breached it. The seam is real rather than arithmetic — this
module says what the command line *is*, `cli.py` says what each command *does* —
and there is still exactly one parser, so a flag cannot be added to a second
surface and diverge from the first.

`cli` re-exports `build_parser`, so `cli.build_parser()` keeps working for every
existing caller and test.
"""

from __future__ import annotations

import argparse

from . import __version__, fallback, prices, rankings
from .net import DEFAULT_MAX_AGE

EPILOG = """\
examples:
  citycost discover --max-cost 1200 --region Asia --min-internet 30 --verify
  citycost compare Da-Nang Hanoi Chiang-Mai --md --crosscheck
  citycost rank --index quality-of-life --top 15
  citycost rank --by country --match viet
  citycost trend Da-Nang --snapshots 10
  citycost movers --from 2019 --to current --top 15
  citycost find Vietnam

sources: numbeo.com (public pages) and nomads.com (MCP endpoint).
Numbeo data is proprietary — cite it, do not redistribute it.
"""


def build_parser() -> argparse.ArgumentParser:
    """The whole command line, built once.

    The `cli` import is function-local and must stay that way: `cli`
    imports this module to re-export `build_parser`, so a module-level
    import would be circular. Resolving the command functions here — rather
    than accepting them as an argument — keeps ONE table mapping a
    subcommand to its implementation. A second table is how a flag gets
    added to one surface and not the other.
    """
    from . import cli

    p = argparse.ArgumentParser(
        prog="citycost", description=cli.__doc__.split("\n")[0], epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"citycost {__version__}")

    def common(sp, *, cache=True):
        sp.add_argument("--json", action="store_true", help="JSON output")
        sp.add_argument("--no-color", action="store_true")
        sp.add_argument("--fetch-mode", choices=list(fallback.MODES),
                        default=None,
                        help="auto (default) tries direct and falls back to "
                             "the configured fetcher when a source refuses "
                             "this client; never stays direct; always skips "
                             "the direct attempt")
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
    d.set_defaults(func=cli.cmd_discover)

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
    c.set_defaults(func=cli.cmd_compare)

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
    r.set_defaults(func=cli.cmd_rank)

    t = common(sub.add_parser("trend", help="one city across snapshots"))
    t.add_argument("city")
    t.add_argument("--index", default="cost-of-living",
                   choices=sorted(rankings.VERTICALS))
    t.add_argument("--column", help="which index column to plot")
    t.add_argument("--snapshots", type=int, default=12)
    t.add_argument("--md", action="store_true")
    t.set_defaults(func=cli.cmd_trend)

    s = common(sub.add_parser("snapshots", help="available historical snapshots"))
    s.add_argument("--index", default="cost-of-living",
                   choices=sorted(rankings.VERTICALS))
    s.set_defaults(func=cli.cmd_snapshots)

    f = common(sub.add_parser("find", help="which slugs Numbeo has for a country"),
               cache=False)
    f.add_argument("country")
    f.set_defaults(func=cli.cmd_find)

    ci = common(sub.add_parser("city", help="nomads.com detail for one slug"))
    ci.add_argument("slug")
    ci.set_defaults(func=cli.cmd_city)

    m = common(sub.add_parser("meetups", help="upcoming nomad meetups"))
    m.add_argument("--city")
    m.add_argument("--country")
    m.add_argument("--days-ahead", type=int)
    m.add_argument("--limit", type=int, default=20)
    m.set_defaults(func=cli.cmd_meetups)

    do = common(sub.add_parser("doctor", help="check every source is reachable"))
    do.set_defaults(func=cli.cmd_doctor)

    ca = sub.add_parser("cache", help="report, prune or clear the local cache")
    # Reading is the default and every destructive path is named. Before 1.4.0
    # the no-flag form pruned, so looking at the cache deleted part of it.
    ca.add_argument("--prune", action="store_true",
                    help="remove ordinary entries older than the retention "
                         "window; archived snapshots are never pruned")
    ca.add_argument("--clear", action="store_true",
                    help="remove ordinary entries now")
    ca.add_argument("--include-archive", action="store_true",
                    help="with --clear, also delete pinned historical "
                         "snapshots — hundreds of throttled requests that "
                         "cannot be re-taken while the address is banned")
    ca.add_argument("--json", action="store_true", help="JSON output")
    ca.set_defaults(func=cli.cmd_cache)
    return p
