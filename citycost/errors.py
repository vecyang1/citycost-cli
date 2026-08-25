"""Failure kinds that have *different remedies*, so they must not share a message.

The whole point of this module is that "no data" and "wrong name" and "the site
changed" send a reader to three different places. Collapsing them into one
string is how a user spends an afternoon on the wrong problem.
"""

from __future__ import annotations


class CitycostError(Exception):
    """Base. Carries a remedy the caller can act on, not just a diagnosis."""

    def __init__(self, message: str, remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def __str__(self) -> str:
        return f"{self.message}\n  -> {self.remedy}" if self.remedy else self.message


class UnknownCity(CitycostError):
    """Numbeo answered 'Cannot find city id' — the slug names nothing."""


class ThinData(CitycostError):
    """The city page exists; too few people have priced it."""


class LayoutChanged(CitycostError):
    """The page loaded but the structure this client keys on was absent.

    Distinct from the two above because the fix is in *this* repo, not in the
    caller's spelling.
    """


class SourceUnavailable(CitycostError):
    """Network, HTTP status, or an upstream rate limit.

    Carries the HTTP `status` when there was one, so callers decide on a number
    rather than by searching this class's own message for "429". A message is
    prose that gets reworded; a status is a fact. Keying the fallback on the
    prose is how a reworded sentence silently turns a working escape hatch off.
    """

    def __init__(self, message: str, remedy: str = "",
                 status: int | None = None,
                 exit_code: int | None = None) -> None:
        super().__init__(message, remedy)
        #: HTTP status, when a server answered.
        self.status = status
        #: Process exit code, when an *external fetcher* failed. Deliberately a
        #: separate field: overloading `status` would make 403-the-HTTP-status
        #: and 4-the-exit-code the same kind of thing, and the first reader to
        #: compare one against the other's vocabulary gets a plausible answer.
        self.exit_code = exit_code
