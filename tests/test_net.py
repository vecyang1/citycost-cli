import os
import tempfile
import unittest
from unittest import mock

from . import _sandbox
from citycost import net
from citycost.errors import SourceUnavailable


class TestDecoding(unittest.TestCase):
    def test_a_utf8_bom_is_stripped_rather_than_welded_to_the_first_field(self):
        """A BOM decoded as latin-1 becomes three characters glued to the first
        header cell, and that column then reads as empty forever."""
        body = '﻿"title","price"\n"a","1"\n'.encode("utf-8")
        self.assertTrue(net._decode(body, "text/csv").startswith('"title"'))

    def test_undeclared_charset_does_not_fall_back_to_latin1(self):
        text = net._decode("café ₫60,000".encode("utf-8"), "text/html")
        self.assertIn("₫60,000", text)

    def test_undecodable_bytes_degrade_rather_than_raise(self):
        self.assertIsInstance(net._decode(b"\xff\xfe\x00bad", "text/html"), str)


class TestRetryPolicy(unittest.TestCase):
    """Driven through the real raise site, not through hand-made exceptions.

    These tests used to build a `SourceUnavailable` whose *message* contained
    "HTTP 429" — which passed for as long as the retry decision was a substring
    search, and would have kept passing after the decision moved to a status
    field while the production path quietly retried every rate limit. A test
    that constructs the error itself is testing its own fixture; letting
    `urlopen` raise a real `HTTPError` tests the client.
    """

    @staticmethod
    def _http_error(code):
        import urllib.error
        return urllib.error.HTTPError("http://t/", code, "no", {}, None)

    def _count_attempts(self, code):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise self._http_error(code)

        with mock.patch.object(net.urllib.request, "urlopen", boom):
            with mock.patch.object(net.time, "sleep"):
                with self.assertRaises(SourceUnavailable) as ctx:
                    net.http_get("http://t/")
        return len(calls), ctx.exception

    def test_a_rate_limit_is_never_retried(self):
        """Retrying a 429 is what makes a rate limit worse."""
        n, exc = self._count_attempts(429)
        self.assertEqual(n, 1)
        self.assertEqual(exc.status, 429)

    def test_a_4xx_is_never_retried(self):
        n, exc = self._count_attempts(404)
        self.assertEqual(n, 1)
        self.assertEqual(exc.status, 404)

    def test_a_5xx_is_still_retried(self):
        """The other direction: a 500 is a blip and giving up on the first one
        reports 'unreachable' about a site that is up."""
        n, exc = self._count_attempts(500)
        self.assertEqual(n, net.RETRIES)
        self.assertEqual(exc.status, 500)

    def test_the_retry_decision_survives_a_reworded_message(self):
        """The regression guard for the substring era: an error whose prose
        says nothing about a status must still be refused on its number."""
        exc = SourceUnavailable("the sky is falling", "", status=429)
        calls = []

        def boom():
            calls.append(1)
            raise exc

        with self.assertRaises(SourceUnavailable):
            net._with_retry(boom, what="t")
        self.assertEqual(len(calls), 1)

    def test_a_transient_failure_is_retried(self):
        """Measured: the first connect under load timed out and the immediate
        retry succeeded, so never-retry would be wrong in the other direction."""
        calls = []

        def blip():
            calls.append(1)
            if len(calls) < 2:
                raise SourceUnavailable("timed out", "check network")
            return "ok"

        with mock.patch.object(net.time, "sleep"):
            self.assertEqual(net._with_retry(blip, what="t"), "ok")
        self.assertEqual(len(calls), 2)

    def test_exhausting_retries_reports_the_last_remedy_not_a_generic_one(self):
        def always():
            raise SourceUnavailable("timed out", "check network connectivity")

        with mock.patch.object(net.time, "sleep"):
            with self.assertRaises(SourceUnavailable) as c:
                net._with_retry(always, what="t")
        self.assertIn("check network connectivity", c.exception.remedy)


class TestThrottle(unittest.TestCase):
    def test_the_second_hit_to_a_host_waits(self):
        slept = []
        with mock.patch.object(net.time, "sleep", slept.append):
            net._last_hit.clear()
            net._throttle("https://www.numbeo.com/a")
            net._throttle("https://www.numbeo.com/b")
        self.assertTrue(slept and slept[0] > 0)

    def test_a_different_host_is_not_delayed_by_the_first(self):
        slept = []
        with mock.patch.object(net.time, "sleep", slept.append):
            net._last_hit.clear()
            net._throttle("https://www.numbeo.com/a")
            net._throttle("https://nomads.com/mcp")
        self.assertEqual(slept, [])


