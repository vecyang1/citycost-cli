"""Failure kinds that have *different remedies*, so they must not share a message.

The whole point of this module is that "no data" and "wrong name" and "the site
changed" send a reader to three different places. Collapsing them into one
string is how a user spends an afternoon on the wrong problem.
"""


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
    """Network, HTTP status, or an upstream rate limit."""
