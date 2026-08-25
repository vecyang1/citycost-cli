"""The transport policy: direct first, the fetcher only when refused.

Three properties here are about things that must *not* happen, and none of them
can be checked by looking at a return value:

* a 404 must not reach the fetcher (it would buy the same 404),
* `mode=never` must not reach the fetcher,
* `mode=always` must not attempt the direct read.

So they are asserted with traps — a script that leaves a marker file, and a
`side_effect` that raises — rather than by inspecting what came back. A stub
that politely returns a plausible value cannot tell you whether it was called.
"""

import os
import sys
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401
from citycost import fallback, net
from citycost.errors import SourceUnavailable

MARKER = os.path.join(_sandbox.SANDBOX, "fetcher-ran")

#: exit 0, echo a body, and record anything that arrived on stdin.
OK = os.path.join(_sandbox.SANDBOX, "ok_fetcher.py")
#: exit 0 but leaves a marker — used where the fetcher must NOT run.
TRAP = os.path.join(_sandbox.SANDBOX, "trap_fetcher.py")
#: exit 1 while still printing a plausible body to stdout.
FAIL = os.path.join(_sandbox.SANDBOX, "fail_fetcher.py")
#: exit 0 with nothing on stdout.
EMPTY = os.path.join(_sandbox.SANDBOX, "empty_fetcher.py")

with open(OK, "w") as fh:
    fh.write(
        "import sys, os\n"
        # Unconditional on purpose: an `isatty()` guard would make the test
        # above pass by skipping the very read whose blocking is the bug.
        "body = sys.stdin.buffer.read()\n"
        "open(os.environ['STDIN_SINK'], 'wb').write(body)\n"
        "sys.stdout.write('FETCHED ' + sys.argv[1])\n")
with open(TRAP, "w") as fh:
    fh.write("import sys, os\n"
             "open(os.environ['TRAP_MARKER'], 'w').write('ran')\n"
             "sys.stdout.write('should never be trusted')\n")
with open(FAIL, "w") as fh:
    fh.write("import sys\n"
             "sys.stdout.write('<html>429 Too Many Requests</html>')\n"
             "sys.stderr.write('proxy said no')\n"
             "sys.exit(1)\n")
with open(EMPTY, "w") as fh:
    fh.write("import sys\nsys.exit(0)\n")

STDIN_SINK = os.path.join(_sandbox.SANDBOX, "stdin-sink")
os.environ["STDIN_SINK"] = STDIN_SINK
os.environ["TRAP_MARKER"] = MARKER

CMD_OK = f"{sys.executable} {OK} {{url}}"
CMD_TRAP = f"{sys.executable} {TRAP} {{url}}"
CMD_FAIL = f"{sys.executable} {FAIL} {{url}}"
CMD_EMPTY = f"{sys.executable} {EMPTY} {{url}}"

URL = "https://www.numbeo.com/cost-of-living/in/Da-Nang"


def blocked(status=429):
    return SourceUnavailable(f"{URL} rate-limited", "wait", status=status)


class TransportBase(unittest.TestCase):
    def setUp(self):
        fallback.reset()
        for var in (fallback.GET_CMD_ENV, fallback.POST_CMD_ENV,
                    fallback.MODE_ENV):
            os.environ.pop(var, None)
        for path in (MARKER, STDIN_SINK):
            if os.path.exists(path):
                os.remove(path)
        net._last_hit.clear()


