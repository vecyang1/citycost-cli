"""A reachability check that its own cache can answer examines zero network.

`citycost doctor`'s entire contract is *is this source reachable right now*.
It passed `max_age=args.max_age` — default 3600 — to both Numbeo checks, so an
hour-old cache answered the question the command exists to ask. Measured
2026-08-25 with the network made unreachable two different ways:

    CITYCOST_FETCH_MODE=always CITYCOST_FETCH_CMD='/usr/bin/false {url}'
      -> numbeo rankings  ok  558 cities, 6 columns
      -> numbeo prices    ok  Prague: 21/21 rows priced (metric served)

    CITYCOST_CONFIG=/dev/null CITYCOST_FETCH_MODE=never   (a user with no
    fetcher, on an address Numbeo has banned for seven days)
      -> exit 0, every check green

The second is the one that matters: the command you run *because* something is
wrong reported nothing wrong, with a plausible detail line, at exit 0. That is
the same shape as a status page that reports a dead database as calm — the
check did not fail, it was never asked.

This file pins the fix from the outside, through `main()`, because the bug was
not in either fetch function. Both were correct. The defect was the *argument*
doctor chose to call them with, and a test that patched the fetchers would have
asserted over the mock instead of over the choice.
"""

import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from . import _sandbox  # noqa: F401
from citycost import cli
from citycost.errors import SourceUnavailable


class _Dead:
    """A source that answers from nowhere. Any call is an unreachable network.

    A `side_effect` rather than a return value, because the property under test
    is *whether the call happens at all*. A stub that returned a plausible page
    would let a cached run and a live run look identical from here — which is
    precisely the confusion that shipped.
    """

    def __init__(self):
        self.max_ages = []

    def __call__(self, *args, **kwargs):
        self.max_ages.append(kwargs.get("max_age"))
        raise SourceUnavailable("network is down", "n/a", status=429)


def _run_doctor():
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["doctor"])
    return code, out.getvalue() + err.getvalue()


class DoctorReadsLive(unittest.TestCase):

    def test_doctor_asks_both_numbeo_sources_for_a_live_read(self):
        """max_age=0 is the whole fix, so assert on the argument, not the text.

        Asserting only on the rendered word "live" would pass for a doctor that
        printed the label and still read the cache."""
        dead_r, dead_p = _Dead(), _Dead()
        with mock.patch("citycost.rankings.fetch", dead_r), \
             mock.patch("citycost.prices.fetch", dead_p), \
             mock.patch("citycost.cli._transport_check",
                        return_value=("fallback fetcher", "off", "")), \
             mock.patch("citycost.discover.server_info", side_effect=SourceUnavailable("down", "n/a")):
            _run_doctor()
        self.assertEqual(dead_r.max_ages, [0],
                         "doctor asked rankings for a cached answer")
        self.assertEqual(dead_p.max_ages, [0],
                         "doctor asked prices for a cached answer")

    def test_doctor_fails_when_the_sources_are_unreachable(self):
        """Failure propagation, NOT the regression.

        Worth stating plainly: with `fetch` mocked to raise, this passed before
        the fix too. The mock is the thing that made the network unreachable,
        so it also erased the property that shipped broken — real `fetch`
        returning a cached page instead of raising. The test above, which
        asserts on the *argument* doctor chose, is the one that goes red."""
        with mock.patch("citycost.rankings.fetch", _Dead()), \
             mock.patch("citycost.prices.fetch", _Dead()), \
             mock.patch("citycost.cli._transport_check",
                        return_value=("fallback fetcher", "off", "")), \
             mock.patch("citycost.discover.server_info", side_effect=SourceUnavailable("down", "n/a")):
            code, text = _run_doctor()
        self.assertNotEqual(code, 0,
                            "doctor exited 0 with every source unreachable")
        self.assertIn("FAIL", text)

    def test_doctor_can_still_pass(self):
        """The mirror. A gate that cannot go green is as useless as one that
        cannot go red, and this one is easy to over-tighten into always-FAIL."""
        rec = {"values": {v: 1.0 for v in set(cli.prices.TARGETS.values())},
               "measurement_system": "metric"}
        with mock.patch("citycost.rankings.fetch",
                        return_value={"rows": [1] * 558, "columns": [1] * 6}), \
             mock.patch("citycost.prices.fetch", return_value=rec), \
             mock.patch("citycost.cli._transport_check",
                        return_value=("fallback fetcher", "ok", "probe ok")), \
             mock.patch("citycost.discover.server_info",
                        return_value={"serverInfo": {"name": "n"},
                                      "protocolVersion": "p"}), \
             mock.patch("citycost.discover.list_tools",
                        return_value=[{"name": "get_city"}]):
            code, text = _run_doctor()
        self.assertEqual(code, 0, text)
        self.assertIn("21/21", text)


