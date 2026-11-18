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
        Block(kind="code", value="if a < b:\n    build()  # **raw**"),
        Block(kind="list", items=["First <update>", "Second & **last**"]),
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
        "if a &lt; b:\n    build()  # **raw**",
        "First &lt;update&gt;",
        "Second &amp; <strong>last</strong>",
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
    assert "<code>build.gradle</code>" in alert.html
    assert "rest of the repository" in alert.text
    assert "<table" not in alert.text


def test_branch_repair_renders_markdown_and_preserves_plain_text():
    cause = (
        "The fix is applied.\n\n"
        "- **Root cause:** missing `corepack`\n"
        "- **Verified `build`:** exit code 0\n\n"
        "See the logs.\nNext steps are in the PR."
    )
    alert = alerts.branch_fixed("owner/repo", "https://example.com/pr/1", "build", cause)

    assert "<strong>Root cause:</strong> missing <code>corepack</code>" in alert.html
    assert "<strong>Verified <code>build</code>:</strong> exit code 0" in alert.html
    assert alert.html.count("<li>") == 2
    assert "<ul " in alert.html
    assert "See the logs.<br>Next steps are in the PR." in alert.html
    assert cause in alert.text


def test_review_markdown_escapes_html_and_keeps_fenced_code_literal():
    body = (
        "**<img src=x>**\n\n"
        "```sh\n"
        'echo "**literal**" && echo "<script>"\n'
        "```\n\n"
        "1. Check [the logs](https://example.com/logs?a=1&b=2).\n"
        "2. Avoid [this](javascript:alert)."
    )
    alert = alerts.review_received("owner/repo", 1, "https://example.com/pr/1", "user", body, False)

    assert "<strong>&lt;img src=x&gt;</strong>" in alert.html
    assert "<pre " in alert.html
    assert "echo &quot;**literal**&quot; &amp;&amp; echo &quot;&lt;script&gt;&quot;" in alert.html
    assert "<ol " in alert.html
    assert 'href="https://example.com/logs?a=1&amp;b=2"' in alert.html
    assert "[this](javascript:alert)" in alert.html
    assert 'href="javascript:' not in alert.html
