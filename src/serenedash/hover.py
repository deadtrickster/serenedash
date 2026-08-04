"""serenedash.hover — what is under the pointer.

Every explanation here comes out of LEGEND. That is deliberate: the legend already claims to
document every label and number on the screen, so a second table of hover text would be a second
place for the same prose to go stale, and the one that goes stale is always the one nobody opens.
Hovering is just the legend, looked up by where you are pointing instead of by reading it top to
bottom.

Nothing is inferred about a value. The tooltip names the panel and the row, and repeats what the
legend says about that term — it never invents a reading of the number, which is the failure mode
this dashboard has had over and over.
"""
import re
import textwrap

from .fmt import COL_BAR, COL_LABEL, COL_VALUE, SPARK, strip

# What the panel as a whole is, for a pointer that is on a border, a heading, or a row whose label
# the legend does not carry (a memory tag, a thread name, a symbol). One line each; anything longer
# belongs in LEGEND where `l` will show it too.
PANELS = {
    "storage": "database and WAL size, then du of the store's own directories with each one's "
               "share of the on-disk total",
    "memory": "duckdb_memory() by pool against memory_limit, plus the process's own resident and "
              "paged-out memory. Every trace is its own row's bar over time",
    "activity": "sessions from pg_stat_activity, active first, excluding this dashboard's own. The "
                "text is clipped to the row — `a` fetches and shows the whole statement",
    "threads": "the whole process against every core, then the threads carrying it. Each row is a "
               "share of ONE core",
    "profile": "symbols from the newest perf capture, grouped by engine. Shares are of every "
               "sampled cycle in the window",
    "host": "the machine and the process — the context every other number on the screen is read "
            "against",
    "config": "settings with measured consequences, each predicate run against this server",
    "doctor": "every precondition for a full picture, and what each missing one costs you",
    "legend": "what every label and number on the main screen means",
    "graph": "call graph from the newest capture, callers above callees",
}

# Placeholders in a legend term: `wal  N.NNx`, `N% of X on disk`, `spilling +X/T`. They stand for
# the number, so they are not what anyone points at.
_NOISE = re.compile(r"^[+-]?[NXT][%.\w/]*$|^(of|on|the|a|per|and)$")

_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

_SPARKS = set(SPARK) | {"·"}

# The column grid `line()` lays every row on. A pointer's offset within its panel says which field
# it is in without having to re-parse the row, which matters because the tail is free-form.
_BAR_AT = COL_LABEL + COL_VALUE + 2


def _terms():
    """{(section, key): (term, meaning)} and {key: (section, term, meaning)}, built from LEGEND."""
    from .views import LEGEND
    bysec, anywhere = {}, {}
    for section, items in LEGEND:
        for term, meaning in items:
            # `columnar / search idx / spill` documents three labels in one entry, and each of them
            # is a row you can point at.
            for alt in term.split("/"):
                words = [w for w in alt.strip().split() if not _NOISE.match(w)]
                for key in ({" ".join(words)} | set(words)) - {""}:
                    bysec.setdefault((section, key.lower()), (term, meaning))
                    anywhere.setdefault(key.lower(), (section, term, meaning))
    return bysec, anywhere


_BYSEC, _ANY = _terms()


def segment_at(text, col):
    """(start column, text) of the box cell the column falls in.

    In wide mode two panels share a line — `│ left │ │ right │` — so a row's label is not at the
    start of the line, it is at the start of whichever cell was pointed at.
    """
    starts = [i + 1 for i, ch in enumerate(text) if ch == "│"]
    if not starts:
        return 0, text
    lo = max((p for p in starts if p <= col), default=0)
    hi = min((p - 1 for p in starts if p > col), default=len(text))
    return lo, text[lo:hi]


def panel_at(lines, row, col, view="main"):
    """Which panel the pointer is over. The view's own name outside the main frame."""
    if view != "main":
        return view
    for i in range(min(row, len(lines) - 1), -1, -1):
        text = strip(lines[i])
        # Titles sit immediately after the box corner, and a line can carry two of them.
        spans = [(m.start(), m.group(1)) for m in re.finditer(r"┌─(\S+)", text)]
        if not spans:
            continue
        return next((t for s, t in reversed(spans) if s <= col), spans[0][1])
    return None


