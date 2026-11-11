"""Email rendering.

Email clients are hostile to CSS: no external stylesheets, no reliable support
for <style> blocks, and inconsistent table rendering. So the markup is plain
tables with every style inline, which is the one thing that works everywhere.

The look is a letter from Middle-earth: parchment, forest green, old gold,
and a serif masthead. Detail rows live in their own tables so prose and
callouts cannot distort the label/value columns.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from itertools import groupby

# Warm parchment and woodland ink, with distinct colours for alert severity.
INK = "#302c22"
MUTED = "#706650"
LINE = "#d8c9a6"
PAPER = "#faf5e8"
CANVAS = "#e9e2d2"
GOLD = "#d9bd78"
ACCENT = "#284b3c"
ACCENT_SOFT = "#edf0e5"
WARN = "#6b431d"
WARN_SOFT = "#f5ebd3"
DANGER = "#74362d"
DANGER_SOFT = "#f6e8de"
OK = "#2c4c35"
OK_SOFT = "#eaf0df"
# Bookish display faces, with local fallbacks for clients that block web fonts.
DISPLAY_FONT = "'Palatino Linotype','Book Antiqua',Palatino,'Noto Serif Display',Georgia,serif"


@dataclass
class Block:
    """One row of the email: a label and a value, or a paragraph."""

    kind: str = "row"  # row | paragraph | note | code | list
    label: str = ""
    value: str = ""
    items: list[str] = field(default_factory=list)
    tone: str = ""  # "", "warn", "danger", "ok"


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _link(url: str, text: str | None = None) -> str:
    if not url:
        return ""
    label = text or url
    return (
        f'<a href="{_e(url)}" style="color:{ACCENT};text-decoration:none;'
        f'font-weight:600">{_e(label)}</a>'
    )


def render_html(
    title: str,
    blocks: list[Block],
    heading: str = "",
    accent: str = ACCENT,
    accent_soft: str = ACCENT_SOFT,
    footer: str = "",
    preheader: str = "",
    accent_label: str = "",
) -> str:
    """Render an email as HTML."""
    pre = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{_e(preheader)}</div>'
        if preheader
        else ""
    )

    rows = []
    for is_detail, group in groupby(blocks, key=lambda block: block.kind == "row"):
        if is_detail:
            details = []
            for block in group:
                value = _e(block.value)
                # A bare URL becomes a link.
                if block.value.startswith("http") and " " not in block.value:
                    value = _link(block.value)
                details.append(
                    f'<tr><td width="34%" style="padding:7px 16px 7px 0;color:{MUTED};'
                    f"font-size:13px;line-height:1.6;vertical-align:top;"
                    f'overflow-wrap:anywhere;word-break:break-word">{_e(block.label)}</td>'
                    f'<td style="padding:7px 0;color:{INK};font-size:14px;line-height:1.6;'
                    f'vertical-align:top;overflow-wrap:anywhere;word-break:break-word">'
                    f"{value}</td></tr>"
                )
            rows.append(
                '<tr><td style="padding:8px 0 12px">'
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                'style="table-layout:fixed;font-family:Arial,Helvetica,sans-serif">'
                f"{''.join(details)}</table></td></tr>"
            )
            continue

        for block in group:
            if block.kind == "paragraph":
                rows.append(
                    f'<tr><td style="padding:10px 0;color:{INK};font-size:16px;'
                    f'line-height:1.7;overflow-wrap:anywhere;word-break:break-word">'
                    f"{_e(block.value)}</td></tr>"
                )
            elif block.kind == "note":
                tone = {
                    "warn": (WARN, WARN_SOFT),
                    "danger": (DANGER, DANGER_SOFT),
                    "ok": (OK, OK_SOFT),
                }.get(block.tone, (accent, accent_soft))
                rows.append(
                    '<tr><td style="padding:10px 0"><table role="presentation" width="100%" '
                    'cellpadding="0" cellspacing="0" style="table-layout:fixed">'
                    f'<tr><td style="padding:14px 16px;background:{tone[1]};'
                    f"border-left:3px solid {tone[0]};color:{INK};font-size:15px;"
                    f'line-height:1.65;overflow-wrap:anywhere;word-break:break-word">'
                    f"{_e(block.value)}</td></tr></table></td></tr>"
                )
            elif block.kind == "code":
                rows.append(
                    '<tr><td style="padding:10px 0"><table role="presentation" width="100%" '
                    'cellpadding="0" cellspacing="0" style="table-layout:fixed">'
                    f'<tr><td style="padding:14px 16px;background:{CANVAS};'
                    f"border:1px solid {LINE};color:{INK};"
                    f"font-family:Consolas,Menlo,monospace;"
                    f"font-size:12px;line-height:1.6;white-space:pre-wrap;"
                    f'overflow-wrap:anywhere;word-break:break-word">{_e(block.value)}'
                    "</td></tr></table></td></tr>"
                )
            elif block.kind == "list":
                items = "".join(
                    f'<li style="margin:0 0 6px;color:{INK};font-size:15px;line-height:1.65">'
                    f"{_e(item)}</li>"
                    for item in block.items
                )
                rows.append(
                    '<tr><td style="padding:8px 0;overflow-wrap:anywhere;word-break:break-word">'
                    f'<ul style="margin:0;padding:0 0 0 20px">{items}</ul></td></tr>'
                )

    footer_html = ""
    if footer:
        footer_html = (
            f'<tr><td style="padding:16px 24px;border-top:1px solid {LINE};'
            f'color:{MUTED};font-size:13px;font-style:italic;line-height:1.7">'
            f"{_e(footer)}</td></tr>"
        )

    # Without a heading the subject line becomes the title.
    heading_html = (
        '<tr><td style="padding:24px 24px 8px">'
        f'<h1 style="margin:0;color:{INK};font-size:25px;line-height:1.3;'
        f"font-family:{DISPLAY_FONT};font-weight:400;font-variant:small-caps;"
        f'letter-spacing:.025em;overflow-wrap:anywhere;word-break:break-word">'
        f"{_e(heading or title)}</h1></td></tr>"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title></head>
<body style="margin:0;padding:0;background:{CANVAS};color:{INK};
font-family:Georgia,'Times New Roman',serif">
{pre}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="background:{CANVAS};padding:28px 12px">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
 style="max-width:600px;width:100%;table-layout:fixed;background:{PAPER};
border:1px solid {LINE};border-top:3px solid {GOLD};border-collapse:separate">
<tr><td style="background:{accent};padding:22px 24px;border-bottom:2px solid {GOLD}">
<p style="margin:0 0 10px;color:{GOLD};font-family:Arial,Helvetica,sans-serif;
font-size:10px;line-height:1.6;letter-spacing:.16em;text-transform:uppercase">
From the havens of Middle-earth</p>
<p style="margin:0;color:{PAPER};font-family:{DISPLAY_FONT};font-size:34px;
font-variant:small-caps;line-height:1.2;letter-spacing:.06em;
overflow-wrap:anywhere;word-break:break-word">{_e(accent_label)}</p>
<p aria-hidden="true" style="margin:10px 0 0;color:{GOLD};font-size:14px;
letter-spacing:.3em">&#10022; &nbsp; &#10022; &nbsp; &#10022;</p>
</td></tr>
{heading_html}
<tr><td style="padding:0 24px 18px"><table role="presentation" width="100%"
 cellpadding="0" cellspacing="0" style="table-layout:fixed">{"".join(rows)}</table></td></tr>
{footer_html}
</table>
</td></tr></table>
</body></html>"""


def render_text(title: str, blocks: list[Block], footer: str = "") -> str:
    """The plain-text twin of the HTML, for clients that prefer it.

    The title is not repeated: in an email it is the subject line.
    """
    lines: list[str] = []
    for block in blocks:
        if block.kind == "row":
            lines.append(f"{block.label}: {block.value}")
        elif block.kind in ("paragraph", "note") or block.kind == "code":
            lines += ["", block.value, ""]
        elif block.kind == "list":
            lines += [""] + [f"  - {item}" for item in block.items]
    if footer:
        lines += ["", footer]
    return "\n".join(lines).strip()
