# Changelog

## [1.0.0] — 2026-04-29

Initial release.

### Features

**Dashboard**
- Live overview: mirror source, total count, median latency, best candidate
- Source badge: system mirrorlist, Arch API, or demo fallback

**Mirrors**
- Visual rows with country, protocol, sync age, estimated latency, speed and health status
- Filter chips: by source (system / API), protocol (https / http), health (healthy / slow / dead)

**Live Tests**
- Real DNS, TCP and TLS probes against every mirror
- Live latency and speed chips updated per-mirror as probes complete
- Session-aware cancellation (Stop button mid-run)
- Apply Best Mirror: one-click write top probe result to `/etc/pacman.d/mirrorlist`

**Generate**
- Run `reflector` with configured flags and preview output mirrorlist
- Unified diff against current mirrorlist before applying
- Apply creates automatic timestamped backup (`.bak.YYYYMMDD-HHMMSS`)

**Restore**
- Roll back to any previous backup from a dropdown
- Sorted by date descending

**Settings**
- Full `reflector` flag surface: countries (picker UI), protocols, age, sort strategy, completion %, timeout, save count, IPv4/IPv6 flags
- Settings persisted to `~/.config/vsreflector-manager/settings.json`

**Chaotic AUR**
- Auto-detect install and config state (`chaotic-mirrorlist` package + `[chaotic-aur]` in `pacman.conf`)
- Not installed: shows official setup commands with one-click copy
- Installed, not configured: shows `pacman.conf` snippet with one-click copy
- Ready: all mirrors listed with enable/disable toggles
- Probe All Mirrors: DNS/TCP/TLS latency test per mirror, live chip updates
- Apply Changes: write toggle selection via pkexec
- Apply Best Mirror: activate only the fastest probe result

**About**
- App icon, version, author, GitHub and license links
