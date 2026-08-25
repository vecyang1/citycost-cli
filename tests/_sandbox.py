"""One owner for test isolation. Every test module imports this FIRST.

Per-module `mkdtemp` looks careful and is not: the module under test resolves
its cache location once, at *its* import, so the first test file to load wins
and every other file spends the run asserting against a directory nothing will
write to. Keying this on the import cache makes the deduplication structural
rather than something each new file has to remember.
"""

import os
import tempfile

SANDBOX = tempfile.mkdtemp(prefix="citycost-tests-")
os.environ["CITYCOST_CACHE_DIR"] = SANDBOX

# Locations and credentials need opposite treatment. A credential wants to be
# *absent* so a test cannot accidentally authenticate; a location wants to be
# *set to somewhere disposable*, because unset means "use the default", and the
# default is the developer's real cache.
for _var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_var, None)

# The transport fallback is both at once, and the danger runs in one direction
# only. `CITYCOST_CONFIG` is a *location*: unset means "use the default", and
# the default is the developer's real fetch.conf, which names a real proxy that
# costs real money — so it is pointed at a path inside the sandbox that does
# not exist, which is the "nothing configured" state every test should start
# from. The three command variables are *switches*: an exported one would send
# a unit test through a live proxy, so they are removed outright.
#
# Added when the fallback landed, in the same change, not after it. The window
# between a new resolution path and its isolation is precisely when a suite
# runs against live data, and it leaves no failing test behind to say so.
os.environ["CITYCOST_CONFIG"] = os.path.join(SANDBOX, "fetch.conf")
for _var in ("CITYCOST_FETCH_CMD", "CITYCOST_POST_CMD", "CITYCOST_FETCH_MODE"):
    os.environ.pop(_var, None)
