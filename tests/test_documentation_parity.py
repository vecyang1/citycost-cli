"""Every command this project PRINTS must be a command this project PARSES.

Error text and documentation are the one interface nothing asserts on. The
handler is correct, the exit code is correct, the suite is green, and the
sentence is wrong — and a remedy naming a command that does not parse hands the
reader something that exits immediately, at the worst moment they will have
with the software.

Two subjects, and the denominator of each is derived rather than retyped: every
command string printed by README.md, the CLI's own epilog, and any string
literal in `citycost/`; and every subcommand the parser registers. Both counts
are asserted to be non-trivial, because a selector that silently stops matching
would otherwise pass by ranging over nothing.

Measured while this was being written: `citycost/discover.py` printed the
remedy "check the argument names against `citycost doctor --tools`", and
`citycost doctor --tools` is `error: unrecognized arguments`. It had shipped.
"""

import argparse
import pathlib
import re
import shlex
import unittest
from unittest import mock

from . import _sandbox  # noqa: F401
from citycost import cli, parser as cli_parser

REPO = pathlib.Path(__file__).resolve().parent.parent
README = REPO / "README.md"

#: A placeholder a human is meant to substitute. `<slug>`, `{url}`, `…`.
PLACEHOLDER = re.compile(r"^(<.+>|\{.+\}|\.\.\.|…)$")

#: Commands that legitimately do not parse here because they are not this CLI.
NOT_OURS = ("citycost-cli",)


class _DoesNotParse(Exception):
    """Raised in place of argparse's `sys.exit`, so a refusal is catchable."""


def _subcommands() -> list[str]:
    p = cli.build_parser()
    subs = [a for a in p._actions
            if isinstance(a, argparse._SubParsersAction)]
    assert len(subs) == 1
    return sorted(subs[0].choices)


def _joined(source: str) -> str:
    """Glue Python's adjacent string literals back together before extracting.

    A remedy long enough to be worth writing is long enough to be wrapped, and
    a line-based extractor then reads `citycost rank --snapshot` as the whole
    command and grades it as one — failing on documentation that is correct,
    while a genuinely truncated remedy elsewhere would pass. The extractor is a
    lossy read of the thing being tested and the loss is invisible from the
    assertion's side, so it is repaired here rather than tolerated.
    """
    return re.sub(r'"\s*\n\s*f?"', "", source)


def _commands_in(text: str) -> list[str]:
    """Every `citycost …` invocation in a blob of text.

    Deliberately greedy to the end of the line rather than matched by a
    character class. A class like `[a-z0-9 -]*` looks equivalent and stops dead
    at the first capital letter — which in a real example is the argument, so
    `citycost trend "Vancouver, Canada"` would be captured as `trend ` and
    graded as a bare subcommand with none of its flags. That truncation is
    invisible from the assertion's side and moves the count by nothing.
    """
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("$ ").strip()
        for m in re.finditer(r"`(citycost [^`]+)`", line):
            out.append(m.group(1))
        if line.startswith("citycost "):
            out.append(line.split("#")[0].strip())
        # `python -m citycost …` is the same surface under its other name.
        if line.startswith("python -m citycost") or \
                line.startswith("python3 -m citycost"):
            out.append("citycost " + line.split("citycost", 1)[1].strip())
    # Prose starts with the program name too — "citycost runs the command,
    # takes stdout as the body" is a sentence, not an invocation. The
    # discriminator is the SECOND token: a real command names a registered
    # subcommand or starts with a flag. Keyed on the parser's own registry so
    # a new subcommand is picked up without editing this file.
    # Shell plumbing is not this CLI's argument list. `| jq`, `> out.csv` and
    # `&& echo` belong to the shell, and a gate that graded them would fail on
    # documentation that is not only correct but idiomatic.
    # Whitespace on BOTH sides is load-bearing: a bare `[|>]` also splits
    # `--index <vertical>` at its closing bracket, truncating a correct command
    # into an invalid one and failing the gate on documentation it was supposed
    # to pass. A character class that looks equivalent and is not is exactly
    # what this file exists to catch, so it is asserted below.
    out = [re.split(r"\s(?:\||>>?|&&)\s", c)[0].strip() for c in out]
    known = set(_subcommands())
    keep = []
    for c in out:
        if any(bad in c for bad in NOT_OURS):
            continue
        rest = c.split(None, 1)[1] if " " in c else ""
        head = rest.split(None, 1)[0].strip("`.,") if rest else ""
        if head in known or head.startswith("-"):
            keep.append(c)
    return keep


def _choices_for(parser, subcommand: str) -> dict:
    """`{option string: first valid choice}` for the named subcommand.

    A placeholder standing in for a `choices=` value cannot be a generic token:
    argparse rejects it, and the gate would then fail on documentation that is
    perfectly correct. Substituting a REAL choice keeps the gate testing what
    it claims to test — the shape of the command — rather than punishing the
    author for writing `<index>`.
    """
    subs = [a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)]
    sp = subs[0].choices.get(subcommand) if subs else None
    out = {}
    for action in getattr(sp, "_actions", []):
        if action.choices:
            for opt in action.option_strings:
                out[opt] = sorted(str(c) for c in action.choices)[0]
    return out


