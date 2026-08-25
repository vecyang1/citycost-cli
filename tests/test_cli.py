import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from . import _sandbox  # noqa: F401
import pathlib
import re

from citycost import cli, movers, panel, prices, rankings
from .test_htmlparse import RANKING_HTML
from .test_prices import PAGE


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _subparsers_action(p):
    """argparse exposes its subcommands only through a private action.

    Worth reaching for anyway: the alternative is a tuple of names in this
    file, which grades whatever somebody remembered to add to it. A
    subcommand added later would then be ungraded while this test still
    reported green — the denominator that silently narrows.
    """
    subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(subs) == 1, f"expected one subparsers action, got {len(subs)}"
    return subs[0]


class TestParser(unittest.TestCase):
    def test_every_subcommand_is_reachable(self):
        p = cli.build_parser()
        names = sorted(_subparsers_action(p).choices)
        # Report the denominator. A selector that silently stops finding
        # subcommands would otherwise pass this loop by ranging over nothing.
        self.assertGreater(len(names), 1, names)
        for cmd in names:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(p.parse_args([cmd] + _min_args(cmd)))

    def test_every_command_function_is_wired_to_a_subcommand(self):
        """An implemented command nobody can invoke is an orphaned feature.

        Both sides are enumerated independently — the parser's own choices and
        the `cmd_*` functions defined in `cli` — so this fails in both
        directions: a command implemented and never wired, and a subcommand
        wired to a function that no longer exists. A third hardcoded list here
        would only ever grade itself.
        """
        implemented = {n for n in vars(cli) if n.startswith("cmd_")
                       and callable(getattr(cli, n))}
        wired = {sp.get_default("func").__name__
                 for sp in _subparsers_action(cli.build_parser()).choices.values()}
        self.assertEqual(implemented, wired)

    def test_no_subcommand_is_an_error_not_a_silent_success(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])


def _min_args(cmd):
    # `movers` needs --from because a reversed base is invisible in the
    # output: `--from current --to 2019` inverts every sign and still renders a
    # complete, plausible table. Requiring it is the guard.
    return {"compare": ["Prague"], "trend": ["Prague"], "find": ["Vietnam"],
            "city": ["prague-czech-republic"],
            "movers": ["--from", "2019"]}.get(cmd, [])


