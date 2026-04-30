from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MirrorInfo:
    name: str
    url: str
    country: str
    protocol: str
    sync_age: str
    latency_ms: int
    speed_mbps: int
    status: str
    source: str = "mock"
    enabled: bool = True


@dataclass(slots=True)
class TestJob:
    name: str
    url: str
    state: str
    progress: float
    latency_ms: int
    speed_mbps: int
    stage: str
    detail: str = ""


DEFAULT_MIRRORS = [
    MirrorInfo(
        name="arch.mirror.pkgbuild.com",
        url="https://arch.mirror.pkgbuild.com/$repo/os/$arch",
        country="Canada",
        protocol="HTTPS",
        sync_age="14 min",
        latency_ms=24,
        speed_mbps=184,
        status="Excellent",
        source="demo",
    ),
    MirrorInfo(
        name="mirror.umd.edu",
        url="https://mirror.umd.edu/archlinux/$repo/os/$arch",
        country="USA",
        protocol="HTTPS",
        sync_age="28 min",
        latency_ms=37,
        speed_mbps=152,
        status="Healthy",
        source="demo",
    ),
    MirrorInfo(
        name="ftp.osuosl.org",
        url="https://ftp.osuosl.org/pub/archlinux/$repo/os/$arch",
        country="USA",
        protocol="HTTPS",
        sync_age="41 min",
        latency_ms=52,
        speed_mbps=118,
        status="Healthy",
        source="demo",
    ),
    MirrorInfo(
        name="mirror.math.princeton.edu",
        url="https://mirror.math.princeton.edu/pub/archlinux/$repo/os/$arch",
        country="USA",
        protocol="HTTPS",
        sync_age="1 h 12 min",
        latency_ms=68,
        speed_mbps=96,
        status="Watch",
        source="demo",
    ),
]
