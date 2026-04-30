from __future__ import annotations

import email.utils
import difflib
import json
import os
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from vs_reflector_manager.data import DEFAULT_MIRRORS, MirrorInfo, TestJob


MIRRORLIST_PATH = "/etc/pacman.d/mirrorlist"
MIRROR_STATUS_URL = "https://www.archlinux.org/mirrors/status/json/"
MIRROR_STATUS_TTL = 3600


@dataclass(slots=True)
class MirrorSource:
    mirrors: list[MirrorInfo]
    source_name: str
    command: str
    generated_at: str
    retrieved_at: str
    warning: str = ""


@dataclass(slots=True)
class GenerationOptions:
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


@dataclass(slots=True)
class GenerationResult:
    success: bool
    command: list[str]
    mirrorlist_text: str
    diff_text: str
    message: str


@dataclass(slots=True)
class ApplyResult:
    success: bool
    backup_path: str
    message: str


@dataclass(slots=True)
class MirrorStatusCache:
    valid_until: float
    mirrors: list[MirrorInfo]
    countries: set[str]


_mirror_status_cache: MirrorStatusCache | None = None
_cache_lock = threading.Lock()


def fetch_mirror_status_from_api() -> MirrorStatusCache | None:
    global _mirror_status_cache
    with _cache_lock:
        if _mirror_status_cache is not None and time.time() < _mirror_status_cache.valid_until:
            return _mirror_status_cache

    try:
        request = urllib.request.Request(
            MIRROR_STATUS_URL,
            headers={"User-Agent": "vsReflector-Manager/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)

        mirrors: list[MirrorInfo] = []
        countries: set[str] = set()
        now_val = time.time()

        for item in data.get("urls", []):
            if not item.get("active", False):
                continue
            url = item.get("url")
            if not url:
                continue

            protocol = (item.get("protocol") or "https").upper()
            if protocol not in ("HTTPS", "HTTP", "RSYNC"):
                continue

            delay = item.get("delay") or 0
            duration_avg = item.get("duration_avg") or 1.0
            try:
                estimated_latency = int(delay + duration_avg * 1000)
            except (TypeError, ValueError):
                estimated_latency = 0
            score = item.get("score") or 99
            try:
                speed = max(1, int(200 / max(float(score), 1)))
            except (TypeError, ValueError, ZeroDivisionError):
                speed = 1

            if item.get("completion_pct", 0) < 1.0:
                status = "Incomplete"
            elif speed > 150:
                status = "Excellent"
            elif speed > 100:
                status = "Healthy"
            elif speed > 50:
                status = "Watch"
            else:
                status = "Slow"

            parsed = urlparse(url)
            host = parsed.hostname or url

            sync_time = item.get("last_sync")
            if sync_time:
                try:
                    sync_str = str(sync_time)
                    sync_dt = datetime.fromisoformat(sync_str.replace("Z", "+00:00"))
                    age_seconds = (datetime.now(UTC) - sync_dt).total_seconds()
                    age_minutes = int(age_seconds / 60)
                    if age_minutes < 60:
                        sync_age = f"{age_minutes} min"
                    elif age_minutes < 1440:
                        sync_age = f"{age_minutes // 60} h"
                    else:
                        sync_age = f"{age_minutes // 1440} d"
                except Exception:
                    sync_age = "Unknown"
            else:
                sync_age = "Unknown"

            mirror = MirrorInfo(
                name=host,
                url=url,
                country=item.get("country_code", item.get("country", "??")),
                protocol=protocol,
                sync_age=sync_age,
                latency_ms=estimated_latency,
                speed_mbps=speed,
                status=status,
                source="archlinux.org",
                enabled=True,
            )
            mirrors.append(mirror)

            country = item.get("country", "")
            if country:
                countries.add(country)

        if mirrors:
            result = MirrorStatusCache(
                valid_until=now_val + MIRROR_STATUS_TTL,
                mirrors=mirrors,
                countries=countries,
            )
            with _cache_lock:
                _mirror_status_cache = result
            return result

    except Exception as err:
        print(f"Failed to fetch mirror status: {err}")

    return None


def load_mirrors() -> MirrorSource:
    mirrors, metadata = parse_current_mirrorlist()
    if mirrors:
        return MirrorSource(
            mirrors=mirrors,
            source_name="system mirrorlist",
            command=metadata.get("with", "Unknown"),
            generated_at=metadata.get("when", "Unknown"),
            retrieved_at=metadata.get("retrieved", "Unknown"),
            warning=reflector_status_warning(),
        )

    status_cache = fetch_mirror_status_from_api()
    if status_cache is not None:
        usable = [m for m in status_cache.mirrors if m.enabled]
        if usable:
            return MirrorSource(
                mirrors=usable,
                source_name="Arch Mirror Status API",
                command=f"Fetched {len(usable)} mirrors from archlinux.org",
                generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
                retrieved_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
                warning="",
            )

    return MirrorSource(
        mirrors=list(DEFAULT_MIRRORS),
        source_name="demo dataset",
        command="No system mirrorlist was available",
        generated_at="Unknown",
        retrieved_at="Unknown",
        warning="Using demo data because /etc/pacman.d/mirrorlist could not be parsed.",
    )


def parse_current_mirrorlist(path: str = MIRRORLIST_PATH) -> tuple[list[MirrorInfo], dict[str, str]]:
    if not os.path.exists(path):
        return [], {}

    metadata: dict[str, str] = {}
    mirrors: list[MirrorInfo] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("# With:"):
                metadata["with"] = stripped.removeprefix("# With:").strip()
            elif stripped.startswith("# When:"):
                metadata["when"] = stripped.removeprefix("# When:").strip()
            elif stripped.startswith("# Retrieved:"):
                metadata["retrieved"] = stripped.removeprefix("# Retrieved:").strip()
            elif stripped.startswith("Server ="):
                url = stripped.removeprefix("Server =").strip()
                mirrors.append(mirror_from_url(url))
    return mirrors, metadata


def mirror_from_url(url: str) -> MirrorInfo:
    parsed = urlparse(url)
    host = parsed.hostname or url
    protocol = (parsed.scheme or "https").upper()
    latency_ms = synthetic_latency(host)
    speed_mbps = synthetic_speed(latency_ms)
    return MirrorInfo(
        name=host,
        url=url,
        country=infer_country(host),
        protocol=protocol,
        sync_age="From active mirrorlist",
        latency_ms=latency_ms,
        speed_mbps=speed_mbps,
        status=health_status(latency_ms),
        source="system",
    )


def reflector_status_warning() -> str:
    if shutil.which("reflector") is None:
        return "Reflector is not installed. The app can still inspect the current mirrorlist."
    return ""


def synthetic_latency(seed: str) -> int:
    return 18 + (sum(ord(char) for char in seed) % 62)


def synthetic_speed(latency_ms: int) -> int:
    return max(42, 220 - int(latency_ms * 1.8))


def health_status(latency_ms: int) -> str:
    if latency_ms <= 35:
        return "Excellent"
    if latency_ms <= 55:
        return "Healthy"
    if latency_ms <= 75:
        return "Watch"
    return "Slow"


def infer_country(host: str) -> str:
    value = host.lower()
    hints = {
        "losangeles": "USA",
        "fastly": "Global CDN",
        "frankfurt": "Germany",
        "singapore": "Singapore",
        "johannesburg": "South Africa",
        "taipei": "Taiwan",
        "de": "Germany",
        "ca": "Canada",
        "mx": "Mexico",
        "us": "USA",
    }
    for token, country in hints.items():
        if token in value:
            return country
    return "Unknown"


def build_test_jobs(mirrors: list[MirrorInfo], limit: int = 5) -> list[TestJob]:
    jobs: list[TestJob] = []
    for mirror in mirrors[:limit]:
        jobs.append(
            TestJob(
                name=mirror.name,
                url=mirror.url,
                state="Queued",
                progress=0.0,
                latency_ms=0,
                speed_mbps=0,
                stage="Waiting",
                detail=mirror.country,
            )
        )
    return jobs


def run_probe(url: str, on_update) -> None:
    parsed = urlparse(url)
    host = parsed.hostname
    scheme = parsed.scheme or "https"
    port = parsed.port or (443 if scheme == "https" else 80)
    if not host:
        on_update(state="Failed", stage="Invalid URL", progress=1.0, detail="No host detected")
        return

    try:
        start = time.perf_counter()
        on_update(state="Running", stage="DNS lookup", progress=0.12, detail=f"Resolving {host}")
        addr_info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        dns_elapsed = elapsed_ms(start)
        on_update(
            state="Running",
            stage="DNS lookup",
            progress=0.22,
            latency_ms=dns_elapsed,
            detail=f"{len(addr_info)} address candidates",
        )

        on_update(state="Running", stage="TCP connect", progress=0.36, detail=f"Opening {host}:{port}")
        tcp_start = time.perf_counter()
        sock = socket.create_connection((host, port), timeout=4.0)
        tcp_elapsed = elapsed_ms(tcp_start)
        total_latency = dns_elapsed + tcp_elapsed
        on_update(
            state="Running",
            stage="TCP connect",
            progress=0.55,
            latency_ms=total_latency,
            detail=f"Connected in {tcp_elapsed} ms",
        )

        with sock:
            if scheme == "https":
                on_update(state="Running", stage="TLS handshake", progress=0.72, detail="Negotiating TLS")
                tls_start = time.perf_counter()
                context = ssl.create_default_context()
                with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                    tls_elapsed = elapsed_ms(tls_start)
                    total_latency += tls_elapsed
                    tls_sock.sendall(
                        (
                            f"HEAD / HTTP/1.1\r\n"
                            f"Host: {host}\r\n"
                            "User-Agent: vsReflector-Manager\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("ascii")
                    )
                    response_start = time.perf_counter()
                    tls_sock.recv(128)
                    total_latency += elapsed_ms(response_start)
            else:
                sock.sendall(
                    (
                        f"HEAD / HTTP/1.1\r\n"
                        f"Host: {host}\r\n"
                        "User-Agent: vsReflector-Manager\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                response_start = time.perf_counter()
                sock.recv(128)
                total_latency += elapsed_ms(response_start)

        speed_guess = synthetic_speed(max(total_latency, 1))
        on_update(
            state="Complete",
            stage="Validated",
            progress=1.0,
            latency_ms=total_latency,
            speed_mbps=speed_guess,
            detail=f"Probe completed at {http_date_now()}",
        )
    except Exception as err:
        on_update(
            state="Failed",
            stage="Probe failed",
            progress=1.0,
            detail=str(err),
        )


def elapsed_ms(start: float) -> int:
    return max(1, int((time.perf_counter() - start) * 1000))


def http_date_now() -> str:
    return email.utils.format_datetime(datetime.now(UTC))


def build_reflector_command(options: GenerationOptions, output_path: str) -> list[str]:
    command = [
        "reflector",
        "--verbose",
        "--cache-timeout",
        "60",
        "--latest",
        str(options.latest),
        "--number",
        str(options.number),
        "--protocol",
        options.protocols,
        "--sort",
        options.sort_by,
        "--completion-percent",
        str(options.completion_percent),
        "--save",
        output_path,
    ]
    if options.countries.strip():
        command.extend(["--country", options.countries])
    if options.age > 0:
        command.extend(["--age", str(options.age)])
    if options.use_ipv4:
        command.append("--ipv4")
    if options.use_ipv6:
        command.append("--ipv6")
    if options.include_isos:
        command.append("--isos")
    return command


def generate_mirrorlist(options: GenerationOptions) -> GenerationResult:
    if shutil.which("reflector") is None:
        return GenerationResult(
            success=False,
            command=["reflector"],
            mirrorlist_text="",
            diff_text="",
            message="Reflector is not installed.",
        )

    with tempfile.NamedTemporaryFile(prefix="vs-reflector-", suffix=".mirrorlist", delete=False) as handle:
        output_path = handle.name

    command = build_reflector_command(options, output_path)
    try:
        run = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=options.timeout_seconds,
        )
        with open(output_path, encoding="utf-8") as generated:
            mirrorlist_text = generated.read()
        diff_text = build_mirrorlist_diff(mirrorlist_text)
        message = run.stderr.strip() or "Mirrorlist generated successfully."
        return GenerationResult(
            success=True,
            command=command,
            mirrorlist_text=mirrorlist_text,
            diff_text=diff_text,
            message=message,
        )
    except subprocess.CalledProcessError as err:
        message = (err.stderr or err.stdout or str(err)).strip()
        if "failed to retrieve mirrorstatus data" in message.lower():
            message = (
                "Reflector could not retrieve mirrorstatus data. "
                "Check network or cached reflector data before generating a preview.\n\n"
                f"{message}"
            )
        return GenerationResult(
            success=False,
            command=command,
            mirrorlist_text="",
            diff_text="",
            message=message,
        )
    except Exception as err:
        return GenerationResult(
            success=False,
            command=command,
            mirrorlist_text="",
            diff_text="",
            message=str(err),
        )
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def build_mirrorlist_diff(generated_text: str, current_path: str = MIRRORLIST_PATH) -> str:
    try:
        with open(current_path, encoding="utf-8") as current:
            current_text = current.read()
    except OSError as err:
        return f"Failed to read current mirrorlist: {err}"

    diff = difflib.unified_diff(
        current_text.splitlines(),
        generated_text.splitlines(),
        fromfile=current_path,
        tofile="generated mirrorlist",
        lineterm="",
    )
    return "\n".join(diff) or "No differences against the current mirrorlist."


def apply_mirrorlist(mirrorlist_text: str, target_path: str = MIRRORLIST_PATH) -> ApplyResult:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{target_path}.bak.{timestamp}"
    temp_file = None

    try:
        with tempfile.NamedTemporaryFile(
            prefix="vs-reflector-apply-", suffix=".mirrorlist", mode="w", delete=False
        ) as f:
            f.write(mirrorlist_text)
            temp_file = f.name

        cmd = (
            f"cp {shlex.quote(target_path)} {shlex.quote(backup_path)} && "
            f"cp {shlex.quote(temp_file)} {shlex.quote(target_path)}"
        )
        result = subprocess.run(
            ["pkexec", "sh", "-c", cmd],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            if "Authorization canceled" in error_msg or result.returncode == 127:
                return ApplyResult(
                    success=False,
                    backup_path="",
                    message="Canceled: authentication was canceled.",
                )
            return ApplyResult(
                success=False,
                backup_path="",
                message=f"Failed: {error_msg or 'Could not obtain root privileges'}",
            )

        return ApplyResult(
            success=True,
            backup_path=backup_path,
            message=f"Mirrorlist applied successfully. Backup saved to {backup_path}",
        )

    except Exception as err:
        return ApplyResult(
            success=False,
            backup_path="",
            message=f"Failed: {err}",
        )
    finally:
        if temp_file:
            try:
                os.unlink(temp_file)
            except OSError:
                pass


@dataclass(slots=True)
class RestoreResult:
    success: bool
    restored_from: str
    message: str


def list_backups(target_path: str = MIRRORLIST_PATH) -> list[tuple[str, str]]:
    backups: list[tuple[str, str]] = []
    directory = os.path.dirname(target_path) or "."
    basename = os.path.basename(target_path)
    prefix = f"{basename}.bak."
    try:
        for entry in os.listdir(directory):
            if entry.startswith(prefix):
                full_path = os.path.join(directory, entry)
                try:
                    mtime = os.path.getmtime(full_path)
                    timestamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                    backups.append((full_path, timestamp))
                except OSError:
                    pass
    except OSError:
        pass
    backups.sort(key=lambda x: x[0], reverse=True)
    return backups


def restore_mirrorlist(backup_path: str, target_path: str = MIRRORLIST_PATH) -> RestoreResult:
    if not os.path.exists(backup_path):
        return RestoreResult(
            success=False,
            restored_from=backup_path,
            message=f"Backup file not found: {backup_path}",
        )
    try:
        cmd = f"cp {shlex.quote(backup_path)} {shlex.quote(target_path)}"
        result = subprocess.run(
            ["pkexec", "sh", "-c", cmd],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            if "Authorization canceled" in error_msg or result.returncode == 127:
                return RestoreResult(
                    success=False,
                    restored_from=backup_path,
                    message="Canceled: authentication was canceled.",
                )
            return RestoreResult(
                success=False,
                restored_from=backup_path,
                message=f"Failed: {error_msg or 'Could not obtain root privileges'}",
            )
        return RestoreResult(
            success=True,
            restored_from=backup_path,
            message=f"Restored from {os.path.basename(backup_path)}",
        )
    except Exception as err:
        return RestoreResult(
            success=False,
            restored_from=backup_path,
            message=f"Failed to restore: {err}",
        )
