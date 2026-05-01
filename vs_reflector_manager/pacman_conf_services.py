from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass


PACMAN_CONF_PATH = "/etc/pacman.conf"


@dataclass
class PacmanOptions:
    color: bool = False
    parallel_downloads: int = 5
    verbose_pkg_lists: bool = False
    i_love_candy: bool = False
    multilib: bool = False
    core_testing: bool = False
    extra_testing: bool = False
    multilib_testing: bool = False


@dataclass
class PacmanApplyResult:
    success: bool
    message: str


def _parse_options(text: str) -> PacmanOptions:
    color = bool(re.search(r"^Color\s*$", text, re.MULTILINE))
    verbose = bool(re.search(r"^VerbosePkgLists\s*$", text, re.MULTILINE))
    i_love_candy = bool(re.search(r"^ILoveCandy\s*$", text, re.MULTILINE))
    m = re.search(r"^ParallelDownloads\s*=\s*(\d+)", text, re.MULTILINE)
    parallel = int(m.group(1)) if m else 5
    multilib = bool(re.search(r"^\[multilib\]", text, re.MULTILINE))
    core_testing = bool(re.search(r"^\[core-testing\]", text, re.MULTILINE))
    extra_testing = bool(re.search(r"^\[extra-testing\]", text, re.MULTILINE))
    multilib_testing = bool(re.search(r"^\[multilib-testing\]", text, re.MULTILINE))
    return PacmanOptions(
        color=color,
        parallel_downloads=parallel,
        verbose_pkg_lists=verbose,
        i_love_candy=i_love_candy,
        multilib=multilib,
        core_testing=core_testing,
        extra_testing=extra_testing,
        multilib_testing=multilib_testing,
    )


def read_pacman_options(path: str = PACMAN_CONF_PATH) -> PacmanOptions:
    try:
        with open(path, encoding="utf-8") as f:
            return _parse_options(f.read())
    except OSError:
        return PacmanOptions()


def _toggle_flag(text: str, flag: str, enable: bool) -> str:
    if enable:
        return re.sub(rf"^#{re.escape(flag)}\s*$", flag, text, flags=re.MULTILINE)
    return re.sub(rf"^{re.escape(flag)}\s*$", f"#{flag}", text, flags=re.MULTILINE)


def _set_parallel_downloads(text: str, n: int) -> str:
    return re.sub(
        r"^#?ParallelDownloads\s*=\s*\d+",
        f"ParallelDownloads = {n}",
        text,
        flags=re.MULTILINE,
    )


def _toggle_repo(text: str, repo: str, enable: bool) -> str:
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip("\n").strip()
        if enable and stripped == f"#[{repo}]":
            result.append(f"[{repo}]\n")
            i += 1
            while i < len(lines):
                nl = lines[i]
                ns = nl.strip()
                if ns.startswith("#Include"):
                    result.append(nl.replace("#Include", "Include", 1))
                    i += 1
                    break
                elif not ns or ns.startswith("#"):
                    result.append(nl)
                    i += 1
                else:
                    break
            continue
        if not enable and stripped == f"[{repo}]":
            result.append(f"#[{repo}]\n")
            i += 1
            while i < len(lines):
                nl = lines[i]
                ns = nl.strip()
                if ns.startswith("Include"):
                    result.append(nl.replace("Include", "#Include", 1))
                    i += 1
                    break
                elif not ns:
                    result.append(nl)
                    i += 1
                else:
                    break
            continue
        result.append(line)
        i += 1
    return "".join(result)


def _toggle_candy(text: str, enable: bool) -> str:
    has_active = bool(re.search(r"^ILoveCandy\s*$", text, re.MULTILINE))
    has_commented = bool(re.search(r"^#ILoveCandy\s*$", text, re.MULTILINE))
    if enable:
        if has_active:
            return text
        if has_commented:
            return re.sub(r"^#ILoveCandy\s*$", "ILoveCandy", text, flags=re.MULTILINE)
        # Insert after CheckSpace or ParallelDownloads line
        return re.sub(
            r"^(CheckSpace|ParallelDownloads\s*=.*?)$",
            r"\1\nILoveCandy",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        if has_commented:
            return text
        return re.sub(r"^ILoveCandy\s*$", "#ILoveCandy", text, flags=re.MULTILINE)


def build_new_conf(original: str, options: PacmanOptions) -> str:
    current = _parse_options(original)
    text = original
    text = _toggle_flag(text, "Color", options.color)
    text = _toggle_flag(text, "VerbosePkgLists", options.verbose_pkg_lists)
    text = _toggle_candy(text, options.i_love_candy)
    text = _set_parallel_downloads(text, options.parallel_downloads)
    for repo, new_val, cur_val in (
        ("multilib", options.multilib, current.multilib),
        ("core-testing", options.core_testing, current.core_testing),
        ("extra-testing", options.extra_testing, current.extra_testing),
        ("multilib-testing", options.multilib_testing, current.multilib_testing),
    ):
        if new_val != cur_val:
            text = _toggle_repo(text, repo, new_val)
    return text


def apply_pacman_options(
    options: PacmanOptions, path: str = PACMAN_CONF_PATH
) -> PacmanApplyResult:
    temp_file = None
    try:
        with open(path, encoding="utf-8") as f:
            original = f.read()
        new_text = build_new_conf(original, options)
        with tempfile.NamedTemporaryFile(
            prefix="vs-pacman-", suffix=".conf", mode="w", delete=False
        ) as f:
            f.write(new_text)
            temp_file = f.name
        cmd = f"cp {shlex.quote(temp_file)} {shlex.quote(path)}"
        result = subprocess.run(["pkexec", "sh", "-c", cmd], capture_output=True, text=True)
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            if "Authorization canceled" in error or result.returncode == 127:
                return PacmanApplyResult(success=False, message="Canceled: authentication was canceled.")
            return PacmanApplyResult(
                success=False, message=f"Failed: {error or 'Could not obtain root privileges'}"
            )
        return PacmanApplyResult(success=True, message="pacman.conf updated successfully.")
    except Exception as err:
        return PacmanApplyResult(success=False, message=f"Failed: {err}")
    finally:
        if temp_file:
            try:
                os.unlink(temp_file)
            except OSError:
                pass
