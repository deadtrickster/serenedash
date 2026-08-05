"""serenedash.export — the same frames, as SVG and as a page.

The renderer already returns a list of ANSI lines, and every view goes through it. So an export is a
pure function of those lines rather than a second dashboard: `--format html` and `--format svg`
cannot disagree with the terminal about what a panel says, for the same reason `--format json`
cannot disagree with the MCP server.

## Why SVG and not a <pre>

A terminal guarantees one glyph is one cell. A browser does not. Box-drawing and block glyphs
(`┌ ─ █ ░ ▇`) routinely resolve to a different font from the ASCII beside them, with a different
advance width, so a run of `─` stops being exactly N cells and the right border walks a little
further out on every line. Choosing a better font stack only moves the problem to whichever machine
lacks that font.

Pinning fixes it instead of hiding it: every styled run carries its true column as `x` and an
explicit `textLength`, so the glyphs are fitted back onto the grid whatever font supplied them. It
also means the export looks right on a machine with no good monospace font at all.
"""
import html
import re

# One entry per SGR colour this renderer emits. Deliberately a fixed palette rather than the
# terminal's, because an export is read somewhere the terminal's theme does not exist.
PAL = {"30": "#4b5263", "31": "#e06c75", "32": "#98c379", "33": "#e5c07b",
       "34": "#61afef", "35": "#c678dd", "36": "#56b6c2", "37": "#abb2bf"}

# Cell advance and line height in px. Any pair works - the pinning makes the glyphs fit the cell
# rather than the cell follow the glyphs - so these only set how large the export renders.
CW, LH = 7.22, 15.0

_SGR = re.compile("\033\\[([0-9;]*)m")


def runs(line):
    """[(column, text, colour, bold, dim)] for one ANSI line, columns counted in visible cells.

    The column is what makes the export exact: it is the count of visible characters before the run,
    so a run knows where it belongs independently of how wide anything renders.
    """
    out, col, fg, bold, dim, pos = [], 0, None, False, False, 0
    for m in _SGR.finditer(line):
        text = line[pos:m.start()]
        if text:
            out.append((col, text, fg, bold, dim))
            col += len(text)
        for code in m.group(1).split(";"):
            if code in ("0", ""):
                fg, bold, dim = None, False, False
            elif code == "1":
                bold = True
            elif code == "2":
                dim = True
            elif code in PAL:
                fg = PAL[code]
        pos = m.end()
    if line[pos:]:
        out.append((col, line[pos:], fg, bold, dim))
    return out


def svg(lines, pad=8, background="#101318"):
    """One SVG for a list of ANSI lines, every run pinned to its column."""
    cols = max((sum(len(t) for _, t, *_ in runs(ln)) for ln in lines), default=0)
    w, h = cols * CW + pad * 2, len(lines) * LH + pad * 2
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
             f'viewBox="0 0 {w:.0f} {h:.0f}" font-size="12" '
             f'font-family="DejaVu Sans Mono,Liberation Mono,Menlo,Consolas,monospace">',
             f'<rect width="100%" height="100%" fill="{background}" rx="6"/>']
    for row, line in enumerate(lines):
        y = pad + (row + 1) * LH - 4
        for col, text, fg, bold, dim in runs(line):
            if not text.strip():
                continue                       # whitespace needs no element; the grid places the rest
            attrs = f' fill="{fg or PAL["37"]}"'
            if bold:
                attrs += ' font-weight="700"'
            if dim:
                attrs += ' opacity=".55"'
            parts.append(
                f'<text x="{pad + col * CW:.2f}" y="{y:.0f}" '
                f'textLength="{len(text) * CW:.2f}" lengthAdjust="spacingAndGlyphs" '
                f'xml:space="preserve"{attrs}>{html.escape(text)}</text>')
    parts.append("</svg>")
    return "".join(parts)


CSS = """
:root{color-scheme:dark light}
body{margin:0;padding:2rem 1.2rem;max-width:1600px;margin-inline:auto;background:#1a1d23;
 color:#c8ccd4;font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
h1{font-size:1.5rem;margin:0 0 .2rem;color:#e6e9ef}
h2{font-size:1.05rem;margin:2.2rem 0 .5rem;color:#e5c07b;font-weight:600}
.sub{color:#7d8590;margin:0 0 1.4rem}
.term{background:#101318;border:1px solid #2b303b;border-radius:6px;padding:.6rem;
 overflow-x:auto;margin:0}
.term svg{display:block;max-width:none}
@media(prefers-color-scheme:light){body{background:#fbfbfd;color:#2b303b}h1{color:#11141a}
 .sub{color:#4b5263}}
:root[data-theme=light] body{background:#fbfbfd;color:#2b303b}
:root[data-theme=dark] body{background:#1a1d23;color:#c8ccd4}
"""


def page(sections, title="serenedash", subtitle=""):
    """A complete HTML document. `sections` is [(heading, [ansi lines])].

    Complete, not a fragment: doctype, `<html>`, and above all `<meta charset>`. Every box-drawing
    glyph in these frames is multi-byte UTF-8, and a viewer with nothing to go on is free to decode
    them as something else - which turns a dashboard into mojibake and looks like a rendering bug in
    the dashboard rather than a missing line in its head.
    """
    body = [f"<h1>{html.escape(title)}</h1>"]
    if subtitle:
        body.append(f'<p class="sub">{html.escape(subtitle)}</p>')
    for heading, lines in sections:
        if not lines:
            continue
        body.append(f"<h2>{html.escape(heading)}</h2>")
        body.append(f'<div class="term">{svg(lines)}</div>')
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f"<title>{html.escape(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
            + "\n".join(body) + "\n</body>\n</html>\n")