class DoctorSaysTheReadWasLive(unittest.TestCase):
    """Provenance on the line itself. `ok` alone cannot distinguish a live read
    from a cached one, and the reader of a health report has no other way to
    ask — so the report must carry the answer rather than imply it."""

    def test_the_numbeo_rows_are_labelled_live(self):
        rec = {"values": {v: 1.0 for v in set(cli.prices.TARGETS.values())},
               "measurement_system": "metric"}
        with mock.patch("citycost.rankings.fetch",
                        return_value={"rows": [1] * 558, "columns": [1] * 6}), \
             mock.patch("citycost.prices.fetch", return_value=rec), \
             mock.patch("citycost.cli._transport_check",
                        return_value=("fallback fetcher", "ok", "probe ok")), \
             mock.patch("citycost.discover.server_info",
                        return_value={"serverInfo": {"name": "n"},
                                      "protocolVersion": "p"}), \
             mock.patch("citycost.discover.list_tools",
                        return_value=[{"name": "get_city"}]):
            _, text = _run_doctor()
        self.assertEqual(text.count("live"), 2,
                         "both Numbeo checks must state the read was live")

class RawIsNotShapedByADisplayFlag(unittest.TestCase):
    """`--full` selects table columns. It was also silently selecting how much
    of `--json`'s `raw` a machine consumer got: 8 of 21 parsed rows, under a key
    whose name promises the unfiltered parse. `raw["taxi_km"]` was then `None`
    for a row that had been read successfully and thrown away by a rendering
    option — a confident absence, which is the one answer nobody re-checks.

    `values` still follows `--full`; that is a documented display selection and
    a consumer summing it would change meaning if it grew. `raw` is the parse.
    """

    def _payload(self, full: bool) -> dict:
        keys = list(set(cli.prices.TARGETS.values()))
        rec = {"slug": "X", "values": {k: 1.0 for k in keys},
               "raw": {k: "$1.00" for k in keys}, "seen": {},
               "_budget": {"missing": [], "total": 1.0}, "_age_s": 0}
        args = mock.Mock(full=full)
        return cli._as_json(rec, [k for k, _ in cli._display_keys(args)])

    def test_raw_carries_every_parsed_row_without_full(self):
        self.assertEqual(set(self._payload(full=False)["raw"]),
                         set(cli.prices.TARGETS.values()))

    def test_raw_is_identical_with_and_without_full(self):
        self.assertEqual(self._payload(full=False)["raw"],
                         self._payload(full=True)["raw"])

    def test_values_still_follows_the_display_flag(self):
        """The mirror: this must NOT have been widened along with raw."""
        self.assertLess(len(self._payload(full=False)["values"]),
                        len(self._payload(full=True)["values"]))

class TransportFlagMustBeExercised(unittest.TestCase):
    """`--fetch-mode` asks about the network; the cache can answer instead.

    The measured incident is this session's own: `citycost compare Hanoi
    --fetch-mode never` returned exit 0 with 21 complete figures, and that was
    read as "the ban has lifted" while the address had six days left. The run
    was correct — it served a two-minute-old cache — and it printed the age.
    Printing was not enough. A fact that is *reachable* is not a fact that
    *arrives*, so the notice is attached to the condition instead.

    Three cases, because the third is what keeps the other two usable: a
    warning that also fires on healthy input gets muted, and then the two real
    cases are gone too.
    """

    def _run(self, argv, live, cached):
        with mock.patch("citycost.net.reads",
                        return_value={"live": live, "cached": cached}), \
             mock.patch("citycost.cli.cmd_compare", return_value=0):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                cli.main(argv)
        return out.getvalue() + err.getvalue()

    def test_warns_when_an_explicit_mode_read_only_cache(self):
        text = self._run(["compare", "X", "--fetch-mode", "never"],
                         live=0, cached=1)
        self.assertIn("was not exercised", text)
        self.assertIn("--max-age 0", text, "the notice must carry the remedy, "
                                           "not just the diagnosis")

    def test_silent_when_the_transport_was_actually_used(self):
        text = self._run(["compare", "X", "--fetch-mode", "never"],
                         live=1, cached=0)
        self.assertNotIn("was not exercised", text)

    def test_silent_on_a_default_run_that_hit_cache(self):
        """Nobody asked about the transport, so there is nothing to report."""
        text = self._run(["compare", "X"], live=0, cached=1)
        self.assertNotIn("was not exercised", text)

    def test_always_mode_is_covered_too(self):
        """`always` is the same question from the other side; keying the check
        on `never` alone would have covered half the flag."""
        text = self._run(["compare", "X", "--fetch-mode", "always"],
                         live=0, cached=1)
        self.assertIn("was not exercised", text)


if __name__ == "__main__":
    unittest.main()
