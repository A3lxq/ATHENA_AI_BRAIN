"""Tests for article content extraction (docs/design/research-ingestion.md §7).

Fixtures are real, hand-written HTML -- verified empirically (not assumed)
against trafilatura 2.2.0 to actually exercise the "real article" vs.
"no meaningful content" paths this module's failure-mode table depends on.
"""

from __future__ import annotations

from athena.research.extract import ExtractedArticle, extract_article

_ARTICLE_HTML = """<html>
<head>
<title>The Rise of Async Python: A Deep Dive</title>
<meta name="author" content="Jane Doe">
<meta name="date" content="2026-03-15">
</head>
<body>
<nav>Home | About | Contact</nav>
<article>
<h1>The Rise of Async Python: A Deep Dive</h1>
<p class="byline">By Jane Doe, published 2026-03-15</p>
<p>Python's async ecosystem has matured considerably over the last several
years, moving from a niche feature into a mainstream tool relied upon by
web frameworks, database drivers, and task queues alike.</p>
<p>This article explores the evolution of asyncio, the role of event loops,
and how libraries such as httpx and aiosqlite have embraced async-first
design. We also examine common pitfalls developers encounter when mixing
sync and async code in the same codebase.</p>
<p>Finally, we look ahead to what the next few years might bring, including
structured concurrency primitives and improvements to debugging tools for
asynchronous stack traces.</p>
</article>
<footer>Copyright 2026 Example News</footer>
</body>
</html>"""

_ARTICLE_URL = "https://example-news.test/articles/async-python"

# No <nav> -- a nav-only fixture was empirically found to still produce
# fallback text from trafilatura (it falls back to "best available text"),
# so this fixture (a cookie-consent notice plus a footer, no nav, no
# article content) is the one actually verified to return `None`.
_BOILERPLATE_HTML = """<html>
<head><title>Site</title></head>
<body>
<div class="cookie-consent-banner" role="dialog">
<p>This website uses cookies to ensure you get the best experience on our
website. We use cookies to personalize content, provide social media
features, and analyze our traffic.</p>
<a href="/cookies">Cookie Policy</a>
<button>Accept All</button>
<button>Reject All</button>
</div>
<footer>
<p>Copyright 2026 Example Corp. All rights reserved.</p>
</footer>
</body>
</html>"""

_EMPTY_HTML = "<html><head><title>Empty</title></head><body>   \n  \t </body></html>"


def test_extract_article_returns_body_and_metadata_for_a_real_article() -> None:
    result = extract_article(_ARTICLE_HTML, _ARTICLE_URL)

    assert result is not None
    assert isinstance(result, ExtractedArticle)
    assert result.title == "The Rise of Async Python: A Deep Dive"
    assert result.author == "Jane Doe"
    assert result.date == "2026-03-15"
    assert "async ecosystem has matured" in result.markdown_body
    assert "structured concurrency primitives" in result.markdown_body


def test_extract_article_returns_none_for_boilerplate_only_page() -> None:
    assert extract_article(_BOILERPLATE_HTML, "https://example.test/nothing-here") is None


def test_extract_article_returns_none_for_whitespace_only_body() -> None:
    assert extract_article(_EMPTY_HTML, "https://example.test/empty") is None


def test_extract_article_markdown_body_has_no_duplicated_metadata_header() -> None:
    """`with_metadata=True` markdown output embeds a YAML front-matter block
    (title/author/url/hostname/sitename/date) ahead of the body -- this
    module deliberately avoids that path (module docstring), so the body
    must never contain that block's field lines."""
    result = extract_article(_ARTICLE_HTML, _ARTICLE_URL)

    assert result is not None
    assert not result.markdown_body.startswith("---")
    assert "author: Jane Doe" not in result.markdown_body
    assert "hostname:" not in result.markdown_body
    assert "sitename:" not in result.markdown_body
