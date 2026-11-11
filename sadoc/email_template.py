"""Email rendering.

Email clients are hostile to CSS: no external stylesheets, no reliable support
for <style> blocks, and inconsistent table rendering. So the markup is plain
tables with every style inline, which is the one thing that works everywhere.

The look is a single-column card: a coloured header bar with the persona's
name, the subject, the content, and a footer with the signature.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

# A restrained palette that reads well in both light and dark mode.
INK = "#1f2430"
MUTED = "#6b7280"
LINE = "#e5e7eb"
PAPER = "#ffffff"
CANVAS = "#f6f7f9"
ACCENT = "#1d4ed8"
ACCENT_SOFT = "#eff4ff"
WARN = "#b45309"
WARN_SOFT = "#fffbeb"
DANGER = "#b91c1c"
DANGER_SOFT = "#fef2f2"
OK = "#047857"
OK_SOFT = "#ecfdf5"


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
    for block in blocks:
        if block.kind == "row":
            value = _e(block.value)
            # A bare URL becomes a link.
            if block.value.startswith("http") and " " not in block.value:
                value = _link(block.value)
            rows.append(
                f'<tr><td style="padding:6px 16px 6px 0;color:{MUTED};font-size:13px;'
                f'white-space:nowrap;vertical-align:top">{_e(block.label)}</td>'
                f'<td style="padding:6px 0;color:{INK};font-size:14px;'
                f'vertical-align:top">{value}</td></tr>'
            )
        elif block.kind == "paragraph":
            rows.append(
                f'<tr><td style="padding:10px 24px;color:{INK};font-size:14px;'
                f'line-height:1.55">{_e(block.value)}</td></tr>'
            )
        elif block.kind == "note":
            tone = {
                "warn": (WARN, WARN_SOFT),
                "danger": (DANGER, DANGER_SOFT),
                "ok": (OK, OK_SOFT),
            }.get(block.tone, (MUTED, "#f9fafb"))
            rows.append(
                f'<tr><td style="padding:12px 16px;background:{tone[1]};'
                f"border-left:3px solid {tone[0]};color:{INK};font-size:13px;"
                f'line-height:1.55;border-radius:3px">{_e(block.value)}</td></tr>'
            )
        elif block.kind == "code":
            rows.append(
                f'<tr><td style="padding:12px 16px;background:{CANVAS};'
                f"border:1px solid {LINE};border-radius:4px;color:{INK};"
                f"font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
                f"font-size:12px;line-height:1.5;white-space:pre-wrap;"
                f'word-break:break-word">{_e(block.value)}</td></tr>'
            )
        elif block.kind == "list":
            items = "".join(
                f'<li style="margin:0 0 4px 0;color:{INK};font-size:14px;line-height:1.5">'
                f"{_e(item)}</li>"
                for item in block.items
            )
            rows.append(
                f'<tr><td style="padding:8px 24px 8px 40px"><ul '
                f'style="margin:0;padding:0">{items}</ul></td></tr>'
            )

    footer_html = ""
    if footer:
        footer_html = (
            f'<tr><td style="padding:16px 24px;border-top:1px solid {LINE};'
            f'color:{MUTED};font-size:12px;line-height:1.6">{footer}</td></tr>'
        )

    heading_html = ""
    if heading:
        heading_html = (
            f'<tr><td style="padding:20px 24px 4px 24px;color:{INK};font-size:19px;'
            f'font-weight:700;letter-spacing:-0.01em">{_e(heading)}</td></tr>'
        )
    else:
        # Without a heading the subject line becomes the title.
        heading_html = (
            f'<tr><td style="padding:20px 24px 4px 24px;color:{INK};font-size:19px;'
            f'font-weight:700;letter-spacing:-0.01em">{_e(title)}</td></tr>'
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title></head>
<body style="margin:0;padding:0;background:{CANVAS};color:{INK};
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
{pre}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="background:{CANVAS};padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
 style="max-width:600px;width:100%;background:{PAPER};border:1px solid {LINE};
border-radius:10px;overflow:hidden;box-shadow:0 1px 2px rgba(16,24,40,.05)">
<tr><td style="background:{accent};padding:14px 24px;color:#ffffff;font-size:13px;
font-weight:600;letter-spacing:.04em;text-transform:uppercase">{_e(accent_label)}
</td></tr>
{heading_html}
<tr><td style="padding:0 0 8px 0"><table role="presentation" width="100%"
 cellpadding="0" cellspacing="0">{"".join(rows)}</table></td></tr>
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
