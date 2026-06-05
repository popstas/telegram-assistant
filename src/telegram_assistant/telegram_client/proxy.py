"""Parse proxy URLs into Telethon-compatible proxy dicts."""

from __future__ import annotations

from urllib.parse import unquote, urlparse

SOCKS5 = 2
SOCKS4 = 1
HTTP = 3

_SCHEME_MAP = {
    "socks5": SOCKS5,
    "socks4": SOCKS4,
    "http": HTTP,
    "https": HTTP,
}


def parse_proxy_url(proxy_url: str | None) -> dict | None:
    """Parse a proxy URL into the dict Telethon accepts as ``proxy=``.

    Returns ``None`` if ``proxy_url`` is empty/None. Raises ``ValueError`` on
    unsupported schemes or missing hostname. Default port is 1080 for SOCKS,
    8080 for HTTP. Credentials are URL-decoded.
    """
    if not proxy_url:
        return None

    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    proxy_type = _SCHEME_MAP.get(scheme)
    if proxy_type is None:
        raise ValueError(
            f"Unsupported proxy scheme: {scheme!r}. "
            "Supported: socks5, socks4, http, https"
        )
    if not parsed.hostname:
        sanitized = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        raise ValueError(f"Proxy URL missing hostname: {sanitized}")

    port = parsed.port or (1080 if scheme.startswith("socks") else 8080)

    result: dict = {
        "proxy_type": proxy_type,
        "addr": parsed.hostname,
        "port": port,
    }
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result