def _phrases(text, col):
    """Words under the column, longest phrase first: `io wait` before `wait`.

    Two-word labels are most of the vocabulary that needs explaining at all — `io wait`, `in use`,
    `nothing running`, `os threads`, `search idx` — so matching a single word first would answer
    the wrong question every time.
    """
    hits = list(_WORD.finditer(text))
    here = next((i for i, m in enumerate(hits) if m.start() <= col < m.end()), None)
    if here is None:
        return []
    out = []
    for lo, hi in ((here - 1, here + 1), (here, here + 2), (here, here + 1)):
        if lo < 0 or hi > len(hits):
            continue
        span = text[hits[lo].start():hits[hi - 1].end()]
        # Only if they really are adjacent words; a phrase spanning a column gap is two labels that
        # happen to sit next to each other, not a term.
        if " " * 2 not in span:
            out.append(span.lower())
    return out


def describe(lines, row, col, view="main"):
    """(title, body) for the point, or None. Title is `panel · term`."""
    if not (0 <= row < len(lines)):
        return None
    text = strip(lines[row])
    if col >= len(text.rstrip()) and view == "main":
        col = min(col, max(0, len(text) - 1))
    panel = panel_at(lines, row, col, view)
    sec = panel if panel in PANELS else None
    start, seg = segment_at(text, col)
    off = col - start
    ch = text[col] if 0 <= col < len(text) else " "

    # The label is the first field of the cell, so it identifies the row wherever in the row you
    # are pointing — which is the whole point of hovering a bar rather than its label.
    label = " ".join(seg[:COL_LABEL].split()) if len(seg) > 2 else ""

    # A border or a title is the panel, not a row in it. Without this, pointing at the `threads`
    # box heading answered with `os threads` out of the host panel's legend — the title happens to
    # be a word another section documents, and a heading is the one place you have asked about the
    # panel itself.
    if sec and set("┌└─┐┘") & set(seg or text):
        return sec, PANELS[sec]

    lookups = _phrases(seg, off) + ([label.lower()] if label else [])
    for key in lookups:
        hit = _BYSEC.get((sec, key)) if sec else None
        if hit:
            return f"{sec} · {hit[0]}", hit[1]
    # A setting the config panel watches carries its own reason for being watched.
    if sec == "config" and label:
        from .hazards import HAZARDS
        if label in HAZARDS:
            return f"config · {label}", HAZARDS[label][0]
    # Only from the label field. In a tail the words are the server's, not the dashboard's — a
    # symbol called `flat_map` matched `flat T` out of the storage legend, and a statement can
    # contain any word in this file.
    if off < COL_LABEL or not sec:
        for key in lookups:
            hit = _ANY.get(key)
            if hit:
                # Named in another panel's section. Say which, so a term that means one thing under
                # `storage` and another under `memory` is never quietly answered from the wrong one.
                where = "" if hit[0] == sec else f"  (legend: {hit[0]})"
                return f"{sec or hit[0]} · {hit[1]}{where}", hit[2]

    # Nothing named. The grid still knows what KIND of thing it is, and for a bar or a trace that
    # is the useful half of the answer: what it is a share of.
    if _BAR_AT <= off < _BAR_AT + COL_BAR and ch in "█░":
        return (f"{sec or 'panel'} · bar",
                f"`{label}` as a share of this row's own denominator, which the row's tail names. "
                f"Every bar and its trace divide by the same thing.")
    if ch in _SPARKS and off >= _BAR_AT + COL_BAR:
        return (f"{sec or 'panel'} · history",
                f"`{label}` over the recent past, oldest on the left, drawn against the same "
                f"denominator as the bar on this row.")
    if sec in PANELS:
        return sec, PANELS[sec]
    return None


def tip_box(tip, c, width):
    """The tooltip itself. Sized to its text, never past the terminal."""
    title, body = tip
    wrapped = textwrap.wrap(body, max(24, min(58, width - 6))) or [""]
    # A row is "│" + one space + the text + padding + "│", so it needs inner = text + 1. Sizing
    # `inner` to the longest line itself left that one row a column wider than its own box: the
    # padding went negative, the right border slid inward on every other row, and the longest line
    # printed over the frame beneath it.
    inner = max(len(title) + 2, max(len(x) for x in wrapped) + 1)
    out = [f"{c['dim']}┌─{c['r']}{c['yel']}{c['b']}{title}{c['r']}{c['dim']}"
           + "─" * max(0, inner - len(title) - 1) + f"┐{c['r']}"]
    for x in wrapped:
        out.append(f"{c['dim']}│{c['r']} {x}{' ' * (inner - len(x) - 1)}{c['dim']}│{c['r']}")
    out.append(f"{c['dim']}└" + "─" * inner + f"┘{c['r']}")
    return out, inner + 2


def place(box_w, box_h, mx, my, width, height):
    """Where to put the box: under the pointer, flipped at an edge so it is never clipped."""
    top = my + 1 if my + 1 + box_h <= height else my - box_h
    left = mx + 1 if mx + 1 + box_w <= width else width - box_w
    return max(0, min(top, height - box_h)), max(0, left)
