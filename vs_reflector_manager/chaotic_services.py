from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field

CHAOTIC_MIRRORLIST_PATH = "/etc/pacman.d/chaotic-mirrorlist"
PACMAN_CONF_PATH = "/etc/pacman.conf"

SETUP_COMMANDS = [
    "sudo pacman-key --recv-key 3056513887B78AEB --keyserver keyserver.ubuntu.com",
    "sudo pacman-key --lsign-key 3056513887B78AEB",
    "sudo pacman -U 'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-keyring.pkg.tar.zst'",
    "sudo pacman -U 'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-mirrorlist.pkg.tar.zst'",
]

PACMAN_CONF_SNIPPET = "[chaotic-aur]\nInclude = /etc/pacman.d/chaotic-mirrorlist"


@dataclass
class ChaoticMirror:
    url: str
    active: bool
    label: str
    latency_ms: int = 0
    state: str = "idle"


@dataclass
class ChaoticState:
    installed: bool
    configured: bool
    mirrors: list[ChaoticMirror] = field(default_factory=list)


@dataclass
class ChaoticApplyResult:
    success: bool
    message: str


def detect_state() -> ChaoticState:
    installed = os.path.exists(CHAOTIC_MIRRORLIST_PATH)
    configured = False
    if os.path.exists(PACMAN_CONF_PATH):
        try:
            with open(PACMAN_CONF_PATH, encoding="utf-8") as f:
                configured = "[chaotic-aur]" in f.read()
        except OSError:
            pass
    mirrors: list[ChaoticMirror] = []
    if installed:
        try:
            mirrors = parse_mirrorlist()
        except Exception:
            pass
    return ChaoticState(installed=installed, configured=configured, mirrors=mirrors)


def parse_mirrorlist(path: str = CHAOTIC_MIRRORLIST_PATH) -> list[ChaoticMirror]:
    mirrors: list[ChaoticMirror] = []
    current_label = ""

    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("##"):
            current_label = stripped.lstrip("#").strip()
        elif stripped.startswith("#Server") and "=" in stripped:
            url = stripped.split("=", 1)[1].strip()
            mirrors.append(ChaoticMirror(url=url, active=False, label=current_label))
        elif stripped.startswith("Server ="):
            url = stripped.split("=", 1)[1].strip()
            mirrors.append(ChaoticMirror(url=url, active=True, label=current_label))
        elif stripped.startswith("#") and not stripped.startswith("#Server"):
            text = stripped.lstrip("#").strip()
            if text and not text.startswith("*") and not text.startswith("By"):
                current_label = text

    return mirrors


def rebuild_mirrorlist(
    mirrors: list[ChaoticMirror],
    path: str = CHAOTIC_MIRRORLIST_PATH,
) -> str:
    url_to_active: dict[str, bool] = {m.url: m.active for m in mirrors}

    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Server ="):
            url = stripped.split("=", 1)[1].strip()
            if url_to_active.get(url, True):
                result.append(line)
            else:
                result.append("#" + line)
        elif stripped.startswith("#Server") and "=" in stripped:
            url = stripped.split("=", 1)[1].strip()
            if url_to_active.get(url, False):
                result.append(line.replace("#Server", "Server", 1))
            else:
                result.append(line)
        else:
            result.append(line)

    return "".join(result)


def apply_chaotic_mirrorlist(
    text: str,
    path: str = CHAOTIC_MIRRORLIST_PATH,
) -> ChaoticApplyResult:
    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="vs-chaotic-", suffix=".mirrorlist", mode="w", delete=False
        ) as f:
            f.write(text)
            temp_file = f.name

        cmd = f"cp {shlex.quote(temp_file)} {shlex.quote(path)}"
        result = subprocess.run(
            ["pkexec", "sh", "-c", cmd],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            if "Authorization canceled" in error or result.returncode == 127:
                return ChaoticApplyResult(success=False, message="Canceled: authentication was canceled.")
            return ChaoticApplyResult(success=False, message=f"Failed: {error or 'Could not obtain root privileges'}")
        return ChaoticApplyResult(success=True, message="Chaotic AUR mirrorlist updated successfully.")
    except Exception as err:
        return ChaoticApplyResult(success=False, message=f"Failed: {err}")
    finally:
        if temp_file:
            try:
                os.unlink(temp_file)
            except OSError:
                pass
