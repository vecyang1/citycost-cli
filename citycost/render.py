"""Output. Four shapes, one rule: an unknown must never render as a number.

`None` becomes ``N/A`` in text and stays ``null`` in JSON and empty in CSV. It
is never ``0``, never ``""`` in a numeric column, and never quietly omitted —
a column that disappears when the data is missing turns "we could not read it"
into "there was nothing to read".
"""

from __future__ import annotations

import csv
import io
import json
import sys

GREEN, DIM, BOLD, RESET = "\033[92m", "\033[2m", "\033[1m", "\033[0m"

FLAGS = {
    "China": "🇨🇳", "Vietnam": "🇻🇳", "Japan": "🇯🇵", "Thailand": "🇹🇭",
    "United States": "🇺🇸", "Australia": "🇦🇺", "Taiwan": "🇹🇼",
    "South Korea": "🇰🇷", "Malaysia": "🇲🇾", "Indonesia": "🇮🇩",
    "Philippines": "🇵🇭", "Singapore": "🇸🇬", "Portugal": "🇵🇹",
    "Spain": "🇪🇸", "Mexico": "🇲🇽", "Georgia": "🇬🇪", "Turkey": "🇹🇷",
    "India": "🇮🇳", "Germany": "🇩🇪", "France": "🇫🇷", "Italy": "🇮🇹",
    "United Kingdom": "🇬🇧", "Netherlands": "🇳🇱", "Switzerland": "🇨🇭",
    "Canada": "🇨🇦", "Brazil": "🇧🇷", "Colombia": "🇨🇴", "Argentina": "🇦🇷",
    "Poland": "🇵🇱", "Czech Republic": "🇨🇿", "Hungary": "🇭🇺",
    "Romania": "🇷🇴", "Bulgaria": "🇧🇬", "Greece": "🇬🇷", "Nepal": "🇳🇵",
    "Cambodia": "🇰🇭", "Sri Lanka": "🇱🇰", "Morocco": "🇲🇦", "Egypt": "🇪🇬",
    "United Arab Emirates": "🇦🇪", "Serbia": "🇷🇸", "Croatia": "🇭🇷",
    "Estonia": "🇪🇪", "Albania": "🇦🇱", "Uruguay": "🇺🇾", "Peru": "🇵🇪",
}


def fmt_age(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def flag(country: str) -> str:
    return FLAGS.get((country or "").strip(), "")


def money(v, *, decimals_below: float = 20.0) -> str:
    """Small amounts keep a decimal, large ones lose it — $2.3 and $2.9 are a
    real difference, $1,251 and $1,252 are not."""
    if v is None:
        return "N/A"
    v = float(v)
    return f"${v:,.1f}" if abs(v) < decimals_below else f"${v:,.0f}"


def number(v, digits: int = 1) -> str:
    return "N/A" if v is None else f"{float(v):,.{digits}f}"


def signed(v, digits: int = 1) -> str:
    """A delta with its sign always shown, and `N/A` for an absent one.

    The sign is the whole message here and a bare `2.1` reads as a rise whether
    it was one or not. Absent stays absent: a movement that could not be
    computed is not a movement of zero.
    """
    if v is None:
        return "N/A"
    return f"{v:+.{digits}f}"


def percent(v) -> str:
    return "N/A" if v is None else f"{float(v):.0f}%"


def percent_1(v) -> str:
    return "N/A" if v is None else f"{float(v):.2f}%"


def emit_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def emit_csv(rows: list[dict], columns: list[str] | None = None) -> None:
    """Every declared column is written even when every value is empty.

    A column that vanishes because nothing filled it makes a partial read look
    like a complete one with fewer fields.
    """
    if not rows:
        return
    cols = columns or list(rows[0].keys())
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    sys.stdout.write(buf.getvalue())


def markdown_table(headers: list[str], rows: list[list[str]],
                   align_right_from: int = 1) -> str:
    sep = ["---" if i < align_right_from else "---:"
           for i in range(len(headers))]
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(sep) + "|"]
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _visible_len(s: str) -> int:
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def text_table(headers: list[str], rows: list[list[str]], *,
               color: bool = True) -> str:
    widths = [_visible_len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], _visible_len(cell))

    def pad(cell: str, w: int, right: bool) -> str:
        gap = " " * max(0, w - _visible_len(cell))
        return gap + cell if right else cell + gap

    head = "  ".join(pad(h, widths[i], i > 0) for i, h in enumerate(headers))
    if color:
        head = f"{BOLD}{head}{RESET}"
    lines = [head, "-" * (sum(widths) + 2 * (len(widths) - 1))]
    for r in rows:
        lines.append("  ".join(
            pad(c, widths[i], i > 0) for i, c in enumerate(r) if i < len(widths)))
    return "\n".join(lines)


def mark_best(rendered: str, value, best, *, color: bool = True) -> str:
    if value is None or best is None or value != best:
        return rendered
    return f"{GREEN}🟢{rendered}{RESET}" if color else f"🟢{rendered}"


def age_note(age, source: str) -> None:
    """Print how old a figure is. Absent age prints nothing rather than zero:
    "age 0s" for an unknown age is the confident wrong answer, and it is the
    one every reader trusts."""
    if age is not None:
        note(f"  data age {fmt_age(int(age))} · {source}")


def note(msg: str) -> None:
    """Provenance and warnings go to stderr, so `--json` and `--csv` stay
    pipe-clean while a human still sees why a number is missing."""
    print(msg, file=sys.stderr)