class ConfigResolution(TransportBase):

    def test_matched_quotes_only(self):
        # `.strip('"').strip("'")` would eat the closing quote of a header and
        # leave shlex an unbalanced string.
        v = "cmd {url} --header 'Content-Type: application/json'"
        self.assertEqual(fallback._unquote(v), v)
        self.assertEqual(fallback._unquote('"a b"'), "a b")
        self.assertEqual(fallback._unquote("'a b'"), "a b")

    def test_missing_config_is_not_configured(self):
        cfg = fallback.settings()
        self.assertEqual(cfg.config_status, "missing")
        self.assertFalse(cfg.available)
        self.assertEqual(cfg.mode, "auto")

    def test_config_file_supplies_the_command(self):
        path = os.environ["CITYCOST_CONFIG"]
        with open(path, "w") as fh:
            fh.write("# comment\n\n"
                     f"{fallback.GET_CMD_ENV}={CMD_OK}\n"
                     f"{fallback.MODE_ENV}=always\n")
        try:
            cfg = fallback.settings()
            self.assertEqual(cfg.config_status, "ok")
            self.assertEqual(cfg.get_cmd, CMD_OK)
            self.assertEqual(cfg.get_source, "config")
            self.assertEqual(cfg.mode, "always")
        finally:
            os.remove(path)

    def test_a_file_that_declares_none_of_our_keys_is_foreign(self):
        """A config path is not a claim of ownership. Reporting 'not
        configured' without naming the file makes 'you have none' and 'yours
        lost' the same sentence."""
        path = os.environ["CITYCOST_CONFIG"]
        with open(path, "w") as fh:
            fh.write("OPENAI_API_KEY=someone-elses\nEDITOR=vim\n")
        try:
            cfg = fallback.settings()
            self.assertEqual(cfg.config_status, "foreign")
            self.assertFalse(cfg.available)
        finally:
            os.remove(path)

    def test_env_beats_config(self):
        path = os.environ["CITYCOST_CONFIG"]
        with open(path, "w") as fh:
            fh.write(f"{fallback.GET_CMD_ENV}=from-file {{url}}\n")
        os.environ[fallback.GET_CMD_ENV] = "from-env {url}"
        try:
            cfg = fallback.settings()
            self.assertEqual(cfg.get_cmd, "from-env {url}")
            self.assertEqual(cfg.get_source, "env")
        finally:
            os.remove(path)

    def test_an_unknown_mode_is_refused_not_silently_defaulted(self):
        os.environ[fallback.MODE_ENV] = "yes-please"
        with self.assertRaises(SourceUnavailable) as ctx:
            fallback.settings()
        self.assertIn("auto, always, or never", ctx.exception.remedy)

    def test_blocked_statuses(self):
        for code in (403, 429, 503):
            self.assertTrue(fallback.is_blocked(blocked(code)), code)
        for code in (404, 500, None):
            self.assertFalse(fallback.is_blocked(blocked(code)), code)


