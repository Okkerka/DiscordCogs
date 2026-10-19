"""One host policy guards both media boundaries.

A media request must never reach the local network, a cloud metadata
endpoint, or a multicast or reserved range - not even through the IPv4
spellings a resolver still accepts.
"""
from __future__ import annotations

import pytest

from TidalPlayerExp.domain.media_hosts import (
    BLOCKED,
    LOOPBACK,
    PUBLIC,
    classify_media_host,
)
from TidalPlayerExp.playback.ffmpeg import _public_media_url
from TidalPlayerExp.providers.youtube_resolver import _valid_https_url


@pytest.mark.parametrize("host", [
    "169.254.169.254",       # cloud metadata
    "2852039166",            # the same address in decimal
    "0xa9fea9fe",            # the same address in hex
    "169.254.169.254.",      # the same address with a trailing dot
    "0",                     # 0.0.0.0, the unspecified address
    "10.0.0.1",              # private
    "192.168.1.1",           # private
    "224.0.0.1",             # multicast
    "240.0.0.1",             # reserved
    "metadata.example.local",
    "metadata.example.internal",
    "",
])
def test_local_and_non_canonical_hosts_are_blocked(host):
    assert classify_media_host(host) == BLOCKED


@pytest.mark.parametrize("host", [
    "127.0.0.1",
    "127.1",                 # short form a resolver still accepts
    "0x7f000001",            # hex form of the same address
    "localhost",
    "build.localhost",
    "::1",
    "::ffff:127.0.0.1",
])
def test_loopback_hosts_are_reported_separately(host):
    assert classify_media_host(host) == LOOPBACK


@pytest.mark.parametrize("host", [
    "cdn.example.com",
    "resources.tidal.com",
    "1.googleusercontent.com",
])
def test_public_hostnames_stay_public(host):
    assert classify_media_host(host) == PUBLIC


@pytest.mark.parametrize("host", ["not a host", "example", "example..com", "-bad-.com"])
def test_malformed_hostnames_are_blocked(host):
    assert classify_media_host(host) == BLOCKED


@pytest.mark.parametrize("url", [
    "https://2852039166/latest/meta-data/",
    "https://0xa9fea9fe/latest/meta-data/",
    "https://169.254.169.254./latest/meta-data/",
    "https://0/latest/meta-data/",
    "https://10.0.0.1/internal.mp3",
    "https://metadata.example.local/x",
])
def test_ffmpeg_boundary_rejects_local_media(url):
    assert _public_media_url(url) is False


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/track.mp3",
    "http://cdn.example.com/track.mp3",
])
def test_ffmpeg_boundary_allows_public_media(url):
    assert _public_media_url(url) is True


def test_ffmpeg_boundary_keeps_local_sources_working():
    # A local media server is a legitimate source, so loopback stays allowed.
    assert _public_media_url("https://127.0.0.1:8080/track.mp3") is True
    assert _public_media_url("http://localhost:8080/track.mp3") is True


@pytest.mark.parametrize("url", [
    "https://2852039166/latest/meta-data/",
    "https://127.1/track.mp3",
    "https://169.254.169.254/track.mp3",
    "https://metadata.example.local/track.mp3",
])
def test_resolver_boundary_rejects_local_and_loopback_media(url):
    with pytest.raises(ValueError):
        _valid_https_url(url)


def test_resolver_boundary_allows_provider_cdns():
    assert _valid_https_url("https://cdn.example.com/track.mp3") == (
        "https://cdn.example.com/track.mp3"
    )