class TestCompareOutput(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(prices, "http_get", return_value=PAGE)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_json_is_pipe_clean_while_notes_go_to_stderr(self):
        code, out, err = run(["compare", "Da-Nang", "--json", "--max-age", "0"])
        self.assertEqual(code, 0)
        data = json.loads(out)          # raises if a note leaked into stdout
        self.assertEqual(data[0]["slug"], "Da-Nang")
        self.assertIn("Da-Nang", err)

    def test_an_unpriced_component_makes_the_budget_null_not_a_short_sum(self):
        _, out, err = run(["compare", "Da-Nang", "--json", "--max-age", "0"])
        rec = json.loads(out)[0]
        self.assertIsNone(rec["monthly_budget_usd"])
        self.assertIn("transport_pass", rec["missing"])
        self.assertIn("NOTE", err)

    def test_csv_keeps_declared_columns_even_when_empty(self):
        _, out, _ = run(["compare", "Da-Nang", "--csv", "--max-age", "0"])
        header = out.splitlines()[0]
        for col in ("budget", "savings", "control_local", "ratio", "missing"):
            self.assertIn(col, header)

    def test_markdown_mode_emits_a_table(self):
        _, out, _ = run(["compare", "Da-Nang", "--md", "--max-age", "0"])
        self.assertIn("|---", out)

    def test_one_bad_name_among_several_does_not_abort_the_sweep(self):
        """A sweep must not abort because one of ten names was mistyped.

        This test used to pass a single city and assert exit 0, which is a
        narrower subject than its own sentence: with one name and that name
        wrong, *nothing* was read, and exiting 0 hands a `--json` consumer a
        payload of nulls under a success code. One good name is now in the
        fixture, so the claim and the subject match.
        """
        pages = {"Da-Nang": PAGE, "Xx": "<html>Cannot find city id</html>"}
        with mock.patch.object(
                prices, "http_get",
                side_effect=lambda url, **kw: next(
                    v for k, v in pages.items() if f"/{k}" in url)):
            code, out, err = run(["compare", "Da-Nang", "Xx", "--json",
                                  "--max-age", "0"])
        self.assertEqual(code, 0)
        errors = [r.get("error") for r in json.loads(out)]
        self.assertIn("Cannot find", err + " ".join(e or "" for e in errors))

    def test_the_only_name_being_wrong_is_a_failed_run(self):
        with mock.patch.object(prices, "http_get",
                               return_value="<html>Cannot find city id</html>"):
            code, out, err = run(["compare", "Xx", "--json", "--max-age", "0"])
        self.assertEqual(code, 1)
        self.assertIn("Cannot find", err + json.loads(out)[0]["error"])


class TestRankOutput(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(rankings, "http_get",
                                       return_value=RANKING_HTML)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_json_carries_the_columns_it_actually_read(self):
        code, out, _ = run(["rank", "--json", "--max-age", "0"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["columns"],
                         ["Cost of Living Index", "Rent Index"])

    def test_top_truncates_and_the_note_says_so(self):
        _, out, err = run(["rank", "--top", "2", "--max-age", "0"])
        self.assertIn("2 of 3 rows", err)

    def test_match_filters(self):
        _, out, _ = run(["rank", "--match", "zurich", "--json", "--max-age", "0"])
        self.assertEqual(json.loads(out)["count"], 1)

    def test_an_unmatched_sort_column_warns_instead_of_silently_not_sorting(self):
        _, _, err = run(["rank", "--sort", "nonsense", "--max-age", "0"])
        self.assertIn("no column matched", err)

    def test_csv_includes_every_index_column(self):
        _, out, _ = run(["rank", "--csv", "--max-age", "0"])
        self.assertIn("Cost of Living Index", out.splitlines()[0])


class TestExitCodes(unittest.TestCase):
    def test_a_source_failure_is_exit_2_with_a_remedy(self):
        from citycost.errors import SourceUnavailable
        with mock.patch.object(rankings, "http_get",
                               side_effect=SourceUnavailable("down", "wait")):
            code, _, err = run(["rank", "--max-age", "0"])
        self.assertEqual(code, 2)
        self.assertIn("wait", err)


class CompareExitStatus(unittest.TestCase):
    """A run where nothing could be read must not exit 0.

    The failure it prevents is quiet: a blocked source produces a complete,
    well-shaped `--json` payload full of nulls, and a caller that checks only
    the exit code records "these cities are unpriced" for cities that are in
    fact perfectly well priced.
    """

    def _run(self, records):
        with mock.patch.object(cli.prices, "try_fetch",
                               side_effect=lambda slug, **kw: records[slug]):
            with mock.patch.object(cli.render, "note"):
                with mock.patch("sys.stdout", io.StringIO()):
                    return cli.main(["compare", *records, "--json"])

    def test_every_city_failing_exits_nonzero(self):
        self.assertEqual(self._run({
            "Hanoi": {"slug": "Hanoi", "error": "rate-limited", "remedy": "x"},
            "Da-Nang": {"slug": "Da-Nang", "error": "rate-limited", "remedy": "x"},
        }), 1)

    def test_one_city_failing_among_several_still_exits_zero(self):
        self.assertEqual(self._run({
            "Hanoi": {"slug": "Hanoi", "values": {"cheap_meal": 2.0},
                      "country": "Vietnam", "currency": "USD"},
            "Nowhere": {"slug": "Nowhere", "error": "no such slug", "remedy": "x"},
        }), 0)


class DoctorTransportCheck(unittest.TestCase):
    """`doctor` must prove the fetcher works, and must not cry wolf.

    A residential exit node dying mid-request is ordinary — measured: this very
    probe failed once and the same command succeeded seconds later. Reporting
    FAIL on one flaky attempt trains its reader to ignore the row, which is
    worse than not having the row.
    """

    def test_a_single_flaky_attempt_is_not_a_verdict(self):
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise cli.CitycostError("exit node died", "retry")
            return "x" * 559

        with mock.patch.object(cli.fallback, "run", flaky):
            with mock.patch.object(cli.fallback, "settings",
                                   return_value=_cfg(get="f {url}")):
                name, status, detail = cli._transport_check()
        self.assertEqual(status, "ok")
        self.assertIn("1 of 2 attempts failed", detail)

    def test_two_failures_is_a_verdict(self):
        with mock.patch.object(cli.fallback, "run",
                               side_effect=cli.CitycostError("dead", "fix it")):
            with mock.patch.object(cli.fallback, "settings",
                                   return_value=_cfg(get="f {url}")):
                name, status, detail = cli._transport_check()
        self.assertEqual(status, "FAIL")
        self.assertIn("2 attempts failed", detail)

    def test_no_fetcher_is_off_not_failed(self):
        with mock.patch.object(cli.fallback, "settings",
                               return_value=_cfg(get="")):
            name, status, detail = cli._transport_check()
        self.assertEqual(status, "off")
        self.assertIn("CITYCOST_FETCH_CMD", detail)

    def test_a_long_detail_cannot_wreck_the_table(self):
        """The text table pads every column to its widest cell, so one 300-char
        error turns the report into a horizontal scroll and hides its
        neighbours."""
        self.assertEqual(len(cli._short("x" * 400)), cli.DETAIL_WIDTH)
        self.assertEqual(cli._short("short"), "short")
        self.assertEqual(cli._short("a\n  b"), "a b")


class _cfg:
    """Minimal stand-in for fallback.Settings."""

    def __init__(self, get="", post="", mode="auto"):
        self.get_cmd, self.post_cmd, self.mode = get, post, mode
        self.get_source = self.post_source = self.mode_source = "config"
        self.config_status = "missing"
        self.config_file = "/tmp/nowhere/fetch.conf"

    @property
    def available(self):
        return bool(self.get_cmd)

    def cmd_for(self, method):
        return self.post_cmd if method.upper() == "POST" else self.get_cmd


class EveryCommandSpeaksJson(unittest.TestCase):
    """README's first paragraph sells this CLI as agent-first: "Every command
    speaks `--json`". It was false for `cache`, the only subparser not built
    through `common()` — and false in the direction an agent finds by crashing
    with exit 2 mid-run, since nothing announces which commands are exempt.

    Ranges over the parser's own subcommand registry rather than a list written
    here, so a subcommand added next year is either covered or visibly absent.
    The count is asserted too: `0 subcommands checked` and `all correct` are
    otherwise the same green.
    """

    def _subcommands(self):
        parser = cli.build_parser()
        choices = [a.choices for a in parser._subparsers._group_actions
                   if getattr(a, "choices", None)]
        return dict(choices[0])

    def test_the_registry_was_actually_read(self):
        self.assertGreaterEqual(len(self._subcommands()), 10,
                                "subcommand registry did not parse; every "
                                "assertion below would be vacuous")

    def test_each_one_accepts_json(self):
        for name, sub in self._subcommands().items():
            with self.subTest(command=name):
                flags = {o for a in sub._actions for o in a.option_strings}
                self.assertIn("--json", flags,
                              f"`citycost {name} --json` exits 2; the README "
                              f"promises every command speaks it")


class TestEveryDriftStateIsRendered(unittest.TestCase):
    """A drift value the CLI has no sentence for is a refusal the reader cannot
    act on.

    Measured: `identical_tables_cause_unknown` shipped with an exit code, a
    `--json` field and NO stderr note — so a text-mode user saw an empty table
    and a non-zero exit with nothing said. The denominator is `movers`' own
    vocabulary rather than a list retyped here, so a fourth value added later
    is either rendered or visibly missing.
    """

    def _drift_values(self):
        src = pathlib.Path(movers.__file__).read_text(encoding="utf-8")
        found = set(re.findall(r'drift == "([a-z_]+)"', src))
        found |= set(re.findall(r'drift = "([a-z_]+)"', src))
        return found

    def test_the_vocabulary_is_not_empty(self):
        # A selector that stops matching would otherwise pass the loop below
        # by ranging over nothing.
        self.assertGreaterEqual(len(self._drift_values()), 3,
                                self._drift_values())

    def test_each_one_has_a_sentence_in_the_renderer(self):
        src = pathlib.Path(panel.__file__).read_text(encoding="utf-8")
        missing = [d for d in sorted(self._drift_values()) if d not in src]
        self.assertEqual(missing, [], f"drift states with no note: {missing}")


class TestPanelPercentages(unittest.TestCase):
    """An unknown percentage must print as unknown, not crash the branch that
    exists to report it.

    `movers` returns None for an overlap it could not compute — an empty side
    has no denominator, and that is the correct answer rather than a zero.
    Interpolating it with `:.0f` raises TypeError from inside the honest
    refusal, so the degenerate run this code was written for was the one input
    that killed it.
    """

    def test_a_known_share_renders(self):
        self.assertEqual(panel._pct(91.0), "91%")
        self.assertEqual(panel._pct(0.93, of=1), "93%")

    def test_an_unknown_share_renders_as_unknown_not_zero(self):
        self.assertEqual(panel._pct(None), "unknown")
        self.assertEqual(panel._pct(None, of=1), "unknown")

    def test_no_percentage_in_the_panel_renderer_formats_a_raw_value(self):
        """The decidable half: a future `:.0f` on a join figure reintroduces
        the crash, and only a reader would notice. Ranged over the file rather
        than over the two call sites that exist today."""
        src = pathlib.Path(panel.__file__).read_text(encoding="utf-8")
        for bad in ("overlap_pct_of_smaller']:.", "floor_pct']:.",
                    "same_direction_share']:."):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
