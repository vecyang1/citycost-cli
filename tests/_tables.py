"""The fixtures three test modules share, so there is one owner for them.

`movers`, `comparable` and `diffing` are graded by three files beside those
three modules, and every one of them needs the same two shapes: a parsed
ranking table as a dict, and the markup Numbeo actually serves. A per-file copy
of `_table()` is three fixtures that agree on the day they are written and
drift apart afterwards — and a fixture that has drifted is the one thing a
green suite cannot tell you about.

`_sandbox` is imported here FIRST, before `citycost`, so importing this module
can never be the route by which the code under test resolves the developer's
real cache directory.
"""

from . import _sandbox  # noqa: F401  (must be first)
from citycost import rankings

COL = "Cost of Living Index"
RENT = "Rent Index"


def _row(place, **metrics):
    rec = rankings.split_place(place)
    rec["rank"] = 0
    rec["metrics"] = dict(metrics)
    return rec


def _table(rows, columns=None, url="u"):
    ranked = []
    for i, r in enumerate(rows, start=1):
        rr = dict(r)
        rr["rank"] = i
        ranked.append(rr)
    cols = columns if columns is not None else (
        list(rows[0]["metrics"]) if rows else [])
    return {"url": url, "columns": list(cols), "rows": ranked}


def _html(rows, columns=(COL, RENT)):
    """A real ranking page, built the way Numbeo builds one.

    Including the EMPTY first `<td>` — the rank cell Numbeo fills with
    DataTables JS. A fixture written from what the response *means* rather than
    from what it *is* tests the parser against its author's mental model.
    """
    head = "".join(f"<th><div>{c}</div></th>" for c in columns)
    body = "".join(
        "<tr><td></td>"
        f'<td class="cityOrCountryInIndicesTable">{place}</td>'
        + "".join(f'<td style="text-align: right">{v}</td>' for v in values)
        + "</tr>"
        for place, values in rows)
    return (f'<table id="t2" class="stripe"><thead><tr>'
            f'<th><div>Rank</div></th><th><div>City</div></th>{head}'
            f"</tr></thead><tbody>{body}</tbody></table>")
