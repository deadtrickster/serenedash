"""serenedash.fmt"""
import re



C = {"r": "\033[0m", "dim": "\033[2m", "b": "\033[1m", "grn": "\033[32m", "yel": "\033[33m",
     "red": "\033[31m", "cyn": "\033[36m", "mag": "\033[35m", "blu": "\033[34m"}


NOCOLOR = dict.fromkeys(C, "")


# ── one column grid for every panel ─────────────────────────────────────────────────────────────
#
# Same discipline as ragdash: every row goes through line(), so the glyph column is a single ruler
# down the frame and the number after it lands on the same screen column in every panel. Building
# rows ad hoc is why the storage and memory bars used to start at different offsets.
# 22, not 16: `checkpoint_threshold` is 20 characters and `preserve_insertion_order` is 24, and a
# panel naming the settings that matter must not render them as `checkpoint_thres`.
COL_LABEL, COL_VALUE, COL_BAR = 22, 10, 18


SPARK = "▁▂▃▄▅▆▇█"


# Deep enough to fill the tail of a wide terminal; the renderer draws only the tail end that fits,
# so this is a retention limit rather than a width. At -n 5 it is a bit over two hours of history.
HIST = 160


def line(c, label, value="", glyph=None, tail="", lc=None, vc=None):
    # Ellipsis, not a clip: a label cut without a mark reads as the setting's actual name.
    lab = f"{label if len(label) <= COL_LABEL else label[:COL_LABEL - 1] + '…':<{COL_LABEL}}"
    val = value if len(value) <= COL_VALUE else value[:COL_VALUE - 1] + "…"
    g = " " * COL_BAR if glyph is None else glyph
    return (f"{lc or c['dim']}{lab}{c['r']}"
            f"{vc or ''}{val:>{COL_VALUE}}{c['r']}  {g}  {tail}")


def to_bytes(s):
    """'22.6 GiB' -> bytes. SereneDB returns pre-formatted sizes, so they must be parsed back to
    compare them — a ratio is the point, and you cannot divide two strings."""
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B|bytes)", str(s), re.I)
    if not m:
        return 0.0
    v, u = float(m.group(1)), m.group(2).upper().rstrip("B").rstrip("I")
    return v * {"": 1, "BYTES": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}.get(u, 1)


def dur(sec):
    """Coarse on purpose: at 3 days the hours matter, at 4 minutes the seconds do not."""
    if not sec:
        return "?"
    sec = int(sec)
    if sec < 120:
        return f"{sec}s"
    if sec >= 86400:
        return f"{sec // 86400}d {sec % 86400 // 3600}h"
    if sec >= 3600:
        return f"{sec // 3600}h {sec % 3600 // 60}m"
    return f"{sec // 60}m"


def human(n):
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"


def spark(v, w=HIST, top=None):
    v = [x for x in v[-w:] if x is not None]
    if len(v) < 2:
        return "·" * max(1, len(v))
    # With `top` given, every trace on the panel is drawn against the same ceiling, so their heights
    # can be read against each other. Self-scaling made a 260 MB pool that never moves render as a
    # full-height line beside a 34 GB one — each series stretched to its own min and max, which says
    # something about that series' variance and nothing about its size.
    if top:
        # Nothing to draw for a series that is flat at zero. The self-scaling branch below renders a
        # flat series as a mid-height bar on every sample, which turned thirteen empty pools into
        # thirteen identical stripes — ink that reads as activity and means the opposite.
        if not any(v):
            return ""
        return "".join(SPARK[min(7, int(max(0.0, x) / top * 7.99))] for x in v)
    lo, hi = min(v), max(v)
    if hi - lo < 1e-9:
        return SPARK[3] * len(v)
    return "".join(SPARK[min(7, int((x - lo) / (hi - lo) * 7.99))] for x in v)


def bar(frac, width, col):
    f = max(0.0, min(1.0, frac))
    k = int(round(f * width))
    return f"{col}{'█' * k}{C['dim'] if col else ''}{'░' * (width - k)}{C['r'] if col else ''}"


def strip(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "\033":
            while i < len(s) and s[i] != "m":
                i += 1
            i += 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def clip(s, n):
    """Truncate to n VISIBLE columns, keeping escapes whole and closing any colour left open.

    A raw `s[:n]` counts the bytes of every colour escape against the width, so a row carrying four
    of them loses about twenty real characters — which is why the engines line lost the capture name
    it was supposed to end with and cut mid-word at "ker…". Worse, the slice can land inside an
    escape, emitting a partial sequence that swallows the box border that follows it.
    """
    out, vis, i = [], 0, 0
    while i < len(s) and vis < n:
        if s[i] == "\033":
            j = i
            while j < len(s) and s[j] != "m":
                j += 1
            out.append(s[i:j + 1])
            i = j + 1
        else:
            out.append(s[i])
            vis += 1
            i += 1
    # Absorb any escapes left at the cut. They carry no width, so a string whose visible length
    # exactly equals n is NOT truncated just because its trailing reset has not been consumed —
    # appending "…" there added a real column, which is why the one memory row whose note filled the
    # field sat one cell right of every other row, and only when colour was on.
    while i < len(s) and s[i] == "\033":
        j = i
        while j < len(s) and s[j] != "m":
            j += 1
        out.append(s[i:j + 1])
        i = j + 1
    # The reset closes whatever colour the cut landed inside, but only if there was colour to close:
    # appending the global palette's escape unconditionally printed a literal "…[0m" under --no-color.
    return "".join(out) + ("…" + (C["r"] if "\033" in s else "") if i < len(s) else "")