def _argv(parser, command: str) -> list[str]:
    """Tokenise, drop the program name, and neutralise placeholders."""
    parts = shlex.split(command)[1:]
    if not parts:
        return parts
    choices = _choices_for(parser, parts[0])
    out = []
    for i, tok in enumerate(parts):
        if PLACEHOLDER.match(tok):
            prev = parts[i - 1] if i else ""
            out.append(choices.get(prev, "PLACEHOLDER"))
        else:
            out.append(tok)
    return out


class TestEveryPrintedCommandParses(unittest.TestCase):
    def _grade(self, commands, where):
        """Collect every failure and assert once.

        Deliberately not `subTest`: it *swallows* the AssertionError, which
        means the test that proves this gate can go red would itself pass on a
        broken gate. A checker whose own failure path is untestable is the
        thing this file exists to catch, one level up.
        """
        self.assertGreater(len(commands), 0, f"no commands found in {where}")
        p = cli.build_parser()
        failures = []
        for command in commands:
            def boom(msg, _c=command):
                raise _DoesNotParse(f"{_c}  ->  {msg}")
            try:
                # Patched on the CLASS, not the instance: a subparser is a
                # different ArgumentParser object and calls its own `error`,
                # so an instance patch lets a bad SUBCOMMAND exit the process
                # instead of failing the test — the gate would then be green
                # for exactly the commands it exists to grade.
                with mock.patch.object(argparse.ArgumentParser, "exit",
                                       side_effect=boom), \
                     mock.patch.object(argparse.ArgumentParser, "error",
                                       side_effect=boom):
                    p.parse_args(_argv(p, command))
            except (_DoesNotParse, argparse.ArgumentError, ValueError) as exc:
                failures.append(f"{where}: {exc}")
        self.assertEqual(failures, [],
                         f"{len(failures)} of {len(commands)} printed "
                         f"commands do not parse:\n" + "\n".join(failures))
        return len(commands)

    def test_readme(self):
        graded = self._grade(_commands_in(README.read_text(encoding="utf-8")),
                             "README.md")
        # Reported, not merely counted: a selector that narrows later shows up
        # as a number that dropped rather than as continued green.
        self.assertGreater(graded, 10, f"only graded {graded} README commands")

    def test_the_epilog_that_help_prints(self):
        self._grade(_commands_in(cli_parser.EPILOG), "EPILOG")

    def test_every_command_named_in_a_source_string(self):
        """The remedies. This is where the rot is, because a remedy is written
        once, read by a user at the worst moment, and asserted on by nothing.
        """
        found = []
        for py in sorted((REPO / "citycost").glob("*.py")):
            found += [(py.name, c) for c in
                      _commands_in(_joined(py.read_text(encoding="utf-8")))]
        self.assertGreater(len(found), 3, found)
        self._grade([c for _, c in found], "citycost/*.py")

    def test_the_extractor_actually_captures_arguments(self):
        """The extractor is a lossy read of the thing being tested, and the
        loss is invisible from the assertion's side. So assert on what it
        captured, not on how many."""
        sample = 'run `citycost trend "Vancouver, Canada" --snapshots 10` now'
        self.assertEqual(_commands_in(sample),
                         ['citycost trend "Vancouver, Canada" --snapshots 10'])

    def test_shell_plumbing_is_stripped_without_eating_placeholders(self):
        """Both directions of the splitter, because the failure is silent in
        one of them: an over-broad operator truncates a valid command and the
        gate then fails on correct documentation, which gets the gate loosened
        rather than the bug fixed."""
        self.assertEqual(
            _commands_in("`citycost rank --csv > out.csv`"),
            ["citycost rank --csv"])
        self.assertEqual(
            _commands_in("`citycost snapshots --index <vertical>`"),
            ["citycost snapshots --index <vertical>"])
        self.assertEqual(
            _commands_in("`citycost rank --json | jq .`"),
            ["citycost rank --json"])

    def test_a_command_that_does_not_parse_is_caught(self):
        """The gate, proved able to go red. A parity test that has only ever
        seen valid input is the same green light as any other check nobody has
        run against failure."""
        with self.assertRaises(AssertionError):
            self._grade(["citycost doctor --tools"], "synthetic")


class TestReadmeNamesEverySubcommand(unittest.TestCase):
    def test_no_subcommand_is_undocumented(self):
        """`snapshots`, `city`, `meetups` and `cache` shipped unnamed in the
        README for three releases. A command nobody can discover is only
        cheaper than one that does not exist because it still costs the reader
        who goes looking."""
        text = README.read_text(encoding="utf-8")
        names = _subcommands()
        self.assertGreater(len(names), 5, names)
        missing = [n for n in names if not re.search(rf"\b{n}\b", text)]
        self.assertEqual(missing, [], f"undocumented subcommands: {missing}")


if __name__ == "__main__":
    unittest.main()