class TestCache(unittest.TestCase):
    def test_a_hit_returns_the_payload_without_calling_produce(self):
        calls = []
        net.cached_json("k1", "v1", 3600, lambda: (calls.append(1) or {"a": 1}))
        payload, age, cached = net.cached_json("k1", "v1", 3600,
                                               lambda: (calls.append(1) or {"a": 2}))
        self.assertEqual((payload, cached), ({"a": 1}, True))
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(age, int)

    def test_a_schema_change_discards_the_entry_rather_than_serving_stale_keys(self):
        net.cached_json("k2", "v1", 3600, lambda: {"a": 1})
        payload, _, cached = net.cached_json("k2", "v2", 3600, lambda: {"a": 2})
        self.assertEqual((payload, cached), ({"a": 2}, False))

    def test_max_age_zero_always_produces(self):
        net.cached_json("k3", "v1", 3600, lambda: {"a": 1})
        _, _, cached = net.cached_json("k3", "v1", 0, lambda: {"a": 2})
        self.assertFalse(cached)


class TestSandboxIntegrity(unittest.TestCase):
    def test_the_cache_never_escapes_the_temporary_sandbox(self):
        """A location variable left unset means 'use the default', and the
        default is the developer's real cache."""
        self.assertEqual(str(net.cache_dir()), _sandbox.SANDBOX)
        self.assertTrue(str(net.cache_dir()).startswith(tempfile.gettempdir()))

    def test_the_real_cache_directory_is_untouched(self):
        from pathlib import Path
        self.assertNotEqual(net.cache_dir(), Path.home() / ".cache" / "citycost")


class TestFmtAge(unittest.TestCase):
    def test_units(self):
        self.assertEqual(net.fmt_age(45), "45s")
        self.assertEqual(net.fmt_age(700), "11m")
        self.assertEqual(net.fmt_age(9000), "2.5h")
        self.assertEqual(net.fmt_age(300000), "3.5d")


class RetryAfterIsReadNotGuessed(unittest.TestCase):
    """Numbeo's 429 states when the block lifts. This client measured instead.

    Measured 2026-08-25: `Retry-After: Tue, 1 Sep 2026 08:00:00 +0200` — a
    seven-day, address-level ban — while the docs said "duration unknown, at
    least an hour" and the remedy said "wait and retry". The measurement was
    not wrong, it was unnecessary; the remedy built on it was wrong, because
    "wait" and "you cannot wait" are different instructions.
    """

    def test_an_http_date_becomes_a_deadline_and_a_span(self):
        import datetime
        import email.utils
        when = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=7))
        out = net.retry_after({"Retry-After": email.utils.format_datetime(when)})
        self.assertIn("blocked until", out)
        self.assertIn("days", out)

    def test_delta_seconds_is_the_other_legal_form(self):
        out = net.retry_after({"Retry-After": "7200"})
        self.assertIn("hours", out)

    def test_a_past_deadline_is_not_rendered_as_negative_time(self):
        out = net.retry_after({"Retry-After": "Tue, 1 Sep 2020 08:00:00 +0200"})
        self.assertIn("now past", out)

    def test_an_unparseable_value_is_repeated_verbatim_not_dropped(self):
        """Better to hand the reader the server's own string than to swallow
        a header we failed to parse."""
        self.assertIn("soon-ish", net.retry_after({"Retry-After": "soon-ish"}))

    def test_no_header_is_empty_not_a_guess(self):
        self.assertEqual(net.retry_after({}), "")
        self.assertEqual(net.retry_after(None), "")

    def test_a_long_block_changes_the_remedy(self):
        """A week-long address ban must not be answered with 'lower
        concurrency and retry' — that is a right answer to a different
        question, and it is what sent this session probing for an hour."""
        import datetime
        import email.utils
        import urllib.error
        when = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=7))
        hdrs = {"Retry-After": email.utils.format_datetime(when)}

        def blocked(*a, **k):
            raise urllib.error.HTTPError("http://t/", 429, "no", hdrs, None)

        with mock.patch.object(net.urllib.request, "urlopen", blocked):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_get("http://t/")
        self.assertIn("days", ctx.exception.message)
        self.assertIn("another network", ctx.exception.remedy)
        self.assertNotIn("lower concurrency", ctx.exception.remedy)

    def test_a_short_block_keeps_the_slow_down_remedy(self):
        import urllib.error
        hdrs = {"Retry-After": "120"}

        def blocked(*a, **k):
            raise urllib.error.HTTPError("http://t/", 429, "no", hdrs, None)

        with mock.patch.object(net.urllib.request, "urlopen", blocked):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_get("http://t/")
        self.assertIn("lower concurrency", ctx.exception.remedy)



