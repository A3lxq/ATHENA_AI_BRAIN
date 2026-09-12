"""Article content extraction (docs/design/research-ingestion.md §0, §2.2).

A thin wrapper over `trafilatura.extract()`/`trafilatura.extract_metadata()`,
operating only on already-fetched HTML -- this module never calls
`trafilatura.fetch_url()`/`fetch_response()`, which would open a second,
unaudited network-egress path parallel to `athena.research.fetch.fetch_url`
(the project's one audited fetcher, §0).

Empirical finding that shaped this module's shape (verified against
trafilatura 2.2.0, not assumed): `trafilatura.extract(html, url=url,
output_format="markdown", with_metadata=True)` embeds title/author/date/url/
hostname/sitename as a YAML front-matter block (`---\\n...\\n---\\n`) ahead of
the Markdown body, which would have to be parsed back apart to keep
`markdown_body` free of duplicated metadata text. Calling `extract()` with
`with_metadata=False` for the body and `extract_metadata()` separately for
title/author/date is empirically cleaner and more reliable: it returns the
same `trafilatura.settings.Document` the front-matter block is built from
(`.title`/`.author`/`.date`, each `str | None`, `None` -- not `""` -- when
absent) with no string to re-parse, so that approach is what this module
uses. Trafilatura's markdown body itself still opens with a `# Title`
heading -- that is normal Markdown rendering of the article's own title, not
duplicated metadata boilerplate, and is left in place.

Also verified empirically: a page with no genuine article content does not
reliably return `None` on its own -- a nav-only or nav-plus-boilerplate page
can still be returned as fallback text (trafilatura falling back to "best
available text" when nothing more article-shaped is found). Only a body
with no extractable text at all (empty, JS-mounted-only, or pure
non-article boilerplate with no `<nav>`) reliably returns `None`. This
module therefore does not attempt to second-guess trafilatura's own
judgment beyond checking for `None`/whitespace-only output.
"""

from __future__ import annotations

from dataclasses import dataclass

import trafilatura

__all__ = ["ExtractedArticle", "extract_article"]


@dataclass(frozen=True)
class ExtractedArticle:
    title: str | None
    markdown_body: str
    author: str | None
    date: str | None


def extract_article(html: str, url: str) -> ExtractedArticle | None:
    """Extract clean Markdown article content and metadata from `html`.

    Returns `None` when trafilatura finds no meaningful content -- a
    paywalled, JS-only, or genuinely empty page is an expected, common
    outcome (design doc §5), not an error; this function never raises.
    """
    body = trafilatura.extract(html, url=url, output_format="markdown", with_metadata=False)
    if body is None or not body.strip():
        return None

    metadata = trafilatura.extract_metadata(html, default_url=url)

    return ExtractedArticle(
        title=metadata.title,
        markdown_body=body.strip(),
        author=metadata.author,
        date=metadata.date,
    )
