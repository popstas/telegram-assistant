"""Tests for proxy URL parsing."""

from __future__ import annotations

import pytest

from telegram_assistant.telegram_client.proxy import (
    HTTP,
    SOCKS4,
    SOCKS5,
    parse_proxy_url,
)


def test_none_returns_none() -> None:
    assert parse_proxy_url(None) is None


def test_empty_returns_none() -> None:
    assert parse_proxy_url("") is None


def test_socks5_with_explicit_port() -> None:
    assert parse_proxy_url("socks5://host:1080") == {
        "proxy_type": SOCKS5,
        "addr": "host",
        "port": 1080,
    }


def test_socks5_url_decodes_credentials() -> None:
    result = parse_proxy_url("socks5://user:p%40ss@host:1080")
    assert result == {
        "proxy_type": SOCKS5,
        "addr": "host",
        "port": 1080,
        "username": "user",
        "password": "p@ss",
    }


def test_socks4_defaults_port_to_1080() -> None:
    result = parse_proxy_url("socks4://host")
    assert result is not None
    assert result["proxy_type"] == SOCKS4
    assert result["port"] == 1080


def test_http_defaults_port_to_8080() -> None:
    result = parse_proxy_url("http://host")
    assert result is not None
    assert result["proxy_type"] == HTTP
    assert result["port"] == 8080


def test_https_maps_to_http_type() -> None:
    result = parse_proxy_url("https://host:3128")
    assert result is not None
    assert result["proxy_type"] == HTTP
    assert result["port"] == 3128


def test_unsupported_scheme_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported proxy scheme"):
        parse_proxy_url("ftp://host:21")


def test_missing_hostname_raises() -> None:
    with pytest.raises(ValueError, match="missing hostname"):
        parse_proxy_url("socks5://")


def test_http_with_credentials_user_email_style() -> None:
    # Matches the kind of value users put in config: http://user:pass@host:port
    result = parse_proxy_url("http://popstas:secret@kz.popstas.ru:3128")
    assert result == {
        "proxy_type": HTTP,
        "addr": "kz.popstas.ru",
        "port": 3128,
        "username": "popstas",
        "password": "secret",
    }
