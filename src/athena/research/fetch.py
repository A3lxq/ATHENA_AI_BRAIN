"""SSRF-safe URL fetching -- the single, audited network-egress point for
the whole research/ingestion feature (docs/design/research-ingestion.md §0,
§2.1, §6).

Mitigates the threat `docs/SECURITY_MODEL.md` TB-4 already named for this
exact feature (checklist item 16): a model or user supplying
`http://169.254.169.254/...` (cloud metadata), an internal `10.x`/
`172.16-31.x`/`192.168.x`/loopback address, a non-HTTP scheme, or a hostname
that resolves to a public IP at validation time but a private one at
connect time (DNS rebinding).

Layers, applied to every hop of a redirect chain, not just the first URL:
  1. Scheme allowlist (http/https only).
  2. Resolve every A/AAAA record for the hostname and reject if ANY resolved
     address is private/loopback/link-local/reserved/multicast/unspecified,
     including IPv4-mapped-IPv6-encoded addresses.
  3. Pin the actual TCP connection to the validated IP (never the hostname)
     via `_PinnedTransport`, closing the DNS-rebinding TOCTOU gap a
     validate-then-hand-off-the-hostname implementation would leave open.
     TLS SNI/certificate validation still checks the real hostname via the
     `sni_hostname` transport extension.
  4. No automatic redirect-following (`follow_redirects=False`, httpx's own
     default) -- each hop is walked and re-validated manually here, capped
     at `_MAX_REDIRECTS`.
  5. A content-type allowlist and a streamed, hard-capped body size (never
     trusting `Content-Length` alone).

`fetch_url` never calls `trafilatura`'s own `fetch_url()`/`fetch_response()`
-- doing so would open a second, unaudited network-egress path parallel to
this one (design doc §0).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

__all__ = ["FetchedPage", "FetchRefused", "fetch_url"]

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB
_MAX_REDIRECTS = 5
_USER_AGENT = "ATHENA-AI-BRAIN-Research/1.0 (+https://github.com/A3lxq/ATHENA_AI_BRAIN)"

_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


class FetchRefused(Exception):
    """Raised when a URL or a redirect hop is refused by this module's SSRF
    protections -- distinct from a plain network failure (timeout,
    connection reset, DNS resolution failure for an otherwise-legitimate
    reason), which propagates as whatever exception `httpx`/`socket` raised
    and should be treated as potentially transient/retryable by callers.
    """


@dataclass(frozen=True)
class FetchedPage:
    url: str
    """The URL actually fetched -- the final hop after any redirects."""
    html: str
    content_type: str


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Unwrap IPv4-mapped IPv6 (`::ffff:10.0.0.1`) before checking -- otherwise
    # an encoded private v4 address slips past a check that only inspects
    # the outer (v6) address family.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_and_validate(hostname: str) -> str:
    """Resolve every A/AAAA record for `hostname` and return one validated,
    safe IP address literal to connect to.

    Checks EVERY resolved address, not just the first: which address a
    resolver happens to return first is not something this code controls,
    and a hostname could legitimately resolve to a mix of public and
    private addresses.
    """
    try:
        addr_infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise FetchRefused(f"could not resolve hostname {hostname!r}: {exc}") from exc

    if not addr_infos:
        raise FetchRefused(f"hostname {hostname!r} resolved to no addresses")

    resolved_ips: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        # typeshed types `sockaddr[0]` as `str | int` to cover both the
        # AF_INET/AF_INET6 (host, port[, ...]) shape and other address
        # families this call never actually produces (proto=IPPROTO_TCP) --
        # narrow it explicitly rather than widen `resolved_ips`'s type.
        ip_str = str(sockaddr[0])
        ip = ipaddress.ip_address(ip_str)
        if _is_unsafe_ip(ip):
            raise FetchRefused(
                f"hostname {hostname!r} resolves to a disallowed address: {ip_str}"
            )
        resolved_ips.append(ip_str)

    return resolved_ips[0]


class _PinnedTransport(httpx.HTTPTransport):
    """Rewrites every outgoing request to connect to a pre-validated IP
    address rather than letting the transport re-resolve the hostname
    itself at connect time.

    This is what actually closes the DNS-rebinding TOCTOU gap: a plain
    "resolve, validate, then hand the ORIGINAL HOSTNAME to the HTTP client"
    implementation lets the client's own, independent DNS resolution at
    connect time return a different (attacker-controlled) answer than the
    one just validated. `sni_hostname` is set to the original hostname so
    TLS SNI and certificate validation still check the real domain, not the
    raw IP (design doc §0).
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        validated_ip = _resolve_and_validate(original_host)
        request.url = request.url.copy_with(host=validated_ip)
        request.headers["host"] = original_host
        request.extensions["sni_hostname"] = original_host
        return super().handle_request(request)


def _validate_scheme(url: str) -> None:
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise FetchRefused(f"disallowed URL scheme {scheme!r} (only http/https allowed): {url}")


def fetch_url(url: str, *, _transport: httpx.BaseTransport | None = None) -> FetchedPage:
    """Fetch `url`, applying SSRF protections at every redirect hop.

    Raises `FetchRefused` for anything this function's safety checks
    reject -- treat as non-retryable. Any other exception (timeout,
    connection reset, TLS failure, a genuine DNS failure) is a plain,
    potentially transient network failure and propagates as-is.

    `_transport` is a test-only injection hook (a `httpx.MockTransport` in
    unit tests, to exercise redirect-walking/content-type/size-cap logic
    without going through real DNS resolution and `_PinnedTransport`'s own
    SSRF checks, which are tested directly and separately). Production
    code must never pass this -- it defaults to the real `_PinnedTransport`.
    """
    _validate_scheme(url)
    current_url = url

    with httpx.Client(
        transport=_transport if _transport is not None else _PinnedTransport(),
        timeout=_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            _validate_scheme(current_url)
            if not urlsplit(current_url).hostname:
                raise FetchRefused(f"URL has no hostname: {current_url}")

            response = client.get(current_url)

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise FetchRefused(
                        f"redirect response with no Location header: {current_url}"
                    )
                current_url = str(httpx.URL(current_url).join(location))
                continue

            content_type = (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            if content_type not in _ALLOWED_CONTENT_TYPES:
                raise FetchRefused(
                    f"disallowed content-type {content_type!r} for {current_url} "
                    f"(only {sorted(_ALLOWED_CONTENT_TYPES)} allowed)"
                )

            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_BODY_BYTES:
                    raise FetchRefused(
                        f"response body for {current_url} exceeded {_MAX_BODY_BYTES} bytes"
                    )

            return FetchedPage(
                url=current_url,
                html=bytes(body).decode(response.encoding or "utf-8", errors="replace"),
                content_type=content_type,
            )

    raise FetchRefused(f"too many redirects (> {_MAX_REDIRECTS}) starting from {url}")
