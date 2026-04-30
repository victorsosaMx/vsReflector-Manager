from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass


_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "vsreflector-manager")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "settings.json")


@dataclass(slots=True)
class AppSettings:
    countries: str = "MX,US,CA"
    protocols: str = "https"
    latest: int = 20
    number: int = 10
    age: float = 12.0
    sort_by: str = "rate"
    completion_percent: int = 100
    timeout_seconds: int = 60
    use_ipv4: bool = False
    use_ipv6: bool = False
    include_isos: bool = False
    live_test_limit: int = 5


def load_settings() -> AppSettings:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        d = AppSettings()
        return AppSettings(
            countries=data.get("countries", d.countries),
            protocols=data.get("protocols", d.protocols),
            latest=int(data.get("latest", d.latest)),
            number=int(data.get("number", d.number)),
            age=float(data.get("age", d.age)),
            sort_by=data.get("sort_by", d.sort_by),
            completion_percent=int(data.get("completion_percent", d.completion_percent)),
            timeout_seconds=int(data.get("timeout_seconds", d.timeout_seconds)),
            use_ipv4=bool(data.get("use_ipv4", d.use_ipv4)),
            use_ipv6=bool(data.get("use_ipv6", d.use_ipv6)),
            include_isos=bool(data.get("include_isos", d.include_isos)),
            live_test_limit=int(data.get("live_test_limit", d.live_test_limit)),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return AppSettings()


def save_settings(settings: AppSettings) -> None:
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(asdict(settings), f, indent=2)
