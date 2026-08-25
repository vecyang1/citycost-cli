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


if __name__ == "__main__":
    unittest.main(verbosity=2)
