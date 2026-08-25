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
