"""The fixtures and the traps every harvest test stands on. Not a test module.

One owner for `HarvestCase`, imported by `test_harvest.py`,
`test_harvest_plan.py` and `test_harvest_report.py`. A per-file copy would be
three `setUp`s drifting apart, and the first thing to drift is the per-test
cache directory — after which a resume/rerun test is answered by a neighbour's
files and passes for a reason nobody chose.

Every test built on this is offline. The transport is patched at
`rankings.http_get`, which is the seam the whole module is built around:
harvest calls `rankings.fetch` and nothing lower, so a test that has to reach
past that seam is a test reporting that the design broke.

Where the property under test is "this must NOT happen" — no request before a
refusal, no fetcher call, no request from `plan()` at all — the stub is an
`AssertionError` side effect rather than a plausible return value, stood on
both `rankings.http_get` and `net.http_get` by `HarvestCase.transport_trap`. A
stub that returns something hides both ordering and whether it was called; a
trap fails at the moment the wrong thing occurs. Every refusal test carries
one, because `HarvestAborted` is a `CitycostError`: a run that reaches the
banned address and is thrown out by the 429 satisfies the same `assertRaises`
as the guard that was supposed to stop it before request 1, and the one refusal
test written without a trap was measured doing exactly that.
"""

import contextlib
import datetime
import io
import os
import tempfile
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401  (must be first)
from citycost import fallback, harvest, net, rankings

#: A fixed clock, so which ids may be PINNED is a property of the test rather
#: than of the day it runs. On this date `rankings.is_archival` pins 2025-mid
#: and older and refuses 2026 / 2026-mid — the current half-year, which nothing
#: has measured to be finished.
NOW = datetime.datetime(2026, 8, 25, tzinfo=datetime.timezone.utc).timestamp()

ARCHIVE_IDS = ["2020", "2019", "2018"]


def _snapshot_page(ids) -> str:
    opts = "".join(f'<option value="{i}">{i}</option>' for i in ids)
    return ("<html><body><select name='title'>" + opts +
            "</select><table id='t2'></table></body></html>")


def _table_page(places) -> str:
    """A ranking table whose CONTENT varies with `places`, so two snapshots can
    be told apart by fingerprint rather than only by row count — two snapshots
    can legitimately carry the same number of cities."""
    rows = "".join(
        f"<tr><td></td><td>{p}</td><td>{v:.1f}</td><td>{v / 2:.1f}</td></tr>"
        for p, v in places)
    return (
        "<table id='t2'><thead><tr><th><div>Rank</div></th>"
        "<th><div>City</div></th><th><div>Cost of Living Index</div></th>"
        "<th><div>Rent Index</div></th></tr></thead><tbody>"
        + rows + "</tbody></table>")


ONE_TABLE = _table_page([("Zurich, Switzerland", 123.1),
                         ("Da Nang, Vietnam", 32.4)])

#: What Numbeo serves for a selection it has no table for. `rankings._parse`
#: turns this into a SourceUnavailable that is a *permanent answer about that
#: URL*, not a failure.
NO_TABLE_PAGE = "<html><body><p>Cannot find that selection</p></body></html>"


def distinct_pages(n: int) -> list:
    """n tables that genuinely differ — the normal shape. Measured,
    cost-of-living runs 41 rows in 2009 against 547 in 2026-mid."""
    return [_table_page([(f"City {i}, Country", 100.0 + i)] * (i + 1))
            for i in range(1, n + 1)]


class HarvestCase(unittest.TestCase):
    """Each test gets its own cache directory.

    `_sandbox` pins one for the whole process, which is what keeps the suite
    off the developer's real corpus; this narrows it again per test so a
    resume/rerun test cannot be answered by a neighbour's files.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="citycost-harvest-", dir=_sandbox.SANDBOX)
        self._env = mock.patch.dict(os.environ, {"CITYCOST_CACHE_DIR": self._dir})
        self._env.start()
        self.addCleanup(self._env.stop)
        fallback.reset()
        self.addCleanup(fallback.reset)
        net.reset_reads()
        self.addCleanup(net.reset_reads)

    # -- seeding, always through the module that owns the cache key ---------
    #
    # Nothing here writes a cache file directly. `harvest._target_key` and
    # `harvest._list_key` restate a fact `rankings` owns, and that is the one
    # place this module can be silently wrong: a changed key prefix would make
    # the plan report an empty corpus while 192 files sit on disk, costing 192
    # requests against a source that bans by address. Seeding through the real
    # writer makes every test in this file a round trip, so the drift goes red
    # here rather than on somebody's rate limit.

    def seed_list(self, vertical, ids):
        with mock.patch.object(rankings, "http_get",
                               return_value=_snapshot_page(ids)):
            got = rankings.snapshots(vertical, max_age=0)
        self.assertEqual(got, list(ids))

    def seed_table(self, vertical, snapshot, html=ONE_TABLE):
        with mock.patch.object(rankings, "http_get", return_value=html):
            rankings.fetch(vertical, snapshot=snapshot, max_age=0)

    @contextlib.contextmanager
    def transport_trap(self, why):
        """Stand a trap on the transport, at BOTH names it answers to.

        A refusal test asserts that something does NOT happen, and an
        `assertRaises` alone cannot tell "refused before request 1" from
        "refused later, by a different owner, after spending the address":
        `HarvestAborted` is itself a `CitycostError`, so a run that reaches a
        banned address and is thrown out by the 429 satisfies exactly the same
        assertion the lock was supposed to satisfy. Measured on this file's own
        foreign-host test: with `_holder_is_alive` forced to return False — the
        regression that lets the loop run — it sent a request and still
        reported a pass.

        Both names, because `rankings` binds `http_get` at import
        (`from .net import http_get`): a patch on `net.http_get` alone leaves
        the name harvest actually calls untouched, and a trap nobody can trip
        is the thing this repository keeps finding.
        """
        with contextlib.ExitStack() as stack:
            for owner in (net, rankings):
                stack.enter_context(mock.patch.object(
                    owner, "http_get", side_effect=AssertionError(why)))
            yield

    def stderr_of(self, fn, *a, **kw):
        buf, sink = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(sink):
            fn(*a, **kw)
        return buf.getvalue()

    def plan_one(self, vertical="cost-of-living", **kw):
        kw.setdefault("now", NOW)
        return harvest.plan([vertical], **kw)