class AutoFallback(TransportBase):

    def test_429_reroutes_and_returns_the_fetched_body(self):
        os.environ[fallback.GET_CMD_ENV] = CMD_OK
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            body = net.http_get(URL)
        self.assertEqual(body, f"FETCHED {URL}")
        self.assertEqual(fallback.events(),
                         [{"url": URL, "blocked_status": 429, "method": "GET"}])

    def test_403_also_reroutes(self):
        os.environ[fallback.GET_CMD_ENV] = CMD_OK
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(403)):
            self.assertEqual(net.http_get(URL), f"FETCHED {URL}")

    def test_404_does_not_reach_the_fetcher(self):
        """A wrong slug through a residential proxy is the same wrong slug,
        bought. The trap script proves it was never launched."""
        os.environ[fallback.GET_CMD_ENV] = CMD_TRAP
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(404)):
            with self.assertRaises(SourceUnavailable):
                net.http_get(URL)
        self.assertFalse(os.path.exists(MARKER))
        self.assertEqual(fallback.events(), [])

    def test_503_reroutes_even_after_the_retries_are_exhausted(self):
        """A 5xx is retried first, because it is often a blip. The error that
        survives all four attempts must still carry its status: dropping it
        there closed the escape hatch for the one failure most likely to need
        it, while every 429 test stayed green."""
        import urllib.error
        os.environ[fallback.GET_CMD_ENV] = CMD_OK

        def busy(*a, **k):
            raise urllib.error.HTTPError(URL, 503, "busy", {}, None)

        with mock.patch.object(net.urllib.request, "urlopen", busy):
            with mock.patch.object(net.time, "sleep"):
                body = net.http_get(URL)
        self.assertEqual(body, f"FETCHED {URL}")
        self.assertEqual(fallback.events()[0]["blocked_status"], 503)

    def test_a_get_fetcher_reading_stdin_gets_eof_not_the_cli_terminal(self):
        """`input=None` inherits the caller's stdin. A fetcher that reads it
        then hangs until the subprocess timeout — which presents as a slow
        proxy, not as a wiring bug, and is invisible when stdin happens to be
        a tty. The OK script reads stdin unconditionally; if it is not given
        EOF this test does not fail, it *hangs*, which is the point."""
        os.environ[fallback.GET_CMD_ENV] = CMD_OK
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            body = net.http_get(URL)
        self.assertEqual(body, f"FETCHED {URL}")
        with open(STDIN_SINK, "rb") as fh:
            self.assertEqual(fh.read(), b"")

    def test_mode_never_stays_direct(self):
        os.environ[fallback.GET_CMD_ENV] = CMD_TRAP
        os.environ[fallback.MODE_ENV] = "never"
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            with self.assertRaises(SourceUnavailable):
                net.http_get(URL)
        self.assertFalse(os.path.exists(MARKER))

    def test_mode_always_skips_the_direct_attempt(self):
        os.environ[fallback.GET_CMD_ENV] = CMD_OK
        os.environ[fallback.MODE_ENV] = "always"
        boom = AssertionError("the direct path must not be attempted")
        with mock.patch.object(net, "_http_get_once", side_effect=boom):
            self.assertEqual(net.http_get(URL), f"FETCHED {URL}")

    def test_mode_always_without_a_command_says_so(self):
        os.environ[fallback.MODE_ENV] = "always"
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_get(URL)
        self.assertIn("no GET fetch command", ctx.exception.message)

    def test_blocked_with_no_fetcher_says_how_to_get_one(self):
        """The remedy that used to read 'wait and retry' was measured still
        wrong an hour after the load that caused the block had stopped."""
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_get(URL)
        self.assertIn("external GET fetcher", ctx.exception.remedy)
        self.assertIn(fallback.GET_CMD_ENV, ctx.exception.remedy)
        self.assertIn("fetch.conf", ctx.exception.remedy)
        self.assertNotIn("wait and retry", ctx.exception.remedy)


class FetcherContract(TransportBase):

    def test_a_nonzero_exit_is_never_trusted_even_with_a_body(self):
        """A 429 page is 20 KB of plausible HTML. Exit code decides, not
        whether bytes arrived."""
        os.environ[fallback.GET_CMD_ENV] = CMD_FAIL
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_get(URL)
        self.assertIn("exited 1", ctx.exception.message)
        self.assertIn("proxy said no", ctx.exception.message)
        self.assertNotIn("Too Many Requests", ctx.exception.message)

    def test_an_empty_body_with_exit_zero_is_refused(self):
        os.environ[fallback.GET_CMD_ENV] = CMD_EMPTY
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_get(URL)
        self.assertIn("empty body", ctx.exception.message)

    def test_a_template_without_the_placeholder_is_refused(self):
        os.environ[fallback.GET_CMD_ENV] = f"{sys.executable} {OK}"
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_get(URL)
        self.assertIn("{url}", ctx.exception.message)

    def test_a_missing_command_names_the_binary(self):
        os.environ[fallback.GET_CMD_ENV] = "citycost-no-such-fetcher {url}"
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_get(URL)
        self.assertIn("citycost-no-such-fetcher", ctx.exception.message)

    def test_the_fetcher_is_attempted_once_not_retried(self):
        os.environ[fallback.GET_CMD_ENV] = CMD_FAIL
        with mock.patch.object(net, "_http_get_once", side_effect=blocked(429)):
            with mock.patch.object(fallback, "run",
                                   side_effect=SourceUnavailable("x")) as run:
                with self.assertRaises(SourceUnavailable):
                    net.http_get(URL)
        self.assertEqual(run.call_count, 1,
                         "each attempt costs bandwidth; two backoffs "
                         "multiplied is a bill, not resilience")