class TestHttpErrorIsClosed(unittest.TestCase):
    """An `HTTPError` wraps the response's file object. Raising past it without
    `close()` leaves that handle to the garbage collector — a socket held until
    GC in production, and on Python 3.13+ a `ResourceWarning: Implicitly
    cleaning up <HTTPError 429>` at the moment it is collected. Measured
    2026-09-02: the suite printed three of those per run, and every one came
    from the two `except HTTPError` clauses in `net`."""

    @staticmethod
    def _refusing(code, body=b"<html>refused</html>"):
        import io
        import urllib.error
        fp = io.BytesIO(body)
        err = urllib.error.HTTPError("http://t/", code, "no", {}, fp)

        def boom(*a, **k):
            raise err
        return boom, fp

    def test_a_refused_get_closes_the_response(self):
        boom, fp = self._refusing(429)
        with mock.patch.object(net.urllib.request, "urlopen", boom):
            with self.assertRaises(SourceUnavailable):
                net.http_get("http://t/")
        self.assertTrue(fp.closed)

    def test_a_refused_post_reads_the_body_and_then_closes_the_response(self):
        boom, fp = self._refusing(400, b'{"error":"bad shape"}')
        with mock.patch.object(net.urllib.request, "urlopen", boom):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_post_json("http://t/", {"a": 1})
        self.assertTrue(fp.closed)
        # Closed AFTER the detail was read, not instead of it.
        self.assertIn("bad shape", ctx.exception.message)


class TestPaceOverride(unittest.TestCase):
    """`CITYCOST_MIN_INTERVAL` raises the per-host gap for one process — the
    knob a 176-request harvest wants and nothing else needs. It can only RAISE:
    the built-in floor is what a measured ban taught, so a value below it has
    no effect, and `pace_source` says so rather than silently ignoring it — an
    accepted-and-ignored parameter is the trap this repository keeps finding."""

    def test_unset_means_the_built_in_floor(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(net.MIN_INTERVAL_ENV, None)
            self.assertEqual(net.min_interval("www.numbeo.com"), 1.1)
            self.assertEqual(net.min_interval("nomads.com"), 1.5)
            self.assertEqual(net.min_interval("other.example"),
                             net.DEFAULT_INTERVAL)
            self.assertEqual(net.pace_source("www.numbeo.com"),
                             "net.MIN_INTERVAL")

    def test_a_larger_value_raises_every_host(self):
        with mock.patch.dict(os.environ, {net.MIN_INTERVAL_ENV: "2.5"}):
            self.assertEqual(net.min_interval("www.numbeo.com"), 2.5)
            self.assertEqual(net.min_interval("nomads.com"), 2.5)
            self.assertIn("CITYCOST_MIN_INTERVAL=2.5",
                          net.pace_source("www.numbeo.com"))

    def test_the_throttle_actually_sleeps_the_raised_gap(self):
        slept = []
        with mock.patch.dict(os.environ, {net.MIN_INTERVAL_ENV: "3"}), \
             mock.patch.object(net.time, "sleep", slept.append):
            net._last_hit.clear()
            net._throttle("https://www.numbeo.com/a")
            net._throttle("https://www.numbeo.com/b")
        self.assertTrue(slept and 2.9 < slept[0] <= 3.0, slept)

    def test_a_smaller_value_cannot_lower_the_floor_and_says_so(self):
        with mock.patch.dict(os.environ, {net.MIN_INTERVAL_ENV: "0.2"}):
            self.assertEqual(net.min_interval("www.numbeo.com"), 1.1)
            self.assertIn("no effect", net.pace_source("www.numbeo.com"))

    def test_a_value_that_is_not_a_gap_is_refused_naming_the_variable(self):
        from citycost.errors import CitycostError
        for bad in ("fast", "nan", "-1", "inf"):
            with self.subTest(value=bad), \
                 mock.patch.dict(os.environ, {net.MIN_INTERVAL_ENV: bad}):
                with self.assertRaises(CitycostError) as ctx:
                    net.min_interval("www.numbeo.com")
                self.assertIn("CITYCOST_MIN_INTERVAL", str(ctx.exception))

    def test_an_empty_value_is_unset_not_an_error(self):
        with mock.patch.dict(os.environ, {net.MIN_INTERVAL_ENV: ""}):
            self.assertEqual(net.min_interval("www.numbeo.com"), 1.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
