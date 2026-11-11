"""Email tables must keep detail columns separate from full-width content."""

from html.parser import HTMLParser

from sadoc import alerts
from sadoc.email_template import Block, render_html


class EmailTables(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.tables = []
        self.stack = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            rows = []
            self.tables.append(rows)
            self.stack.append(rows)
        elif tag == "tr":
            self.stack[-1].append(0)
        elif tag == "td":
            self.stack[-1][-1] += int(dict(attrs).get("colspan", "1"))

    def handle_endtag(self, tag):
        if tag == "table":
            self.stack.pop()


def test_mixed_blocks_do_not_create_inconsistent_table_columns():
    # A paragraph in the detail table used to occupy only the label column,
    # stretching it and pushing the values to the far edge of the email.
    blocks = [
        Block(label="Repository", value="owner/repo"),
        Block(label="Language", value="Gradle"),
        Block(kind="paragraph", value="A long account of the journey. " * 10),
        Block(kind="note", tone="warn", value="This needs a hand."),
        Block(kind="code", value="build.gradle\nversion = '1.0'"),
        Block(kind="list", items=["One update", "Another update"]),
        Block(label="Next stop", value="The western shore"),
    ]
    tables = EmailTables(render_html("News", blocks, footer="Fair winds"))

    assert tables.tables
    for rows in tables.tables:
        assert len(set(rows)) == 1, f"Mixed column counts in an email table: {rows}"


def test_html_preserves_and_escapes_every_block_kind():
    blocks = [
        Block(label="Repository <name>", value="owner/repo & friends"),
        Block(label="Link", value="https://example.com/pr?a=1&b=2"),
        Block(kind="paragraph", value="A <strange> tongue"),
        Block(kind="note", value="Wait & see"),
        Block(kind="code", value="if a < b:\n    build()"),
        Block(kind="list", items=["First <update>", "Second & last"]),
    ]
    markup = render_html(
        "News <today>", blocks, accent_label="Sadoc & company", footer="Fair winds <friend>"
    )

    for escaped in (
        "Repository &lt;name&gt;",
        "owner/repo &amp; friends",
        'href="https://example.com/pr?a=1&amp;b=2"',
        "A &lt;strange&gt; tongue",
        "Wait &amp; see",
        "if a &lt; b:\n    build()",
        "First &lt;update&gt;",
        "Second &amp; last",
        "News &lt;today&gt;",
        "Sadoc &amp; company",
        "Fair winds &lt;friend&gt;",
    ):
        assert escaped in markup


def test_unsupported_alert_keeps_the_plain_text_details():
    alert = alerts.unsupported_ecosystem(
        "owner/repo", ["Gradle"], ["build.gradle"], also_supported=True
    )
    assert "Repository: owner/repo" in alert.text
    assert "Language: Gradle" in alert.text
    assert "build.gradle" in alert.text
    assert "rest of the repository" in alert.text
    assert "<table" not in alert.text