class PostFallback(TransportBase):

    def test_post_reroutes_and_delivers_the_body_on_stdin(self):
        """A GET-only escape hatch is half a fallback, and the half that works
        hides that the other half never runs."""
        os.environ[fallback.POST_CMD_ENV] = CMD_OK
        payload = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
        fake = mock.Mock(side_effect=blocked(429))
        with mock.patch.object(net, "_http_post_json_once", fake):
            with mock.patch.object(fallback, "run",
                                   return_value='{"result": "ok"}') as run:
                out = net.http_post_json("https://nomads.com/mcp", payload)
        self.assertEqual(out, {"result": "ok"})
        self.assertEqual(run.call_args.kwargs["method"], "POST")
        self.assertIn(b'"tools/list"', run.call_args.kwargs["data"])

    def test_post_body_really_reaches_the_subprocess_stdin(self):
        """The mock above proves the argument is passed; this proves the
        subprocess receives it. A fake that accepts an argument and drops it
        reads as covered."""
        os.environ[fallback.POST_CMD_ENV] = CMD_OK
        payload = {"hello": "world"}
        with mock.patch.object(net, "_http_post_json_once",
                               side_effect=blocked(429)):
            with self.assertRaises(SourceUnavailable):
                # the OK script echoes a non-JSON body, which must be refused
                net.http_post_json("https://nomads.com/mcp", payload)
        with open(STDIN_SINK, "rb") as fh:
            self.assertEqual(fh.read(), b'{"hello": "world"}')

    def test_a_get_only_configuration_does_not_silently_serve_post(self):
        os.environ[fallback.GET_CMD_ENV] = CMD_OK
        with mock.patch.object(net, "_http_post_json_once",
                               side_effect=blocked(429)):
            with self.assertRaises(SourceUnavailable) as ctx:
                net.http_post_json("https://nomads.com/mcp", {})
        # The remedy must name the POST variable, not the GET one its owner
        # has already set and is looking straight at.
        self.assertIn(fallback.POST_CMD_ENV, ctx.exception.remedy)
        self.assertNotIn(fallback.GET_CMD_ENV, ctx.exception.remedy)
        self.assertIn("stdin", ctx.exception.remedy)


class RetryPolicy(TransportBase):

    def test_a_4xx_is_an_answer_and_is_not_retried(self):
        fake = mock.Mock(side_effect=blocked(404))
        with mock.patch.object(net, "_http_get_once", fake):
            with self.assertRaises(SourceUnavailable):
                net.http_get(URL)
        self.assertEqual(fake.call_count, 1)

    def test_a_transient_failure_is_retried(self):
        fake = mock.Mock(side_effect=[
            SourceUnavailable("timeout", "", None),
            SourceUnavailable("timeout", "", None),
            "recovered"])
        with mock.patch.object(net, "_http_get_once", fake):
            with mock.patch.object(net.time, "sleep"):
                self.assertEqual(net.http_get(URL), "recovered")
        self.assertEqual(fake.call_count, 3)


class SandboxIsolation(unittest.TestCase):
    """The suite must not be able to reach the developer's real fetcher."""

    def test_config_points_inside_the_sandbox(self):
        self.assertTrue(
            str(fallback.config_path()).startswith(_sandbox.SANDBOX),
            f"CITYCOST_CONFIG escaped the sandbox: {fallback.config_path()}")

    def test_the_real_config_is_never_consulted(self):
        import pathlib
        real = pathlib.Path.home() / ".config" / "citycost" / "fetch.conf"
        self.assertNotEqual(fallback.config_path(), real)


if __name__ == "__main__":
    unittest.main()
