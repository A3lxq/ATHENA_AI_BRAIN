"""Tests for the SSRF-safe fetcher (docs/design/research-ingestion.md §7).

This is the highest-priority test surface in Phase 7 per the design doc's
own test strategy -- every rejection case TB-4 (docs/SECURITY_MODEL.md) was
threat-modeled against is exercised here.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import httpx
import pytest

from athena.research.fetch import (
    FetchRefused,
    _is_unsafe_ip,
    _resolve_and_validate,
    _validate_scheme,
    fetch_url,
)

# --- scheme allowlist ---------------------------------------------------


@pytest.mark.parametrize("scheme", ["file", "ftp", "gopher", "javascript"])
def test_validate_scheme_rejects_non_http_schemes(scheme: str) -> None:
    with pytest.raises(FetchRefused, match="disallowed URL scheme"):
        _validate_scheme(f"{scheme}://example.com/x")


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_validate_scheme_accepts_http_and_https(scheme: str) -> None:
    _validate_scheme(f"{scheme}://example.com/x")  # must not raise


# --- IP classification, including encoding tricks -----------------------


@pytest.mark.parametrize(
    "ip_str",
    [
        "10.0.0.1",  # private
        "172.16.0.1",  # private
        "192.168.1.1",  # private
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link-local -- the cloud-metadata address
        "0.0.0.0",  # noqa: S104 -- unspecified address, not a bind call
        "224.0.0.1",  # multicast
        "::1",  # loopback (v6)
        "fe80::1",  # link-local (v6)
        "::ffff:10.0.0.1",  # IPv4-mapped IPv6 encoding a private v4 address
        "::ffff:169.254.169.254",  # IPv4-mapped IPv6 encoding the metadata address
    ],
)
def test_is_unsafe_ip_rejects_private_and_encoded_addresses(ip_str: str) -> None:
    import ipaddress

    assert _is_unsafe_ip(ipaddress.ip_address(ip_str)) is True


@pytest.mark.parametrize("ip_str", ["93.184.216.34", "2606:4700:10::6814:179a"])
def test_is_unsafe_ip_accepts_public_addresses(ip_str: str) -> None:
    import ipaddress

    assert _is_unsafe_ip(ipaddress.ip_address(ip_str)) is False


# --- DNS resolution + validation -----------------------------------------


def _fake_getaddrinfo(
    ip_strs: list[str],
) -> Callable[..., list[tuple[object, object, object, str, tuple[str, int]]]]:
    def _fake(host: str, port: object, **kwargs: object) -> list[
        tuple[object, object, object, str, tuple[str, int]]
    ]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ip_strs]

    return _fake


def test_resolve_and_validate_rejects_a_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["10.0.0.5"]))
    with pytest.raises(FetchRefused, match="disallowed address"):
        _resolve_and_validate("internal.example.com")


def test_resolve_and_validate_rejects_if_any_resolved_address_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname resolving to a MIX of public and private addresses is
    refused -- every resolved address is checked, not just the first."""
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34", "169.254.169.254"])
    )
    with pytest.raises(FetchRefused, match="disallowed address"):
        _resolve_and_validate("mixed.example.com")


def test_resolve_and_validate_accepts_a_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34"]))
    assert _resolve_and_validate("public.example.com") == "93.184.216.34"


def test_resolve_and_validate_raises_on_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    with pytest.raises(FetchRefused, match="could not resolve"):
        _resolve_and_validate("nonexistent.invalid")


# --- fetch_url: real end-to-end SSRF rejection (no mocking) --------------


def test_fetch_url_refuses_loopback_target_end_to_end() -> None:
    """A real call through the whole pipeline (real DNS resolution, the
    real `_PinnedTransport`) against a loopback address must be refused --
    no mocking involved, this proves the wiring, not just the unit logic."""
    with pytest.raises(FetchRefused, match="disallowed address"):
        fetch_url("http://127.0.0.1:1/whatever")


def test_fetch_url_refuses_link_local_metadata_address_end_to_end() -> None:
    with pytest.raises(FetchRefused, match="disallowed address"):
        fetch_url("http://169.254.169.254/latest/meta-data/")


# --- fetch_url: redirect-walking / content-type / size-cap, via an
# injected mock transport (bypasses real DNS/_PinnedTransport on purpose --
# those are exercised separately above) --------------------------------


def test_fetch_url_returns_html_body_and_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>")

    page = fetch_url("http://example.test/page", _transport=httpx.MockTransport(handler))
    assert page.url == "http://example.test/page"
    assert "ok" in page.html
    assert page.content_type == "text/html"


def test_fetch_url_walks_and_revalidates_redirect_hops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://example.test/final"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>done</html>")

    page = fetch_url("http://example.test/start", _transport=httpx.MockTransport(handler))
    assert page.url == "http://example.test/final"
    assert "done" in page.html


def test_fetch_url_rejects_redirect_to_disallowed_scheme() -> None:
    """A redirect hop is re-validated exactly like the original URL -- a
    page redirecting to a non-http(s) scheme is refused, not followed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    with pytest.raises(FetchRefused, match="disallowed URL scheme"):
        fetch_url("http://example.test/start", _transport=httpx.MockTransport(handler))


def test_fetch_url_caps_redirect_chain_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/").removeprefix("hop") or 0)
        return httpx.Response(302, headers={"location": f"http://example.test/hop{n + 1}"})

    with pytest.raises(FetchRefused, match="too many redirects"):
        fetch_url("http://example.test/hop0", _transport=httpx.MockTransport(handler))


def test_fetch_url_rejects_disallowed_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    with pytest.raises(FetchRefused, match="disallowed content-type"):
        fetch_url("http://example.test/doc.pdf", _transport=httpx.MockTransport(handler))


def test_fetch_url_rejects_body_exceeding_size_cap() -> None:
    oversized = b"x" * (10 * 1024 * 1024 + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=oversized)

    with pytest.raises(FetchRefused, match="exceeded"):
        fetch_url("http://example.test/huge", _transport=httpx.MockTransport(handler))


# --- a real, live public URL (network-dependent) -------------------------


@pytest.mark.parametrize("url", ["https://example.com/"])
def test_fetch_url_succeeds_against_a_real_public_url(url: str) -> None:
    page = fetch_url(url)
    assert "<html" in page.html.lower()
    assert page.content_type in {"text/html", "application/xhtml+xml"}
