"""`python -m citycost` — the invocation that works without a working PATH.

The console script is the documented entry point, and it is the one that
breaks first: `pipx` and `pip --user` write into a bin directory that is
frequently not on `PATH`, so a correct install answers `command not found`.
The README already tells that reader to run `pipx ensurepath` and open a new
shell, which is right and requires them to leave and come back.

This module is the same program reachable from the interpreter they already
have. It is also what an embedding caller should use — `subprocess` with
`sys.executable -m citycost` cannot pick the wrong copy the way a name on
`PATH` can, which matters here: a pipx-installed 1.1.1 and a source tree at
1.2.0 both answer to `citycost`, and on 2026-08-25 that made a verified fix
look like it had not worked.

Deliberately three lines with no logic of its own. Two entry points that each
decide something is two programs.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
