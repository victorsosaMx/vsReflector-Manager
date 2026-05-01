from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
import time as _time
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from typing import TYPE_CHECKING

from vs_reflector_manager.data import MirrorInfo, TestJob

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTfsu]|\x1b\(B|\x1b=|\x1b>")

if TYPE_CHECKING:
    from vs_reflector_manager.chaotic_services import ChaoticMirror

_COUNTRY_CODES: dict[str, str] = {
    "AU": "Australia", "AT": "Austria", "BE": "Belgium", "BR": "Brazil",
    "CA": "Canada", "CN": "China", "CZ": "Czech Republic", "DK": "Denmark",
    "FI": "Finland", "FR": "France", "DE": "Germany", "HK": "Hong Kong",
    "HU": "Hungary", "IN": "India", "IE": "Ireland", "IT": "Italy",
    "JP": "Japan", "KR": "South Korea", "MX": "Mexico", "NL": "Netherlands",
    "NZ": "New Zealand", "NO": "Norway", "PL": "Poland", "PT": "Portugal",
    "RO": "Romania", "SG": "Singapore", "ZA": "South Africa", "ES": "Spain",
    "SE": "Sweden", "CH": "Switzerland", "TW": "Taiwan", "GB": "United Kingdom",
    "US": "United States",
}
from vs_reflector_manager.services import (
    MIRRORLIST_PATH,
    GenerationOptions,
    GenerationResult,
    MirrorSource,
    RestoreResult,
    apply_mirrorlist,
    apply_pacnew,
    build_test_jobs,
    check_updates,
    delete_pacnew,
    fetch_arch_news,
    find_pacnew_files,
    generate_mirrorlist,
    get_orphan_packages,
    list_backups,
    load_mirrors,
    parse_current_mirrorlist,
    parse_pacman_log,
    remove_orphans,
    restore_mirrorlist,
    run_probe,
)


class StatCard(Gtk.Box):
    def __init__(self, title: str, value: str, detail: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("card")
        self.set_hexpand(True)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        self.title_label = Gtk.Label(label=title, xalign=0)
        self.title_label.add_css_class("dim-label")

        self.value_label = Gtk.Label(label=value, xalign=0)
        self.value_label.add_css_class("title-2")

        self.detail_label = Gtk.Label(label=detail, xalign=0, wrap=True)
        self.detail_label.add_css_class("caption")

        self.append(self.title_label)
        self.append(self.value_label)
        self.append(self.detail_label)

    def update(self, value: str, detail: str) -> None:
        self.value_label.set_text(value)
        self.detail_label.set_text(detail)


class MirrorRow(Adw.ActionRow):
    def __init__(self, mirror: MirrorInfo) -> None:
        super().__init__()
        self.set_title(mirror.name)
        self.set_subtitle(mirror.url)
        self.set_activatable(False)

        prefix = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        prefix.set_valign(Gtk.Align.CENTER)

        country = Gtk.Label(label=mirror.country, xalign=0)
        country.add_css_class("caption")
        protocol = Gtk.Label(label=mirror.protocol, xalign=0)
        protocol.add_css_class("accent")
        prefix.append(country)
        prefix.append(protocol)
        self.add_prefix(prefix)

        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        stats.set_valign(Gtk.Align.CENTER)
        for label in (
            mirror.sync_age,
            f"{mirror.latency_ms} ms",
            f"{mirror.speed_mbps} MB/s",
            mirror.status,
        ):
            chip = Gtk.Label(label=label)
            chip.add_css_class("pill")
            stats.append(chip)
        self.add_suffix(stats)


class TestRow(Gtk.Box):
    def __init__(self, job: TestJob) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.job = job
        self.add_css_class("card")
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        name = Gtk.Label(label=job.name, xalign=0)
        name.add_css_class("heading")
        name.set_hexpand(True)

        self.stage = Gtk.Label(label=job.stage, xalign=1)
        self.stage.add_css_class("dim-label")
        top.append(name)
        top.append(self.stage)

        self.progress = Gtk.ProgressBar()
        self.progress.set_fraction(job.progress)
        self.progress.set_show_text(True)
        self.progress.set_text(job.state)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.latency = Gtk.Label(xalign=0)
        self.speed = Gtk.Label(xalign=0)
        self.detail = Gtk.Label(xalign=0)
        self.detail.set_wrap(True)
        for label in (self.latency, self.speed, self.detail):
            label.add_css_class("caption")
            bottom.append(label)

        self.append(top)
        self.append(self.progress)
        self.append(bottom)
        self.apply_update()

    def update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self.job, key, value)
        self.apply_update()

    def apply_update(self) -> None:
        self.stage.set_text(self.job.stage)
        self.progress.set_fraction(self.job.progress)
        self.progress.set_text(self.job.state)
        self.latency.set_text(
            f"Latency: {self.job.latency_ms} ms" if self.job.latency_ms else "Latency: pending"
        )
        self.speed.set_text(
            f"Speed: {self.job.speed_mbps} MB/s" if self.job.speed_mbps else "Speed: pending"
        )
        self.detail.set_text(self.job.detail or "Waiting for probe")


class ChaoticMirrorRow(Adw.ActionRow):
    def __init__(self, mirror: "ChaoticMirror", on_toggled) -> None:
        super().__init__()
        self._mirror = mirror
        self.probe_latency_ms = 0
        self.probe_state = "idle"
        self.set_title(mirror.label or "Mirror")
        self.set_subtitle(mirror.url)
        self.set_activatable(False)

        self._latency_chip = Gtk.Label(label="—")
        self._latency_chip.add_css_class("pill")
        self._state_chip = Gtk.Label(label="idle")
        self._state_chip.add_css_class("pill")

        self._switch = Gtk.Switch()
        self._switch.set_active(mirror.active)
        self._switch.set_valign(Gtk.Align.CENTER)

        def _on_state_set(_sw, active, m=mirror):
            on_toggled(m, active)
            return False

        self._switch.connect("state-set", _on_state_set)

        suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        suffix.set_valign(Gtk.Align.CENTER)
        suffix.append(self._latency_chip)
        suffix.append(self._state_chip)
        suffix.append(self._switch)
        self.add_suffix(suffix)

    @property
    def mirror_url(self) -> str:
        return self._mirror.url

    def set_active(self, active: bool) -> None:
        self._mirror.active = active
        self._switch.set_active(active)

    def set_probe_result(self, state: str, latency_ms: int = 0) -> None:
        self.probe_state = state
        self.probe_latency_ms = latency_ms
        self._state_chip.set_label(state)
        self._latency_chip.set_label(f"{latency_ms} ms" if latency_ms else "—")


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("vsReflector Manager")
        self.set_default_size(1260, 820)

        from vs_reflector_manager.data import DEFAULT_MIRRORS
        from vs_reflector_manager.settings_store import load_settings
        self._loaded_settings = load_settings()

        self.mirror_source: MirrorSource = MirrorSource(
            mirrors=list(DEFAULT_MIRRORS),
            source_name="Loading…",
            command="",
            generated_at="",
            retrieved_at="",
        )
        self.mirror_rows: list[MirrorRow] = []
        self.test_rows: list[TestRow] = []
        self.generated_result: GenerationResult | None = None
        self.running_probes = 0
        self._probe_session = 0

        split = Adw.NavigationSplitView()
        split.set_min_sidebar_width(240)
        split.set_max_sidebar_width(280)
        split.set_sidebar(Adw.NavigationPage.new(self._build_sidebar(), "Navigation"))
        split.set_content(Adw.NavigationPage.new(self._build_content(), "Content"))

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="vsReflector Manager"))

        self._reload_button = Gtk.Button(label="Reload Mirrors")
        self._reload_button.connect("clicked", self._reload_mirrors)
        self._reload_spinner = Gtk.Spinner()
        self._reload_spinner.set_visible(False)
        reload_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        reload_box.append(self._reload_button)
        reload_box.append(self._reload_spinner)
        header.pack_start(reload_box)

        tests_button = Gtk.Button(label="Run Live Tests")
        tests_button.add_css_class("suggested-action")
        tests_button.connect("clicked", self._run_live_tests)
        header.pack_end(tests_button)

        toolbar.add_top_bar(header)
        toolbar.set_content(split)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(toolbar)
        self.set_content(self._toast_overlay)

        self._load_css()
        self._select_page("dashboard")
        self._refresh_views()
        self.connect("close-request", self._on_close_request)
        GLib.idle_add(self._reload_mirrors, None)
        threading.Thread(target=self._bg_check_updates, daemon=True).start()
        threading.Thread(target=self._bg_fetch_news, daemon=True).start()

    def _show_toast(self, message: str, timeout: int = 3) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)

    # ── Background tasks ──────────────────────────────────────────────────────

    def _bg_check_updates(self) -> None:
        count = check_updates()
        GLib.idle_add(self._set_update_badge, count)

    def _set_update_badge(self, count: int) -> bool:
        if count > 0:
            self._update_badge.set_text(str(count))
            self._update_badge.set_visible(True)
        else:
            self._update_badge.set_visible(False)
        return False

    def _bg_fetch_news(self) -> None:
        articles = fetch_arch_news()
        GLib.idle_add(self._set_news, articles)

    def _set_news(self, articles: list[dict]) -> bool:
        if not articles or not hasattr(self, "_news_inner"):
            return False
        child = self._news_inner.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._news_inner.remove(child)
            child = nxt
        grp = Adw.PreferencesGroup(
            title="Arch Linux News",
            description="Review before running system updates",
        )
        for art in articles:
            row = Adw.ActionRow(title=art["title"])
            if art.get("pubdate"):
                row.set_subtitle(art["pubdate"])
            grp.add(row)
        self._news_inner.append(grp)
        self._news_revealer.set_reveal_child(True)
        return False

    def _on_close_request(self, _window: "MainWindow") -> bool:
        self._save_current_settings()
        return False

    def _save_current_settings(self) -> None:
        from vs_reflector_manager.settings_store import AppSettings, save_settings
        save_settings(AppSettings(
            countries=self.countries_entry.get_text().strip(),
            protocols=self.protocols_entry.get_text().strip() or "https",
            latest=self.latest_spin.get_value_as_int(),
            number=self.number_spin.get_value_as_int(),
            age=self.age_spin.get_value(),
            sort_by=self.sort_combo.get_active_text() or "rate",
            completion_percent=self.completion_spin.get_value_as_int(),
            timeout_seconds=self.timeout_spin.get_value_as_int(),
            use_ipv4=self.ipv4_switch.get_active(),
            use_ipv6=self.ipv6_switch.get_active(),
            include_isos=self.isos_switch.get_active(),
            live_test_limit=self.test_limit_spin.get_value_as_int(),
        ))

    def _apply_loaded_settings(self) -> None:
        s = self._loaded_settings
        self.countries_entry.set_text(s.countries)
        self.protocols_entry.set_text(s.protocols)
        self.latest_spin.set_value(s.latest)
        self.number_spin.set_value(s.number)
        self.age_spin.set_value(s.age)
        sort_opts = ["rate", "age", "score", "delay", "country"]
        idx = sort_opts.index(s.sort_by) if s.sort_by in sort_opts else 0
        self.sort_combo.set_active(idx)
        self.completion_spin.set_value(s.completion_percent)
        self.timeout_spin.set_value(s.timeout_seconds)
        self.ipv4_switch.set_active(s.use_ipv4)
        self.ipv6_switch.set_active(s.use_ipv6)
        self.isos_switch.set_active(s.include_isos)
        self.test_limit_spin.set_value(s.live_test_limit)

    def _show_country_picker(self, _button: Gtk.Button) -> None:
        current = {c.strip().upper() for c in self.countries_entry.get_text().split(",") if c.strip()}

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Select Countries",
            body="Mirrors will be filtered to the selected countries. Leave all unchecked for worldwide.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(320)
        scroll.set_min_content_width(320)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")

        checks: dict[str, Gtk.CheckButton] = {}
        for code, name in sorted(_COUNTRY_CODES.items(), key=lambda x: x[1]):
            item_row = Adw.ActionRow(title=name, subtitle=code)
            check = Gtk.CheckButton()
            check.set_active(code in current)
            check.set_valign(Gtk.Align.CENTER)
            item_row.add_suffix(check)
            item_row.set_activatable_widget(check)
            list_box.append(item_row)
            checks[code] = check

        scroll.set_child(list_box)
        dialog.set_extra_child(scroll)

        def on_response(_dialog: Adw.MessageDialog, response: str) -> None:
            if response == "apply":
                selected = sorted(code for code, cb in checks.items() if cb.get_active())
                self.countries_entry.set_text(",".join(selected))
            _dialog.close()

        dialog.connect("response", on_response)
        dialog.present()

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            .card {
                padding: 16px;
                border-radius: 18px;
                background: alpha(@card_bg_color, 0.92);
            }
            textview.code-view > text {
                background: alpha(@card_bg_color, 0.92);
                color: @window_fg_color;
            }
            .pill {
                padding: 6px 10px;
                border-radius: 999px;
                background: alpha(@accent_bg_color, 0.16);
            }
            .accent {
                color: @accent_color;
                font-weight: 700;
            }
            .warning-card {
                background: alpha(@warning_bg_color, 0.14);
            }
            textview.terminal-view > text {
                background: #0d0f0b;
                color: #c8d8b8;
                caret-color: #c8d8b8;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _build_sidebar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label="Mirror Center", xalign=0)
        title.add_css_class("title-3")
        subtitle = Gtk.Label(label="Reflector visual manager", xalign=0)
        subtitle.add_css_class("dim-label")

        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav.add_css_class("navigation-sidebar")
        self.nav.connect("row-selected", self._on_nav_selected)

        pages = [
            ("dashboard", "Overview", "go-home-symbolic"),
            ("mirrors", "Mirrors", "network-server-symbolic"),
            ("tests", "Live Tests", "media-playback-start-symbolic"),
            ("history", "Generate", "document-edit-symbolic"),
            ("settings", "Settings", "preferences-system-symbolic"),
            ("pacman", "Pacman", "preferences-other-symbolic"),
            ("chaotic", "Chaotic AUR", "package-x-generic-symbolic"),
            ("update", "Update", "software-update-available-symbolic"),
            ("log", "Pacman Log", "document-open-recent-symbolic"),
            ("pacnew", "pacnew Files", "dialog-warning-symbolic"),
            ("about", "About", "help-about-symbolic"),
        ]
        for page_id, label, icon_name in pages:
            row = Gtk.ListBoxRow()
            row.page_id = page_id
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_top(6)
            row_box.set_margin_bottom(6)
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(16)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_hexpand(True)
            row_box.append(icon)
            row_box.append(lbl)
            if page_id == "update":
                self._update_badge = Gtk.Label()
                self._update_badge.add_css_class("pill")
                self._update_badge.add_css_class("accent")
                self._update_badge.set_visible(False)
                row_box.append(self._update_badge)
            row.set_child(row_box)
            self.nav.append(row)

        box.append(title)
        box.append(subtitle)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        box.append(self.nav)
        return box

    def _build_content(self) -> Gtk.Widget:
        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self.stack.add_named(self._build_dashboard(), "dashboard")
        self.stack.add_named(self._build_mirrors(), "mirrors")
        self.stack.add_named(self._build_tests(), "tests")
        self.stack.add_named(self._build_settings(), "settings")
        self.stack.add_named(self._build_history(), "history")
        self.stack.add_named(self._build_pacman(), "pacman")
        self.stack.add_named(self._build_chaotic(), "chaotic")
        self.stack.add_named(self._build_update(), "update")
        self.stack.add_named(self._build_log(), "log")
        self.stack.add_named(self._build_pacnew(), "pacnew")
        self.stack.add_named(self._build_about(), "about")

        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=820)
        clamp.set_child(self.stack)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(clamp)
        return scroller

    def _build_dashboard(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        box.append(
            Adw.WindowTitle(
                title="Overview",
                subtitle="Mirror source, readiness and test health",
            )
        )

        self.source_banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.source_banner.add_css_class("card")
        self.source_label = Gtk.Label(xalign=0, wrap=True)
        self.source_label.add_css_class("heading")
        self.warning_label = Gtk.Label(xalign=0, wrap=True)
        self.warning_label.add_css_class("caption")
        self.source_banner.append(self.source_label)
        self.source_banner.append(self.warning_label)
        box.append(self.source_banner)

        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        self.card_total = StatCard("Available", "0 mirrors", "Loaded from source")
        self.card_generated = StatCard("Generated", "Unknown", "Mirrorlist timestamp")
        self.card_latency = StatCard("Median Latency", "0 ms", "Estimated from the loaded list")
        self.card_best = StatCard("Best Candidate", "-", "Lowest estimated latency")

        for index, card in enumerate(
            (self.card_total, self.card_generated, self.card_latency, self.card_best)
        ):
            grid.attach(card, index % 2, index // 2, 1, 1)

        box.append(grid)
        return box

    def _build_mirrors(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        box.append(
            Adw.WindowTitle(
                title="Mirrors",
                subtitle="Current system mirrorlist with visual health hints",
            )
        )

        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._filter_chips: dict[str, Gtk.ToggleButton] = {}
        for label, key in (
            ("System only", "system"),
            ("HTTPS", "https"),
            ("Healthy+", "healthy"),
        ):
            chip = Gtk.ToggleButton(label=label)
            chip.set_active(False)
            chip.connect("toggled", self._on_filter_toggled)
            self._filter_chips[key] = chip
            filter_row.append(chip)
        box.append(filter_row)

        self.mirror_group = Adw.PreferencesGroup(title="Loaded Mirrors")
        self._mirrors_empty = Gtk.Label(label="No mirrors match the active filters.")
        self._mirrors_empty.add_css_class("dim-label")
        self._mirrors_empty.set_margin_top(24)
        self._mirrors_empty.set_visible(False)
        box.append(self.mirror_group)
        box.append(self._mirrors_empty)
        return box

    def _build_tests(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        box.append(
            Adw.WindowTitle(
                title="Live Tests",
                subtitle="Real DNS, TCP and TLS probes against the loaded mirrors",
            )
        )

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._run_btn = Gtk.Button(label="Run Tests")
        self._run_btn.add_css_class("suggested-action")
        self._run_btn.connect("clicked", self._run_live_tests)
        self._stop_btn = Gtk.Button(label="Stop")
        self._stop_btn.add_css_class("destructive-action")
        self._stop_btn.set_sensitive(False)
        self._stop_btn.connect("clicked", self._cancel_live_tests)
        self._apply_best_btn = Gtk.Button(label="Apply Best Mirror")
        self._apply_best_btn.set_sensitive(False)
        self._apply_best_btn.connect("clicked", self._apply_best_probe)
        actions.append(self._run_btn)
        actions.append(self._stop_btn)
        actions.append(self._apply_best_btn)
        box.append(actions)

        top_stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.card_jobs = StatCard("Active Jobs", "0", "Live probe threads currently running")
        self.card_probe_best = StatCard("Best Probe", "-", "Updates when a probe completes")
        self.card_probe_status = StatCard("Session", "Idle", "Run tests to collect fresh timings")
        for card in (self.card_jobs, self.card_probe_best, self.card_probe_status):
            top_stats.append(card)
        box.append(top_stats)

        self.tests_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.tests_list)
        box.append(scroll)
        return box

    def _build_history(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        box.append(
            Adw.WindowTitle(
                title="Generate",
                subtitle="Generate a new mirrorlist, inspect the diff and apply with backup",
            )
        )

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._generate_button = Gtk.Button(label="Generate Preview")
        self._generate_button.add_css_class("suggested-action")
        self._generate_button.connect("clicked", self._generate_preview)
        self._generate_spinner = Gtk.Spinner()
        self._generate_spinner.set_visible(False)
        self._generate_spinner.set_valign(Gtk.Align.CENTER)
        self._apply_button = Gtk.Button(label="Apply Mirrorlist")
        self._apply_button.connect("clicked", self._apply_generated_mirrorlist)
        self._apply_button.set_sensitive(False)
        restore_button = Gtk.Button(label="Restore from Backup")
        restore_button.connect("clicked", self._show_restore_dialog)
        actions.append(self._generate_button)
        actions.append(self._generate_spinner)
        actions.append(self._apply_button)
        actions.append(restore_button)
        box.append(actions)

        self._comparison_revealer = Gtk.Revealer()
        self._comparison_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._comparison_revealer.set_transition_duration(300)
        self._comparison_revealer.set_reveal_child(False)
        self._comparison_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self._comparison_inner.set_margin_top(4)
        self._comparison_inner.set_margin_bottom(4)
        self._comparison_revealer.set_child(self._comparison_inner)
        box.append(self._comparison_revealer)

        self.command_row = Adw.ActionRow(title="Generated With")
        self.generated_row = Adw.ActionRow(title="Generated At")
        self.retrieved_row = Adw.ActionRow(title="Retrieved At")
        self.preview_status_row = Adw.ActionRow(title="Preview Status", subtitle="No preview generated yet")
        self.preview_detail_row = Adw.ActionRow(title="Details", subtitle="No command has been executed yet")
        self.preview_detail_row.set_subtitle_selectable(True)
        metadata = Adw.PreferencesGroup(title="Metadata")
        metadata.add(self.command_row)
        metadata.add(self.generated_row)
        metadata.add(self.retrieved_row)
        metadata.add(self.preview_status_row)
        metadata.add(self.preview_detail_row)
        box.append(metadata)

        command_title = Gtk.Label(label="Command", xalign=0)
        command_title.add_css_class("heading")
        box.append(command_title)
        command_frame = Gtk.Frame()
        self.command_buffer = Gtk.TextBuffer()
        command_view = Gtk.TextView(buffer=self.command_buffer)
        command_view.set_editable(False)
        command_view.set_monospace(True)
        command_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        command_view.set_vexpand(False)
        command_view.add_css_class("code-view")
        command_scroll = Gtk.ScrolledWindow()
        command_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        command_scroll.set_min_content_height(56)
        command_scroll.set_child(command_view)
        command_frame.set_child(command_scroll)
        box.append(command_frame)

        generated_title = Gtk.Label(label="Generated Mirrorlist", xalign=0)
        generated_title.add_css_class("heading")
        box.append(generated_title)
        generated_frame = Gtk.Frame()
        self.generated_buffer = Gtk.TextBuffer()
        generated_view = Gtk.TextView(buffer=self.generated_buffer)
        generated_view.set_editable(False)
        generated_view.set_monospace(True)
        generated_view.set_wrap_mode(Gtk.WrapMode.NONE)
        generated_view.add_css_class("code-view")
        generated_scroll = Gtk.ScrolledWindow()
        generated_scroll.set_min_content_height(220)
        generated_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        generated_scroll.set_child(generated_view)
        generated_frame.set_child(generated_scroll)
        box.append(generated_frame)

        diff_title = Gtk.Label(label="Diff / Errors", xalign=0)
        diff_title.add_css_class("heading")
        box.append(diff_title)
        diff_frame = Gtk.Frame()
        self.diff_buffer = Gtk.TextBuffer()
        diff_view = Gtk.TextView(buffer=self.diff_buffer)
        diff_view.set_editable(False)
        diff_view.set_monospace(True)
        diff_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        diff_view.add_css_class("code-view")
        diff_scroll = Gtk.ScrolledWindow()
        diff_scroll.set_min_content_height(220)
        diff_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        diff_scroll.set_child(diff_view)
        diff_frame.set_child(diff_scroll)
        box.append(diff_frame)
        return box

    def _build_settings(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.set_margin_top(20)
        page.set_margin_bottom(20)
        page.set_margin_start(20)
        page.set_margin_end(20)

        page.append(
            Adw.WindowTitle(
                title="Settings",
                subtitle="Reflector filters used to generate the next mirrorlist",
            )
        )

        group = Adw.PreferencesGroup(title="Reflector Filters")

        self.countries_entry = Adw.EntryRow(title="Countries")
        self.countries_entry.set_text("MX,US,CA")
        picker_btn = Gtk.Button(icon_name="view-list-symbolic")
        picker_btn.add_css_class("flat")
        picker_btn.set_tooltip_text("Pick countries")
        picker_btn.set_valign(Gtk.Align.CENTER)
        picker_btn.connect("clicked", self._show_country_picker)
        self.countries_entry.add_suffix(picker_btn)
        group.add(self.countries_entry)

        self.protocols_entry = Adw.EntryRow(title="Protocols")
        self.protocols_entry.set_text("https")
        group.add(self.protocols_entry)

        latest_row = Adw.ActionRow(title="Latest Mirrors", subtitle="Limit by most recently synchronized")
        self.latest_spin = Gtk.SpinButton.new_with_range(1, 100, 1)
        self.latest_spin.set_value(20)
        latest_row.add_suffix(self.latest_spin)
        latest_row.set_activatable_widget(self.latest_spin)
        group.add(latest_row)

        number_row = Adw.ActionRow(title="Max Returned Mirrors", subtitle="How many lines go into the output")
        self.number_spin = Gtk.SpinButton.new_with_range(1, 50, 1)
        self.number_spin.set_value(10)
        number_row.add_suffix(self.number_spin)
        number_row.set_activatable_widget(self.number_spin)
        group.add(number_row)

        age_row = Adw.ActionRow(title="Max Age (hours)", subtitle="Only include mirrors synchronized recently")
        self.age_spin = Gtk.SpinButton.new_with_range(0, 72, 0.5)
        self.age_spin.set_value(12)
        age_row.add_suffix(self.age_spin)
        age_row.set_activatable_widget(self.age_spin)
        group.add(age_row)

        sort_row = Adw.ActionRow(title="Sort By", subtitle="Reflector sorting strategy")
        self.sort_combo = Gtk.ComboBoxText()
        for value in ("rate", "age", "score", "delay", "country"):
            self.sort_combo.append_text(value)
        self.sort_combo.set_active(0)
        sort_row.add_suffix(self.sort_combo)
        sort_row.set_activatable_widget(self.sort_combo)
        group.add(sort_row)

        completion_row = Adw.ActionRow(title="Completion Percent", subtitle="Default is 100")
        self.completion_spin = Gtk.SpinButton.new_with_range(0, 100, 1)
        self.completion_spin.set_value(100)
        completion_row.add_suffix(self.completion_spin)
        completion_row.set_activatable_widget(self.completion_spin)
        group.add(completion_row)

        timeout_row = Adw.ActionRow(title="Command Timeout", subtitle="Seconds to wait for reflector")
        self.timeout_spin = Gtk.SpinButton.new_with_range(5, 180, 5)
        self.timeout_spin.set_value(60)
        timeout_row.add_suffix(self.timeout_spin)
        timeout_row.set_activatable_widget(self.timeout_spin)
        group.add(timeout_row)

        test_limit_row = Adw.ActionRow(
            title="Live Test Mirrors",
            subtitle="How many mirrors to probe in the Live Tests tab",
        )
        self.test_limit_spin = Gtk.SpinButton.new_with_range(1, 20, 1)
        self.test_limit_spin.set_value(5)
        test_limit_row.add_suffix(self.test_limit_spin)
        test_limit_row.set_activatable_widget(self.test_limit_spin)
        group.add(test_limit_row)

        page.append(group)

        flags = Adw.PreferencesGroup(title="Additional Flags")
        self.ipv4_switch = Gtk.Switch()
        ipv4_row = Adw.ActionRow(title="IPv4 Only", subtitle="Append --ipv4")
        ipv4_row.add_suffix(self.ipv4_switch)
        ipv4_row.set_activatable_widget(self.ipv4_switch)
        flags.add(ipv4_row)

        self.ipv6_switch = Gtk.Switch()
        ipv6_row = Adw.ActionRow(title="IPv6 Only", subtitle="Append --ipv6")
        ipv6_row.add_suffix(self.ipv6_switch)
        ipv6_row.set_activatable_widget(self.ipv6_switch)
        flags.add(ipv6_row)

        self.isos_switch = Gtk.Switch()
        isos_row = Adw.ActionRow(title="Require ISOs", subtitle="Append --isos")
        isos_row.add_suffix(self.isos_switch)
        isos_row.set_activatable_widget(self.isos_switch)
        flags.add(isos_row)

        page.append(flags)
        self._apply_loaded_settings()
        return page

    def _on_nav_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is not None:
            self._select_page(row.page_id)

    def _select_page(self, page_id: str) -> None:
        self.stack.set_visible_child_name(page_id)
        row = self.nav.get_first_child()
        while row is not None:
            if getattr(row, "page_id", None) == page_id:
                if self.nav.get_selected_row() is not row:
                    self.nav.select_row(row)
                break
            row = row.get_next_sibling()
        if page_id == "log" and not getattr(self, "_log_loaded", False):
            self._log_loaded = True
            self._log_load_fn()

    def _refresh_views(self) -> None:
        self._refresh_dashboard()
        self._refresh_mirror_rows()
        self._refresh_tests()
        self._refresh_mirrorlist_preview()

    def _refresh_dashboard(self) -> None:
        count = len(self.mirror_source.mirrors)
        latencies = [mirror.latency_ms for mirror in self.mirror_source.mirrors]
        median = sorted(latencies)[len(latencies) // 2] if latencies else 0
        best = min(self.mirror_source.mirrors, key=lambda item: item.latency_ms, default=None)

        self.source_label.set_text(
            f"Source: {self.mirror_source.source_name} with {count} loaded mirrors"
        )
        self.warning_label.set_text(
            self.mirror_source.warning or "Reflector status is healthy or not required for preview."
        )
        if self.mirror_source.warning:
            self.source_banner.add_css_class("warning-card")
        else:
            self.source_banner.remove_css_class("warning-card")

        self.card_total.update(f"{count} mirrors", self.mirror_source.command)
        self.card_generated.update(self.mirror_source.generated_at, self.mirror_source.retrieved_at)
        self.card_latency.update(f"{median} ms", "Synthetic estimate from the loaded mirrorlist")
        if best is not None:
            self.card_best.update(best.name, f"{best.latency_ms} ms estimated latency")
        else:
            self.card_best.update("-", "No mirrors loaded")

    def _on_filter_toggled(self, _button: Gtk.ToggleButton) -> None:
        self._refresh_mirror_rows()

    def _refresh_mirror_rows(self) -> None:
        for row in self.mirror_rows:
            self.mirror_group.remove(row)
        self.mirror_rows.clear()

        total = len(self.mirror_source.mirrors)
        mirrors = list(self.mirror_source.mirrors)
        chips = getattr(self, "_filter_chips", {})
        if chips.get("system") and chips["system"].get_active():
            mirrors = [m for m in mirrors if m.source == "system"]
        if chips.get("https") and chips["https"].get_active():
            mirrors = [m for m in mirrors if m.protocol == "HTTPS"]
        if chips.get("healthy") and chips["healthy"].get_active():
            mirrors = [m for m in mirrors if m.status in ("Excellent", "Healthy")]

        filtered = len(mirrors) < total
        title = f"Loaded Mirrors — {len(mirrors)} / {total}" if filtered else f"Loaded Mirrors — {total}"
        self.mirror_group.set_title(title)

        for mirror in mirrors:
            row = MirrorRow(mirror)
            self.mirror_group.add(row)
            self.mirror_rows.append(row)

        empty = len(mirrors) == 0
        self._mirrors_empty.set_visible(empty)

    def _refresh_tests(self) -> None:
        clear_children(self.tests_list)
        self.test_rows.clear()
        limit = self.test_limit_spin.get_value_as_int() if hasattr(self, "test_limit_spin") else 5
        for job in build_test_jobs(self.mirror_source.mirrors, limit=limit):
            row = TestRow(job)
            self.test_rows.append(row)
            self.tests_list.append(row)
        self.card_jobs.update("0", "Live probe threads currently running")
        self.card_probe_best.update("-", "No successful probes yet")
        self.card_probe_status.update("Idle", "Run tests to collect fresh timings")

    def _refresh_mirrorlist_preview(self) -> None:
        self.command_row.set_subtitle(self.mirror_source.command)
        self.generated_row.set_subtitle(self.mirror_source.generated_at)
        self.retrieved_row.set_subtitle(self.mirror_source.retrieved_at)
        try:
            with open(MIRRORLIST_PATH, encoding="utf-8") as handle:
                current_text = handle.read()
        except OSError as err:
            current_text = f"Failed to read {MIRRORLIST_PATH}: {err}"
        self.generated_buffer.set_text(current_text)
        self.diff_buffer.set_text("Generate a preview to see the diff.")
        self.command_buffer.set_text(self.mirror_source.command)
        self.preview_status_row.set_subtitle("No preview generated yet")
        self.preview_detail_row.set_subtitle("No command has been executed yet")

    def _reload_mirrors(self, _button: Gtk.Button) -> None:
        self._reload_button.set_sensitive(False)
        self._reload_spinner.set_visible(True)
        self._reload_spinner.start()
        thread = threading.Thread(target=self._reload_mirrors_thread, daemon=True)
        thread.start()

    def _reload_mirrors_thread(self) -> None:
        source = load_mirrors()
        GLib.idle_add(self._reload_mirrors_done, source)

    def _reload_mirrors_done(self, source: MirrorSource) -> bool:
        self.mirror_source = source
        self._reload_button.set_sensitive(True)
        self._reload_spinner.stop()
        self._reload_spinner.set_visible(False)
        self._refresh_views()
        count = len(source.mirrors)
        self._show_toast(f"Loaded {count} mirrors from {source.source_name}.")
        return False

    def _run_live_tests(self, _button: Gtk.Button) -> None:
        self._select_page("tests")
        self._probe_session += 1
        session = self._probe_session
        self._run_btn.set_sensitive(False)
        self._stop_btn.set_sensitive(True)
        self._apply_best_btn.set_sensitive(False)
        self._refresh_tests()

        self.running_probes = len(self.test_rows)
        self.card_jobs.update(str(self.running_probes), "Live probe threads currently running")
        self.card_probe_status.update("Running", "DNS/TCP/TLS probes in progress")

        for row in self.test_rows:
            row.update(
                state="Queued",
                progress=0.0,
                latency_ms=0,
                speed_mbps=0,
                stage="Waiting",
                detail="Queued for probe",
            )
            thread = threading.Thread(target=self._probe_row, args=(row, session), daemon=True)
            thread.start()

    def _cancel_live_tests(self, _button: Gtk.Button) -> None:
        self._probe_session += 1
        self.running_probes = 0
        self.card_jobs.update("0", "Tests canceled")
        self.card_probe_status.update("Canceled", "Tests were stopped early")
        self._run_btn.set_sensitive(True)
        self._stop_btn.set_sensitive(False)
        for row in self.test_rows:
            if row.job.state not in {"Complete", "Failed"}:
                row.update(state="Canceled", stage="Stopped", progress=0.0, detail="Canceled by user")

    def _probe_row(self, row: TestRow, session: int) -> None:
        def on_update(**kwargs) -> None:
            GLib.idle_add(self._apply_probe_update, row, kwargs, session)

        run_probe(row.job.url, on_update)

    def _apply_probe_update(self, row: TestRow, update: dict, session: int) -> bool:
        if session != self._probe_session:
            return False

        previous_state = row.job.state
        row.update(**update)

        if row.job.state in {"Complete", "Failed"} and previous_state not in {"Complete", "Failed"}:
            self.running_probes = max(0, self.running_probes - 1)
            self.card_jobs.update(str(self.running_probes), "Live probe threads currently running")
            if row.job.state == "Complete":
                current_best = self._best_completed_probe()
                if current_best is not None:
                    self.card_probe_best.update(
                        current_best.job.name,
                        f"{current_best.job.latency_ms} ms, {current_best.job.speed_mbps} MB/s",
                    )
            if self.running_probes == 0:
                self.card_probe_status.update("Finished", "All scheduled probes completed")
                self._run_btn.set_sensitive(True)
                self._stop_btn.set_sensitive(False)
                best = self._best_completed_probe()
                if best is not None:
                    self._apply_best_btn.set_sensitive(True)
                    self._update_dashboard_from_probes()

        return False

    def _update_dashboard_from_probes(self) -> None:
        completed = [r for r in self.test_rows if r.job.state == "Complete"]
        if not completed:
            return
        latencies = sorted(r.job.latency_ms for r in completed)
        median = latencies[len(latencies) // 2]
        best = min(completed, key=lambda r: r.job.latency_ms)
        self.card_latency.update(f"{median} ms", f"Real probe median ({len(completed)} mirrors)")
        self.card_best.update(best.job.name, f"{best.job.latency_ms} ms real probe latency")

    def _best_completed_probe(self) -> TestRow | None:
        completed = [row for row in self.test_rows if row.job.state == "Complete"]
        if not completed:
            return None
        return min(completed, key=lambda row: row.job.latency_ms)

    def _apply_best_probe(self, _button: Gtk.Button) -> None:
        top = sorted(
            [r for r in self.test_rows if r.job.state == "Complete"],
            key=lambda r: r.job.latency_ms,
        )[:3]
        if not top:
            self._show_toast("No completed probes to apply.")
            return

        lines = ["# Generated by vsReflector Manager — best from live probe"]
        for row in top:
            lines.append(f"Server = {row.job.url}")
        mirrorlist_text = "\n".join(lines) + "\n"

        best = top[0]
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Apply Best Mirror?",
            body=(
                f"Apply top {len(top)} probe result(s) as mirrorlist.\n"
                f"Best: {best.job.name} — {best.job.latency_ms} ms\n\n"
                "Requires admin privileges. Current mirrorlist will be backed up."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def on_response(_dialog: Adw.MessageDialog, response: str) -> None:
            if response != "apply":
                return

            def do_apply() -> None:
                result = apply_mirrorlist(mirrorlist_text)
                GLib.idle_add(_on_done, result)

            def _on_done(result: "ApplyResult") -> bool:
                if result.success:
                    self._show_toast(f"Applied {best.job.name} ({best.job.latency_ms} ms). Backup saved.")
                    self._reload_mirrors(None)
                else:
                    self._show_toast(result.message.splitlines()[0], timeout=6)
                return False

            threading.Thread(target=do_apply, daemon=True).start()

        dialog.connect("response", on_response)
        dialog.present()

    def _collect_generation_options(self) -> GenerationOptions:
        return GenerationOptions(
            countries=self.countries_entry.get_text().strip(),
            protocols=self.protocols_entry.get_text().strip() or "https",
            latest=self.latest_spin.get_value_as_int(),
            number=self.number_spin.get_value_as_int(),
            age=self.age_spin.get_value(),
            sort_by=self.sort_combo.get_active_text() or "rate",
            completion_percent=self.completion_spin.get_value_as_int(),
            timeout_seconds=self.timeout_spin.get_value_as_int(),
            use_ipv4=self.ipv4_switch.get_active(),
            use_ipv6=self.ipv6_switch.get_active(),
            include_isos=self.isos_switch.get_active(),
        )

    def _populate_comparison(self, preview_text: str) -> None:
        def do_read() -> None:
            current_mirrors, _ = parse_current_mirrorlist()
            GLib.idle_add(self._populate_comparison_ui, preview_text, current_mirrors)

        threading.Thread(target=do_read, daemon=True).start()

    def _populate_comparison_ui(self, preview_text: str, current_mirrors: list) -> bool:
        def _host(url: str) -> str:
            return urlparse(url).hostname or url
        current_urls = [m.url for m in current_mirrors]
        preview_urls = re.findall(r"^Server\s*=\s*(.+)", preview_text, re.MULTILINE)

        current_hosts = {_host(u) for u in current_urls}
        preview_hosts_list = [_host(u) for u in preview_urls]
        preview_hosts = set(preview_hosts_list)

        new_hosts = preview_hosts - current_hosts
        removed_hosts = current_hosts - preview_hosts
        common_hosts = current_hosts & preview_hosts

        current_https = sum(1 for u in current_urls if u.startswith("https"))
        preview_https = sum(1 for u in preview_urls if u.startswith("https"))
        cur_https_pct = round(100 * current_https / len(current_urls)) if current_urls else 0
        prev_https_pct = round(100 * preview_https / len(preview_urls)) if preview_urls else 0

        mirror_by_host: dict[str, object] = {_host(m.url): m for m in self.mirror_source.mirrors}

        cur_countries = {mirror_by_host[h].country for h in current_hosts if h in mirror_by_host}  # type: ignore[attr-defined]
        prev_countries = {mirror_by_host[h].country for h in preview_hosts if h in mirror_by_host}  # type: ignore[attr-defined]

        def _avg_lat(hosts: set) -> int:
            lats = [mirror_by_host[h].latency_ms for h in hosts if h in mirror_by_host and mirror_by_host[h].latency_ms > 0]  # type: ignore[attr-defined]
            return sum(lats) // len(lats) if lats else 0

        cur_lat = _avg_lat(current_hosts)
        prev_lat = _avg_lat(preview_hosts)

        score = 0
        n_cur, n_prev = len(current_urls), len(preview_urls)
        if n_prev > n_cur:
            score += 2
        elif n_prev == n_cur:
            score += 1
        if prev_https_pct >= cur_https_pct:
            score += 1
        if cur_lat > 0 and prev_lat < cur_lat:
            score += 1
        if len(prev_countries) >= len(cur_countries):
            score += 1

        if score >= 4:
            verdict, verdict_sub, verdict_icon = (
                "Recommended to apply",
                f"{len(new_hosts)} new mirror{'s' if len(new_hosts) != 1 else ''}, improved coverage",
                "emblem-default-symbolic",
            )
        elif score >= 2:
            verdict, verdict_sub, verdict_icon = (
                "Marginal improvement",
                "Similar quality to current list",
                "dialog-information-symbolic",
            )
        else:
            verdict, verdict_sub, verdict_icon = (
                "Consider keeping current",
                "Preview list may not improve on current",
                "dialog-warning-symbolic",
            )

        child = self._comparison_inner.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._comparison_inner.remove(child)
            child = nxt

        # ── Verdict banner ────────────────────────────────────────────
        banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        banner.add_css_class("card")
        icon = Gtk.Image.new_from_icon_name(verdict_icon)
        icon.set_pixel_size(32)
        icon.set_valign(Gtk.Align.CENTER)
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        txt.set_hexpand(True)
        txt.set_valign(Gtk.Align.CENTER)
        hl = Gtk.Label(label=verdict, xalign=0)
        hl.add_css_class("title-3")
        sl = Gtk.Label(label=verdict_sub, xalign=0)
        sl.add_css_class("dim-label")
        txt.append(hl)
        txt.append(sl)
        banner.append(icon)
        banner.append(txt)
        self._comparison_inner.append(banner)

        # ── Stat cards ────────────────────────────────────────────────
        delta = n_prev - n_cur
        delta_str = (f"+{delta}" if delta > 0 else str(delta)) if delta != 0 else "no change"
        lat_detail = (
            f"{'−' if prev_lat < cur_lat else '+'}{abs(prev_lat - cur_lat)} ms vs current"
            if cur_lat and prev_lat
            else (f"avg {prev_lat} ms" if prev_lat else "no data")
        )
        cards = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        for title, value, detail in (
            ("Mirrors",     f"{n_cur} → {n_prev}",                    delta_str),
            ("HTTPS",       f"{cur_https_pct}% → {prev_https_pct}%",  "protocol coverage"),
            ("Countries",   f"{len(cur_countries)} → {len(prev_countries)}", "unique countries"),
            ("Avg Latency", f"{prev_lat} ms" if prev_lat else "—",    lat_detail),
        ):
            cards.append(StatCard(title, value, detail))
        self._comparison_inner.append(cards)

        # ── Changes summary ───────────────────────────────────────────
        chg_grp = Adw.PreferencesGroup(title="Changes")
        for label, count, icon_name in (
            ("New mirrors",     len(new_hosts),     "list-add-symbolic"),
            ("Removed mirrors", len(removed_hosts), "list-remove-symbolic"),
            ("Common mirrors",  len(common_hosts),  "emblem-shared-symbolic"),
        ):
            row = Adw.ActionRow(title=label, subtitle=str(count))
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(16)
            row.add_prefix(img)
            chg_grp.add(row)
        self._comparison_inner.append(chg_grp)

        # ── Top preview mirrors ───────────────────────────────────────
        top_grp = Adw.PreferencesGroup(title=f"Preview Mirrorlist  ({n_prev} mirrors)")
        for url in preview_urls[:6]:
            h = _host(url)
            m = mirror_by_host.get(h)
            row = Adw.ActionRow(title=h, subtitle=url)
            row.set_subtitle_selectable(True)
            if h in new_hosts:
                badge = Gtk.Label(label="new")
                badge.add_css_class("pill")
                badge.add_css_class("accent")
                row.add_prefix(badge)
            if m:
                chips = Gtk.Box(spacing=6)
                chips.set_valign(Gtk.Align.CENTER)
                lat_chip = Gtk.Label(label=f"{m.latency_ms} ms")  # type: ignore[attr-defined]
                lat_chip.add_css_class("pill")
                cty = Gtk.Label(label=m.country)  # type: ignore[attr-defined]
                cty.add_css_class("dim-label")
                cty.add_css_class("caption")
                chips.append(lat_chip)
                chips.append(cty)
                row.add_suffix(chips)
            top_grp.add(row)
        if n_prev > 6:
            more = Adw.ActionRow(title=f"… and {n_prev - 6} more")
            top_grp.add(more)
        self._comparison_inner.append(top_grp)

        # ── New mirrors detail ────────────────────────────────────────
        if new_hosts:
            new_grp = Adw.PreferencesGroup(title=f"New Mirrors  ({len(new_hosts)})")
            for h in sorted(new_hosts):
                m = mirror_by_host.get(h)
                row = Adw.ActionRow(title=h)
                if m:
                    row.set_subtitle(
                        f"{m.country}  ·  {m.latency_ms} ms  ·  {m.protocol}"  # type: ignore[attr-defined]
                    )
                new_grp.add(row)
            self._comparison_inner.append(new_grp)

        self._comparison_revealer.set_reveal_child(True)
        return False

    def _generate_preview(self, _button: Gtk.Button) -> None:
        self._generate_button.set_sensitive(False)
        self._apply_button.set_sensitive(False)
        self._generate_spinner.set_visible(True)
        self._generate_spinner.start()
        self.preview_status_row.set_subtitle("Running reflector…")
        self.command_buffer.set_text("Building command…")
        self._select_page("history")
        options = self._collect_generation_options()
        thread = threading.Thread(target=self._generate_preview_thread, args=(options,), daemon=True)
        thread.start()

    def _generate_preview_thread(self, options: GenerationOptions) -> None:
        result = generate_mirrorlist(options)
        GLib.idle_add(self._apply_generation_result, result)

    def _apply_generation_result(self, result: GenerationResult) -> bool:
        self.generated_result = result
        self._generate_button.set_sensitive(True)
        self._generate_spinner.stop()
        self._generate_spinner.set_visible(False)
        self._apply_button.set_sensitive(result.success)
        self.command_buffer.set_text(shlex.join(result.command))
        if result.success:
            self.preview_status_row.set_subtitle("Preview generated successfully")
            self.preview_detail_row.set_subtitle(result.message)
            self.generated_buffer.set_text(result.mirrorlist_text)
            self.diff_buffer.set_text(result.diff_text)
            self._populate_comparison(result.mirrorlist_text)
        else:
            self.preview_status_row.set_subtitle("Preview generation failed")
            self.preview_detail_row.set_subtitle(result.message.splitlines()[0])
            self.generated_buffer.set_text("Generation failed.")
            self.diff_buffer.set_text(result.message)
            self._comparison_revealer.set_reveal_child(False)
        return False

    def _apply_generated_mirrorlist(self, _button: Gtk.Button) -> None:
        if self.generated_result is None or not self.generated_result.success:
            self._show_toast("Generate a valid preview first.")
            return

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Apply Mirrorlist?",
            body=(
                "This replaces /etc/pacman.d/mirrorlist and requires admin privileges.\n"
                "A timestamped backup is created automatically."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_apply_confirmed)
        dialog.present()

    def _on_apply_confirmed(self, _dialog: Adw.MessageDialog, response: str) -> None:
        if response != "apply":
            return
        self._apply_button.set_sensitive(False)
        self._generate_spinner.set_visible(True)
        self._generate_spinner.start()
        text = self.generated_result.mirrorlist_text

        def do_apply() -> None:
            result = apply_mirrorlist(text)
            GLib.idle_add(_on_done, result)

        def _on_done(result: "ApplyResult") -> bool:
            self._generate_spinner.stop()
            self._generate_spinner.set_visible(False)
            self._apply_button.set_sensitive(True)
            self.preview_status_row.set_subtitle(result.message)
            self.preview_detail_row.set_subtitle(result.message)
            if result.success:
                self._show_toast("Mirrorlist applied. Backup saved.")
                self._reload_mirrors(None)
            else:
                self._show_toast(result.message.splitlines()[0], timeout=6)
            return False

        threading.Thread(target=do_apply, daemon=True).start()

    def _show_restore_dialog(self, _button: Gtk.Button) -> None:
        backups = list_backups()
        if not backups:
            self.preview_status_row.set_subtitle("No backups found")
            self.preview_detail_row.set_subtitle("No backup files exist in /etc/pacman.d/")
            return

        dialog = Adw.MessageDialog(transient_for=self, heading="Restore Mirrorlist from Backup")
        dialog.set_close_response("cancel")

        content = Adw.PreferencesGroup()
        content.set_title("Available Backups")

        dropdown = Adw.ComboRow()
        dropdown.set_title("Select Backup")

        dropdown_model = Gtk.StringList()
        for path, timestamp in backups:
            dropdown_model.append(f"{os.path.basename(path)} ({timestamp})")
        dropdown.set_model(dropdown_model)

        content.add(dropdown)
        dialog.set_extra_child(content)

        def on_restore(_: Gtk.Button) -> None:
            index = dropdown.get_selected()
            if 0 <= index < len(backups):
                backup_path, _timestamp = backups[index]

                def do_restore() -> None:
                    result = restore_mirrorlist(backup_path)
                    GLib.idle_add(_on_done, result)

                def _on_done(result: RestoreResult) -> bool:
                    self.preview_status_row.set_subtitle(result.message)
                    self.preview_detail_row.set_subtitle(result.message)
                    if result.success:
                        self._show_toast("Mirrorlist restored successfully.")
                        self._reload_mirrors(None)
                    else:
                        self._show_toast(result.message.splitlines()[0], timeout=6)
                    return False

                threading.Thread(target=do_restore, daemon=True).start()
            dialog.close()

        dialog.add_response("cancel", "Cancel")
        dialog.add_response("restore", "Restore")
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda d, r: on_restore(None) if r == "restore" else d.close())

        dialog.present()

    def _build_pacman(self) -> Gtk.Widget:
        from vs_reflector_manager.pacman_conf_services import (
            PacmanOptions,
            apply_pacman_options,
            read_pacman_options,
        )

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        window_title = Adw.WindowTitle(
            title="Pacman",
            subtitle="/etc/pacman.conf options and repositories",
        )
        window_title.set_hexpand(True)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Reload from disk")
        refresh_btn.set_valign(Gtk.Align.CENTER)
        header_row.append(window_title)
        header_row.append(refresh_btn)
        box.append(header_row)

        # ── Options group ──────────────────────────────────────────────────
        opts_group = Adw.PreferencesGroup(title="Options")

        color_row = Adw.ActionRow(
            title="Color",
            subtitle="Colorized output in the terminal",
        )
        color_sw = Gtk.Switch()
        color_sw.set_valign(Gtk.Align.CENTER)
        color_row.add_suffix(color_sw)
        color_row.set_activatable_widget(color_sw)
        opts_group.add(color_row)

        verbose_row = Adw.ActionRow(
            title="VerbosePkgLists",
            subtitle="Show old and new package versions in upgrade list",
        )
        verbose_sw = Gtk.Switch()
        verbose_sw.set_valign(Gtk.Align.CENTER)
        verbose_row.add_suffix(verbose_sw)
        verbose_row.set_activatable_widget(verbose_sw)
        opts_group.add(verbose_row)

        candy_row = Adw.ActionRow(
            title="ILoveCandy",
            subtitle="Pac-Man progress bar instead of ####",
        )
        candy_sw = Gtk.Switch()
        candy_sw.set_valign(Gtk.Align.CENTER)
        candy_row.add_suffix(candy_sw)
        candy_row.set_activatable_widget(candy_sw)
        opts_group.add(candy_row)

        parallel_row = Adw.ActionRow(
            title="Parallel Downloads",
            subtitle="Simultaneous package downloads",
        )
        parallel_spin = Gtk.SpinButton()
        parallel_spin.set_adjustment(Gtk.Adjustment(value=5, lower=1, upper=20, step_increment=1))
        parallel_spin.set_valign(Gtk.Align.CENTER)
        parallel_row.add_suffix(parallel_spin)
        opts_group.add(parallel_row)

        box.append(opts_group)

        # ── Repositories group ─────────────────────────────────────────────
        repos_group = Adw.PreferencesGroup(
            title="Repositories",
            description="Changes take effect on next pacman operation",
        )

        repo_switches: dict[str, Gtk.Switch] = {}
        for repo_id, title, subtitle in (
            ("multilib", "multilib", "32-bit package support on x86_64"),
            ("core-testing", "core-testing", "Testing packages for [core]"),
            ("extra-testing", "extra-testing", "Testing packages for [extra]"),
            ("multilib-testing", "multilib-testing", "Testing packages for [multilib]"),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            sw = Gtk.Switch()
            sw.set_valign(Gtk.Align.CENTER)
            row.add_suffix(sw)
            row.set_activatable_widget(sw)
            repos_group.add(row)
            repo_switches[repo_id] = sw

        box.append(repos_group)

        # ── Apply button ───────────────────────────────────────────────────
        apply_btn = Gtk.Button(label="Apply Changes")
        apply_btn.add_css_class("suggested-action")
        apply_btn.set_halign(Gtk.Align.START)
        box.append(apply_btn)

        def _load_options() -> None:
            def do_read() -> None:
                opts = read_pacman_options()
                GLib.idle_add(_apply_opts, opts)

            def _apply_opts(opts) -> bool:
                color_sw.set_active(opts.color)
                verbose_sw.set_active(opts.verbose_pkg_lists)
                candy_sw.set_active(opts.i_love_candy)
                parallel_spin.set_value(opts.parallel_downloads)
                repo_switches["multilib"].set_active(opts.multilib)
                repo_switches["core-testing"].set_active(opts.core_testing)
                repo_switches["extra-testing"].set_active(opts.extra_testing)
                repo_switches["multilib-testing"].set_active(opts.multilib_testing)
                return False

            threading.Thread(target=do_read, daemon=True).start()

        def _on_apply(_btn: Gtk.Button) -> None:
            apply_btn.set_sensitive(False)
            options = PacmanOptions(
                color=color_sw.get_active(),
                parallel_downloads=int(parallel_spin.get_value()),
                verbose_pkg_lists=verbose_sw.get_active(),
                i_love_candy=candy_sw.get_active(),
                multilib=repo_switches["multilib"].get_active(),
                core_testing=repo_switches["core-testing"].get_active(),
                extra_testing=repo_switches["extra-testing"].get_active(),
                multilib_testing=repo_switches["multilib-testing"].get_active(),
            )

            def do_apply() -> None:
                result = apply_pacman_options(options)
                GLib.idle_add(_on_done, result)

            def _on_done(result) -> bool:
                apply_btn.set_sensitive(True)
                if result.success:
                    self._show_toast("pacman.conf updated.")
                    _load_options()
                else:
                    self._show_toast(result.message.splitlines()[0], timeout=6)
                return False

            threading.Thread(target=do_apply, daemon=True).start()

        refresh_btn.connect("clicked", lambda _b: _load_options())
        apply_btn.connect("clicked", _on_apply)
        _load_options()

        # ── Orphaned packages ──────────────────────────────────────────────────
        orphan_grp = Adw.PreferencesGroup(
            title="Orphaned Packages",
            description="Installed as dependencies, no longer required by any package",
        )
        orphan_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        orphan_scan_btn = Gtk.Button(label="Scan for Orphans")
        orphan_scan_btn.set_halign(Gtk.Align.START)
        orphan_remove_btn = Gtk.Button(label="Remove Selected")
        orphan_remove_btn.add_css_class("destructive-action")
        orphan_remove_btn.set_halign(Gtk.Align.START)
        orphan_remove_btn.set_sensitive(False)

        orphan_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        orphan_btns.append(orphan_scan_btn)
        orphan_btns.append(orphan_remove_btn)

        _orphan_checks: list[tuple[str, Gtk.CheckButton]] = []

        def _on_orphan_scan(_btn: Gtk.Button) -> None:
            orphan_scan_btn.set_sensitive(False)
            child = orphan_list_box.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                orphan_list_box.remove(child)
                child = nxt
            _orphan_checks.clear()
            orphan_remove_btn.set_sensitive(False)

            def do_scan() -> None:
                pkgs = get_orphan_packages()
                GLib.idle_add(_on_scan_done, pkgs)

            def _on_scan_done(pkgs: list[str]) -> bool:
                orphan_scan_btn.set_sensitive(True)
                if not pkgs:
                    lbl = Gtk.Label(label="No orphaned packages found.", xalign=0)
                    lbl.add_css_class("dim-label")
                    lbl.set_margin_top(6)
                    orphan_list_box.append(lbl)
                    return False
                for pkg in pkgs:
                    row = Adw.ActionRow(title=pkg)
                    cb = Gtk.CheckButton()
                    cb.set_valign(Gtk.Align.CENTER)
                    cb.connect("toggled", lambda _c: orphan_remove_btn.set_sensitive(
                        any(c.get_active() for _, c in _orphan_checks)
                    ))
                    row.add_prefix(cb)
                    _orphan_checks.append((pkg, cb))
                    orphan_list_box.append(row)
                return False

            threading.Thread(target=do_scan, daemon=True).start()

        def _on_orphan_remove(_btn: Gtk.Button) -> None:
            selected = [pkg for pkg, cb in _orphan_checks if cb.get_active()]
            if not selected:
                return
            orphan_remove_btn.set_sensitive(False)
            orphan_scan_btn.set_sensitive(False)

            def do_remove() -> None:
                ok, msg = remove_orphans(selected)
                GLib.idle_add(_on_remove_done, ok, msg)

            def _on_remove_done(ok: bool, msg: str) -> bool:
                self._show_toast(msg, timeout=5)
                orphan_scan_btn.set_sensitive(True)
                if ok:
                    _on_orphan_scan(orphan_scan_btn)
                else:
                    orphan_remove_btn.set_sensitive(True)
                return False

            threading.Thread(target=do_remove, daemon=True).start()

        orphan_scan_btn.connect("clicked", _on_orphan_scan)
        orphan_remove_btn.connect("clicked", _on_orphan_remove)
        box.append(orphan_grp)
        box.append(orphan_list_box)
        box.append(orphan_btns)

        scroll.set_child(box)
        return scroll

    def _build_update(self) -> Gtk.Widget:
        _proc: list[subprocess.Popen | None] = [None]
        _start_ts: list[float] = [0.0]

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_hexpand(True)

        # ── Header ────────────────────────────────────────────────────
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_margin_top(20)
        header_box.set_margin_bottom(12)
        header_box.set_margin_start(20)
        header_box.set_margin_end(20)

        win_title = Adw.WindowTitle(title="System Update", subtitle="pacman -Syyuu --noconfirm")
        win_title.set_hexpand(True)

        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_btn.set_tooltip_text("Clear output")
        clear_btn.set_valign(Gtk.Align.CENTER)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.add_css_class("destructive-action")
        stop_btn.set_valign(Gtk.Align.CENTER)
        stop_btn.set_sensitive(False)

        run_btn = Gtk.Button(label="Run Update")
        run_btn.add_css_class("suggested-action")
        run_btn.set_valign(Gtk.Align.CENTER)

        header_box.append(win_title)
        header_box.append(clear_btn)
        header_box.append(stop_btn)
        header_box.append(run_btn)

        # ── Terminal ──────────────────────────────────────────────────
        terminal_scroll = Gtk.ScrolledWindow()
        terminal_scroll.set_hexpand(True)
        terminal_scroll.set_size_request(-1, 440)
        terminal_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_cursor_visible(False)
        tv.set_monospace(True)
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.set_left_margin(12)
        tv.set_right_margin(12)
        tv.set_top_margin(12)
        tv.set_bottom_margin(12)
        tv.add_css_class("terminal-view")
        terminal_scroll.set_child(tv)

        buf = tv.get_buffer()
        end_mark = buf.create_mark("end", buf.get_end_iter(), False)

        # ── Stats panel (revealed after completion) ───────────────────
        stats_revealer = Gtk.Revealer()
        stats_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        stats_revealer.set_transition_duration(300)
        stats_revealer.set_reveal_child(False)
        stats_revealer.set_hexpand(True)

        stats_scroll = Gtk.ScrolledWindow()
        stats_scroll.set_hexpand(True)
        stats_scroll.set_size_request(-1, 480)
        stats_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        stats_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        stats_inner.set_margin_top(16)
        stats_inner.set_margin_bottom(20)
        stats_inner.set_margin_start(20)
        stats_inner.set_margin_end(20)
        stats_scroll.set_child(stats_inner)
        stats_revealer.set_child(stats_scroll)

        # ── Stats population ──────────────────────────────────────────
        def _populate_stats(text: str, rc: int | None, elapsed: float) -> None:
            child = stats_inner.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                stats_inner.remove(child)
                child = nxt

            up_to_date = "there is nothing to do" in text.lower()

            m = re.search(r"Packages \((\d+)\)", text)
            pkg_count = int(m.group(1)) if m else 0

            m = re.search(r"Total Download Size:\s+([\d.]+ \S+)", text)
            download_size = m.group(1) if m else "—"

            m = re.search(r"Total Installed Size:\s+([\d.]+ \S+)", text)
            installed_size = m.group(1) if m else "—"

            m = re.search(r"Net Upgrade Size:\s+([\d.]+ \S+)", text)
            net_size = m.group(1) if m else "—"

            upgraded    = re.findall(r"\(\d+/\d+\) upgrading\s+(\S+)",   text)
            installed_p = re.findall(r"\(\d+/\d+\) installing\s+(\S+)",  text)
            removed_p   = re.findall(r"\(\d+/\d+\) removing\s+(\S+)",    text)
            downgraded  = re.findall(r"\(\d+/\d+\) downgrading\s+(\S+)", text)
            reinstalled = re.findall(r"\(\d+/\d+\) reinstalling\s+(\S+)", text)

            db_refreshed  = re.findall(r"^\s+(\S+)\s+[\d.]+\s+\S+iB", text, re.MULTILINE)
            db_uptodate   = re.findall(r"^\s+(\S+) is up to date", text, re.MULTILINE)

            warnings = re.findall(r"^warning:\s+(.+)$", text, re.MULTILINE)
            errors   = re.findall(r"^error:\s+(.+)$",   text, re.MULTILINE)

            total_changed = len(upgraded) + len(installed_p) + len(removed_p) + len(downgraded) + len(reinstalled)

            mins = int(elapsed) // 60
            secs = int(elapsed) % 60
            elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"

            # ── Status banner ──────────────────────────────────────────
            banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            banner.add_css_class("card")
            banner.set_margin_top(4)
            banner.set_margin_bottom(4)

            if rc == 0 and up_to_date:
                icon_name = "emblem-default-symbolic"
                headline = "System is up to date"
                sub_txt  = f"No packages to upgrade  ·  {elapsed_str}"
            elif rc == 0:
                icon_name = "software-update-available-symbolic"
                headline = f"{total_changed} package{'s' if total_changed != 1 else ''} updated"
                sub_txt  = f"Completed in {elapsed_str}"
            else:
                icon_name = "dialog-warning-symbolic"
                headline = "Update did not complete"
                sub_txt  = f"Exit code {rc}  ·  {elapsed_str}"

            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(36)
            icon.set_valign(Gtk.Align.CENTER)

            text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_col.set_valign(Gtk.Align.CENTER)
            text_col.set_hexpand(True)
            hl = Gtk.Label(label=headline, xalign=0)
            hl.add_css_class("title-3")
            sl = Gtk.Label(label=sub_txt, xalign=0)
            sl.add_css_class("dim-label")
            text_col.append(hl)
            text_col.append(sl)
            banner.append(icon)
            banner.append(text_col)
            stats_inner.append(banner)

            # ── Stat cards ─────────────────────────────────────────────
            cards_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            n_in_txn = total_changed if total_changed else pkg_count
            for title, value, detail in (
                ("Duration",    elapsed_str,   "wall clock"),
                ("Packages",    str(n_in_txn), "in transaction"),
                ("Downloaded",  download_size, "from mirrors"),
                ("Disk change", net_size,      "net size delta"),
                ("Installed",   installed_size,"total on disk"),
            ):
                cards_row.append(StatCard(title, value, detail))
            stats_inner.append(cards_row)

            # ── Package groups ─────────────────────────────────────────
            for grp_title, pkg_list, icon_str in (
                ("Upgraded",    upgraded,    "software-update-available-symbolic"),
                ("Installed",   installed_p, "list-add-symbolic"),
                ("Reinstalled", reinstalled, "view-refresh-symbolic"),
                ("Downgraded",  downgraded,  "go-down-symbolic"),
                ("Removed",     removed_p,   "list-remove-symbolic"),
            ):
                if not pkg_list:
                    continue
                grp = Adw.PreferencesGroup(title=f"{grp_title}  ({len(pkg_list)})")
                for name in pkg_list:
                    row = Adw.ActionRow(title=name)
                    img = Gtk.Image.new_from_icon_name(icon_str)
                    img.set_pixel_size(16)
                    row.add_prefix(img)
                    grp.add(row)
                stats_inner.append(grp)

            # ── Database sync ──────────────────────────────────────────
            if db_refreshed or db_uptodate:
                db_grp = Adw.PreferencesGroup(title="Databases")
                for db in db_refreshed:
                    row = Adw.ActionRow(title=db, subtitle="refreshed")
                    img = Gtk.Image.new_from_icon_name("network-transmit-receive-symbolic")
                    img.set_pixel_size(16)
                    row.add_prefix(img)
                    db_grp.add(row)
                for db in db_uptodate:
                    row = Adw.ActionRow(title=db, subtitle="up to date")
                    img = Gtk.Image.new_from_icon_name("emblem-default-symbolic")
                    img.set_pixel_size(16)
                    row.add_prefix(img)
                    db_grp.add(row)
                stats_inner.append(db_grp)

            # ── Warnings ───────────────────────────────────────────────
            if warnings:
                w_grp = Adw.PreferencesGroup(title=f"Warnings  ({len(warnings)})")
                for w in warnings:
                    row = Adw.ActionRow(title=w)
                    img = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                    img.set_pixel_size(16)
                    row.add_prefix(img)
                    w_grp.add(row)
                stats_inner.append(w_grp)

            # ── Errors ────────────────────────────────────────────────
            if errors:
                e_grp = Adw.PreferencesGroup(title=f"Errors  ({len(errors)})")
                for e in errors:
                    row = Adw.ActionRow(title=e)
                    img = Gtk.Image.new_from_icon_name("dialog-error-symbolic")
                    img.set_pixel_size(16)
                    row.add_prefix(img)
                    e_grp.add(row)
                stats_inner.append(e_grp)

        # ── Callbacks ─────────────────────────────────────────────────
        def _append(text: str) -> bool:
            clean = _ANSI_RE.sub("", text)
            buf.insert(buf.get_end_iter(), clean)
            tv.scroll_to_mark(end_mark, 0.0, False, 0.0, 1.0)
            return False

        def _on_done(rc: int | None, elapsed: float) -> bool:
            _proc[0] = None
            run_btn.set_sensitive(True)
            stop_btn.set_sensitive(False)
            _append("\n── Update complete ──\n" if rc == 0 else f"\n── Exited with code {rc} ──\n")
            full = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            _populate_stats(full, rc, elapsed)
            terminal_scroll.set_size_request(-1, 220)
            stats_revealer.set_reveal_child(True)
            if rc == 0:
                threading.Thread(target=self._bg_check_updates, daemon=True).start()
            return False

        def _run(_btn: Gtk.Button) -> None:
            buf.set_text("")
            stats_revealer.set_reveal_child(False)
            terminal_scroll.set_size_request(-1, 440)
            run_btn.set_sensitive(False)
            stop_btn.set_sensitive(True)
            _start_ts[0] = _time.monotonic()

            def do_run() -> None:
                try:
                    proc = subprocess.Popen(
                        ["pkexec", "pacman", "-Syyuu", "--noconfirm"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                    _proc[0] = proc
                    if proc.stdout is None:
                        GLib.idle_add(_on_done, None, _time.monotonic() - _start_ts[0])
                        return
                    for line in proc.stdout:
                        GLib.idle_add(_append, line)
                    proc.wait()
                    elapsed = _time.monotonic() - _start_ts[0]
                    GLib.idle_add(_on_done, proc.returncode, elapsed)
                except Exception as exc:
                    elapsed = _time.monotonic() - _start_ts[0]
                    GLib.idle_add(_append, f"\nError: {exc}\n")
                    GLib.idle_add(_on_done, None, elapsed)

            threading.Thread(target=do_run, daemon=True).start()

        def _stop(_btn: Gtk.Button) -> None:
            if _proc[0] is not None:
                _proc[0].terminate()

        def _clear(_btn: Gtk.Button) -> None:
            buf.set_text("")
            stats_revealer.set_reveal_child(False)
            terminal_scroll.set_size_request(-1, 440)

        run_btn.connect("clicked", _run)
        stop_btn.connect("clicked", _stop)
        clear_btn.connect("clicked", _clear)

        self._news_revealer = Gtk.Revealer()
        self._news_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._news_revealer.set_transition_duration(300)
        self._news_revealer.set_reveal_child(False)
        self._news_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._news_inner.set_margin_top(12)
        self._news_inner.set_margin_bottom(4)
        self._news_inner.set_margin_start(20)
        self._news_inner.set_margin_end(20)
        self._news_revealer.set_child(self._news_inner)

        outer.append(header_box)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        outer.append(self._news_revealer)
        outer.append(terminal_scroll)
        outer.append(stats_revealer)
        return outer

    def _build_chaotic(self) -> Gtk.Widget:
        from vs_reflector_manager.chaotic_services import PACMAN_CONF_SNIPPET, SETUP_COMMANDS, detect_state

        self._chaotic_state = "unknown"
        self._chaotic_mirrors: list = []
        self._chaotic_mirror_rows: list[ChaoticMirrorRow] = []
        self._chaotic_probe_session = 0
        self._chaotic_running_probes = 0

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        window_title = Adw.WindowTitle(
            title="Chaotic AUR",
            subtitle="Third-party prebuilt AUR package repository",
        )
        window_title.set_hexpand(True)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh Status")
        refresh_btn.set_valign(Gtk.Align.CENTER)
        refresh_btn.connect("clicked", self._refresh_chaotic_state)
        header_row.append(window_title)
        header_row.append(refresh_btn)
        box.append(header_row)

        self._chaotic_status_group = Adw.PreferencesGroup(title="Status")
        self._chaotic_installed_row = Adw.ActionRow(title="Mirrorlist Package")
        self._chaotic_configured_row = Adw.ActionRow(title="pacman.conf Entry")
        self._chaotic_status_group.add(self._chaotic_installed_row)
        self._chaotic_status_group.add(self._chaotic_configured_row)
        box.append(self._chaotic_status_group)

        self._chaotic_setup_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        setup_heading = Gtk.Label(label="Installation Guide", xalign=0)
        setup_heading.add_css_class("heading")
        setup_desc = Gtk.Label(
            label="Run these commands in a terminal to install the Chaotic AUR keyring and mirrorlist:",
            xalign=0,
            wrap=True,
        )
        setup_desc.add_css_class("dim-label")
        setup_frame = Gtk.Frame()
        setup_buffer = Gtk.TextBuffer()
        setup_buffer.set_text("\n".join(SETUP_COMMANDS))
        setup_view = Gtk.TextView(buffer=setup_buffer)
        setup_view.set_editable(False)
        setup_view.set_monospace(True)
        setup_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        setup_view.add_css_class("code-view")
        setup_scroll = Gtk.ScrolledWindow()
        setup_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        setup_scroll.set_min_content_height(100)
        setup_scroll.set_child(setup_view)
        setup_frame.set_child(setup_scroll)
        setup_copy_btn = Gtk.Button(label="Copy Commands")
        setup_copy_btn.set_halign(Gtk.Align.START)
        setup_copy_btn.connect(
            "clicked",
            lambda _b, cmds="\n".join(SETUP_COMMANDS): self._copy_to_clipboard(cmds),
        )
        self._chaotic_setup_section.append(setup_heading)
        self._chaotic_setup_section.append(setup_desc)
        self._chaotic_setup_section.append(setup_frame)
        self._chaotic_setup_section.append(setup_copy_btn)
        box.append(self._chaotic_setup_section)

        self._chaotic_config_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        config_heading = Gtk.Label(label="Enable in pacman.conf", xalign=0)
        config_heading.add_css_class("heading")
        config_desc = Gtk.Label(
            label="Chaotic AUR is installed but not enabled in /etc/pacman.conf.\n"
                  "Add this snippet at the end of the file:",
            xalign=0,
            wrap=True,
        )
        config_desc.add_css_class("dim-label")
        config_frame = Gtk.Frame()
        config_buffer = Gtk.TextBuffer()
        config_buffer.set_text(PACMAN_CONF_SNIPPET)
        config_view = Gtk.TextView(buffer=config_buffer)
        config_view.set_editable(False)
        config_view.set_monospace(True)
        config_view.add_css_class("code-view")
        config_scroll = Gtk.ScrolledWindow()
        config_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        config_scroll.set_min_content_height(56)
        config_scroll.set_child(config_view)
        config_frame.set_child(config_scroll)
        config_copy_btn = Gtk.Button(label="Copy Snippet")
        config_copy_btn.set_halign(Gtk.Align.START)
        config_copy_btn.connect(
            "clicked",
            lambda _b, snip=PACMAN_CONF_SNIPPET: self._copy_to_clipboard(snip),
        )
        self._chaotic_config_section.append(config_heading)
        self._chaotic_config_section.append(config_desc)
        self._chaotic_config_section.append(config_frame)
        self._chaotic_config_section.append(config_copy_btn)
        box.append(self._chaotic_config_section)

        self._chaotic_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._chaotic_probe_btn = Gtk.Button(label="Probe All Mirrors")
        self._chaotic_probe_btn.add_css_class("suggested-action")
        self._chaotic_probe_btn.connect("clicked", self._run_chaotic_probes)
        self._chaotic_apply_changes_btn = Gtk.Button(label="Apply Changes")
        self._chaotic_apply_changes_btn.set_sensitive(False)
        self._chaotic_apply_changes_btn.connect("clicked", self._apply_chaotic_changes)
        self._chaotic_apply_best_btn = Gtk.Button(label="Apply Best Mirror")
        self._chaotic_apply_best_btn.set_sensitive(False)
        self._chaotic_apply_best_btn.connect("clicked", self._apply_best_chaotic)
        self._chaotic_actions.append(self._chaotic_probe_btn)
        self._chaotic_actions.append(self._chaotic_apply_changes_btn)
        self._chaotic_actions.append(self._chaotic_apply_best_btn)
        box.append(self._chaotic_actions)

        self._chaotic_mirrors_group = Adw.PreferencesGroup(title="Mirrors")
        box.append(self._chaotic_mirrors_group)

        GLib.idle_add(self._chaotic_load_state)
        return box

    def _chaotic_load_state(self) -> bool:
        from vs_reflector_manager.chaotic_services import detect_state as _detect_state
        def do_detect():
            state = _detect_state()
            GLib.idle_add(self._chaotic_apply_state, state)
        threading.Thread(target=do_detect, daemon=True).start()
        return False

    def _chaotic_apply_state(self, state) -> bool:
        self._chaotic_state = state
        self._update_chaotic_ui()
        return False

    def _update_chaotic_ui(self) -> None:
        state = self._chaotic_state
        if isinstance(state, str):
            self._chaotic_installed_row.set_subtitle("Loading…")
            self._chaotic_configured_row.set_subtitle("Loading…")
            self._chaotic_setup_section.set_visible(False)
            self._chaotic_config_section.set_visible(False)
            self._chaotic_actions.set_visible(False)
            self._chaotic_mirrors_group.set_visible(False)
            return
        installed = state.installed
        configured = state.configured

        self._chaotic_installed_row.set_subtitle(
            "Installed — /etc/pacman.d/chaotic-mirrorlist" if installed else "Not installed"
        )
        self._chaotic_configured_row.set_subtitle(
            "Enabled in /etc/pacman.conf" if configured else "Not configured"
        )

        self._chaotic_setup_section.set_visible(not installed)
        self._chaotic_config_section.set_visible(installed and not configured)
        self._chaotic_actions.set_visible(installed)
        self._chaotic_mirrors_group.set_visible(installed)

        for row in list(self._chaotic_mirror_rows):
            self._chaotic_mirrors_group.remove(row)
        self._chaotic_mirror_rows.clear()
        self._chaotic_mirrors = list(state.mirrors)

        active_count = sum(1 for m in self._chaotic_mirrors if m.active)
        self._chaotic_mirrors_group.set_title(
            f"Mirrors — {active_count} active / {len(self._chaotic_mirrors)} total"
            if self._chaotic_mirrors
            else "Mirrors"
        )

        for mirror in self._chaotic_mirrors:
            row = ChaoticMirrorRow(mirror, self._on_chaotic_mirror_toggled)
            self._chaotic_mirrors_group.add(row)
            self._chaotic_mirror_rows.append(row)

        self._chaotic_apply_changes_btn.set_sensitive(False)
        self._chaotic_apply_best_btn.set_sensitive(False)

    def _refresh_chaotic_state(self, _button: Gtk.Button) -> None:
        from vs_reflector_manager.chaotic_services import detect_state as _detect_state
        self._chaotic_state = "unknown"
        self._update_chaotic_ui()
        def do_detect():
            state = _detect_state()
            def _done():
                self._chaotic_apply_state(state)
                self._show_toast("Chaotic AUR status refreshed.")
                return False
            GLib.idle_add(_done)
        threading.Thread(target=do_detect, daemon=True).start()

    def _on_chaotic_mirror_toggled(self, mirror, active: bool) -> None:
        mirror.active = active
        active_count = sum(1 for m in self._chaotic_mirrors if m.active)
        self._chaotic_mirrors_group.set_title(
            f"Mirrors — {active_count} active / {len(self._chaotic_mirrors)} total"
        )
        self._chaotic_apply_changes_btn.set_sensitive(True)

    def _run_chaotic_probes(self, _button: Gtk.Button) -> None:
        if not self._chaotic_mirror_rows:
            return
        self._chaotic_probe_session += 1
        session = self._chaotic_probe_session
        self._chaotic_probe_btn.set_sensitive(False)
        self._chaotic_apply_best_btn.set_sensitive(False)
        self._chaotic_running_probes = len(self._chaotic_mirror_rows)
        for row in self._chaotic_mirror_rows:
            row.set_probe_result("probing…")
            thread = threading.Thread(
                target=self._chaotic_probe_thread,
                args=(row, session),
                daemon=True,
            )
            thread.start()

    def _chaotic_probe_thread(self, row: ChaoticMirrorRow, session: int) -> None:
        def on_update(**kwargs) -> None:
            GLib.idle_add(self._apply_chaotic_probe_update, row, kwargs, session)

        run_probe(row.mirror_url, on_update)

    def _apply_chaotic_probe_update(
        self, row: ChaoticMirrorRow, update: dict, session: int
    ) -> bool:
        if session != self._chaotic_probe_session:
            return False
        state = update.get("state", "")
        latency_ms = update.get("latency_ms", 0)
        row.set_probe_result(state, latency_ms)
        if state in {"Complete", "Failed"}:
            self._chaotic_running_probes = max(0, self._chaotic_running_probes - 1)
            if self._chaotic_running_probes == 0:
                self._chaotic_probe_btn.set_sensitive(True)
                completed = [r for r in self._chaotic_mirror_rows if r.probe_state == "Complete"]
                if completed:
                    self._chaotic_apply_best_btn.set_sensitive(True)
        return False

    def _apply_chaotic_changes(self, _button: Gtk.Button) -> None:
        from vs_reflector_manager.chaotic_services import apply_chaotic_mirrorlist, rebuild_mirrorlist

        text = rebuild_mirrorlist(self._chaotic_mirrors)
        active_count = sum(1 for m in self._chaotic_mirrors if m.active)
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Apply Chaotic AUR Mirrorlist?",
            body=(
                f"Updates /etc/pacman.d/chaotic-mirrorlist with {active_count} active mirror(s).\n"
                "Requires admin privileges."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.MessageDialog, response: str) -> None:
            if response != "apply":
                return

            def do_apply() -> None:
                result = apply_chaotic_mirrorlist(text)
                GLib.idle_add(_on_done, result)

            def _on_done(result) -> bool:
                if result.success:
                    self._show_toast("Chaotic AUR mirrorlist updated.")
                    from vs_reflector_manager.chaotic_services import detect_state
                    self._chaotic_state = detect_state()
                    self._update_chaotic_ui()
                else:
                    self._show_toast(result.message.splitlines()[0], timeout=6)
                return False

            threading.Thread(target=do_apply, daemon=True).start()

        dialog.connect("response", on_response)
        dialog.present()

    def _apply_best_chaotic(self, _button: Gtk.Button) -> None:
        from vs_reflector_manager.chaotic_services import apply_chaotic_mirrorlist, rebuild_mirrorlist

        completed = [
            r for r in self._chaotic_mirror_rows
            if r.probe_state == "Complete" and r.probe_latency_ms > 0
        ]
        if not completed:
            self._show_toast("No completed probes to apply.")
            return

        best_row = min(completed, key=lambda r: r.probe_latency_ms)
        for row in self._chaotic_mirror_rows:
            row.set_active(row is best_row)

        text = rebuild_mirrorlist(self._chaotic_mirrors)
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Apply Best Chaotic AUR Mirror?",
            body=(
                f"Sets {best_row.mirror_url} as the sole active mirror.\n"
                f"Probed latency: {best_row.probe_latency_ms} ms\n\n"
                "Requires admin privileges."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.MessageDialog, response: str) -> None:
            if response != "apply":
                return

            def do_apply() -> None:
                result = apply_chaotic_mirrorlist(text)
                GLib.idle_add(_on_done, result)

            def _on_done(result) -> bool:
                if result.success:
                    self._show_toast(f"Applied best Chaotic AUR mirror ({best_row.probe_latency_ms} ms).")
                    from vs_reflector_manager.chaotic_services import detect_state
                    self._chaotic_state = detect_state()
                    self._update_chaotic_ui()
                else:
                    self._show_toast(result.message.splitlines()[0], timeout=6)
                return False

            threading.Thread(target=do_apply, daemon=True).start()

        dialog.connect("response", on_response)
        dialog.present()

    def _copy_to_clipboard(self, text: str) -> None:
        self.get_display().get_clipboard().set(text)
        self._show_toast("Copied to clipboard.")

    # ── Pacman Log ────────────────────────────────────────────────────────────

    def _build_log(self) -> Gtk.Widget:
        _ACTION_ICONS = {
            "installed":   "list-add-symbolic",
            "upgraded":    "software-update-available-symbolic",
            "removed":     "list-remove-symbolic",
            "downgraded":  "go-down-symbolic",
            "reinstalled": "view-refresh-symbolic",
        }
        _ALL_ACTIONS = list(_ACTION_ICONS.keys())
        _active_filter: list[str | None] = [None]
        _all_entries: list[list[dict]] = [[]]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        win_title = Adw.WindowTitle(title="Pacman Log", subtitle="/var/log/pacman.log")
        win_title.set_hexpand(True)
        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        reload_btn.set_valign(Gtk.Align.CENTER)
        reload_btn.set_tooltip_text("Reload log")
        header_row.append(win_title)
        header_row.append(reload_btn)
        box.append(header_row)

        # ── Filter chips ────────────────────────────────────────────
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        filter_box.set_margin_bottom(4)
        _filter_btns: dict[str | None, Gtk.ToggleButton] = {}
        all_btn = Gtk.ToggleButton(label="All")
        all_btn.set_active(True)
        all_btn.add_css_class("pill")
        filter_box.append(all_btn)
        _filter_btns[None] = all_btn
        for action in _ALL_ACTIONS:
            btn = Gtk.ToggleButton(label=action.capitalize())
            btn.add_css_class("pill")
            filter_box.append(btn)
            _filter_btns[action] = btn
        box.append(filter_box)

        # ── Stats cards ─────────────────────────────────────────────
        stats_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        _stat_cards: dict[str, StatCard] = {}
        for action in _ALL_ACTIONS:
            sc = StatCard(action.capitalize(), "0", "packages")
            _stat_cards[action] = sc
            stats_row.append(sc)
        box.append(stats_row)

        # ── Entry list ──────────────────────────────────────────────
        list_group = Adw.PreferencesGroup()
        box.append(list_group)

        def _rebuild_list() -> None:
            child = list_group.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                list_group.remove(child)
                child = nxt

            filt = _active_filter[0]
            entries = [e for e in _all_entries[0] if filt is None or e["action"] == filt]

            for entry in entries[:200]:
                row = Adw.ActionRow(title=entry["pkg"], subtitle=entry["date"])
                img = Gtk.Image.new_from_icon_name(_ACTION_ICONS.get(entry["action"], "package-x-generic-symbolic"))
                img.set_pixel_size(16)
                row.add_prefix(img)
                ver_lbl = Gtk.Label(label=entry["version"])
                ver_lbl.add_css_class("dim-label")
                ver_lbl.add_css_class("caption")
                ver_lbl.set_valign(Gtk.Align.CENTER)
                row.add_suffix(ver_lbl)
                list_group.add(row)

            if len(entries) > 200:
                more = Adw.ActionRow(title=f"… {len(entries) - 200} more entries (showing newest 200)")
                list_group.add(more)

            if not entries:
                empty = Adw.ActionRow(title="No entries found")
                list_group.add(empty)

        def _load_log() -> None:
            reload_btn.set_sensitive(False)

            def do_parse() -> None:
                entries = parse_pacman_log()
                GLib.idle_add(_apply_log, entries)

            def _apply_log(entries: list[dict]) -> bool:
                _all_entries[0] = entries
                counts = {a: sum(1 for e in entries if e["action"] == a) for a in _ALL_ACTIONS}
                for action, sc in _stat_cards.items():
                    sc.update(str(counts.get(action, 0)), "packages")
                all_btn.set_label(f"All ({len(entries)})")
                reload_btn.set_sensitive(True)
                _rebuild_list()
                return False

            threading.Thread(target=do_parse, daemon=True).start()

        def _on_filter(action: str | None) -> None:
            _active_filter[0] = action
            for key, btn in _filter_btns.items():
                btn.set_active(key == action)
            _rebuild_list()

        for action, btn in _filter_btns.items():
            _action = action
            btn.connect("toggled", lambda b, a=_action: _on_filter(a) if b.get_active() else None)

        self._log_load_fn = _load_log
        reload_btn.connect("clicked", lambda _b: _load_log())
        return box

    # ── pacnew Files ──────────────────────────────────────────────────────────

    def _build_pacnew(self) -> Gtk.Widget:
        _files: list[list[str]] = [[]]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        win_title = Adw.WindowTitle(
            title="pacnew Files",
            subtitle="Config files left behind by package updates",
        )
        win_title.set_hexpand(True)
        scan_btn = Gtk.Button(label="Scan /etc")
        scan_btn.add_css_class("suggested-action")
        scan_btn.set_valign(Gtk.Align.CENTER)
        header_row.append(win_title)
        header_row.append(scan_btn)
        box.append(header_row)

        desc = Gtk.Label(
            label=(
                "pacman leaves .pacnew files when a package update would overwrite a modified config.\n"
                "Review each file and choose to apply the new version or delete the leftover."
            ),
            xalign=0,
            wrap=True,
        )
        desc.add_css_class("dim-label")
        box.append(desc)

        files_grp = Adw.PreferencesGroup(title="Found Files")
        box.append(files_grp)

        diff_grp = Adw.PreferencesGroup(title="Diff Preview")
        diff_scroll = Gtk.ScrolledWindow()
        diff_scroll.set_min_content_height(200)
        diff_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        diff_buf = Gtk.TextBuffer()
        diff_view = Gtk.TextView(buffer=diff_buf)
        diff_view.set_editable(False)
        diff_view.set_monospace(True)
        diff_view.set_wrap_mode(Gtk.WrapMode.NONE)
        diff_view.add_css_class("code-view")
        diff_scroll.set_child(diff_view)
        diff_grp_frame = Gtk.Frame()
        diff_grp_frame.set_child(diff_scroll)
        box.append(diff_grp)
        box.append(diff_grp_frame)
        diff_grp.set_visible(False)
        diff_grp_frame.set_visible(False)

        def _show_diff(pacnew_path: str) -> None:
            def do_diff() -> None:
                import difflib as _dl
                original = re.sub(r"\.(pacnew|pacsave)$", "", pacnew_path)
                try:
                    with open(original, errors="replace") as f:
                        orig_lines = f.readlines()
                except OSError:
                    orig_lines = []
                try:
                    with open(pacnew_path, errors="replace") as f:
                        new_lines = f.readlines()
                except OSError:
                    new_lines = []
                diff = list(_dl.unified_diff(
                    orig_lines, new_lines,
                    fromfile=original, tofile=pacnew_path, lineterm="",
                ))
                text = "\n".join(diff) if diff else "No differences."
                GLib.idle_add(lambda: (
                    diff_buf.set_text(text),
                    diff_grp.set_visible(True),
                    diff_grp_frame.set_visible(True),
                ) and False)

            threading.Thread(target=do_diff, daemon=True).start()

        def _rebuild_files() -> None:
            child = files_grp.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                files_grp.remove(child)
                child = nxt
            diff_grp.set_visible(False)
            diff_grp_frame.set_visible(False)

            if not _files[0]:
                row = Adw.ActionRow(title="No .pacnew or .pacsave files found")
                files_grp.add(row)
                return

            files_grp.set_title(f"Found Files  ({len(_files[0])})")
            for path in _files[0]:
                row = Adw.ActionRow(title=os.path.basename(path), subtitle=path)
                row.set_subtitle_selectable(True)

                diff_btn = Gtk.Button(label="Diff")
                diff_btn.add_css_class("flat")
                diff_btn.set_valign(Gtk.Align.CENTER)
                diff_btn.connect("clicked", lambda _b, p=path: _show_diff(p))

                apply_btn = Gtk.Button(label="Apply")
                apply_btn.add_css_class("suggested-action")
                apply_btn.set_valign(Gtk.Align.CENTER)

                del_btn = Gtk.Button(icon_name="edit-delete-symbolic")
                del_btn.add_css_class("destructive-action")
                del_btn.set_valign(Gtk.Align.CENTER)
                del_btn.set_tooltip_text("Delete .pacnew")

                def _on_apply(_, p=path) -> None:
                    def do_apply() -> None:
                        ok, msg = apply_pacnew(p)
                        def _done(ok: bool = ok, msg: str = msg) -> bool:
                            self._show_toast(msg, timeout=5 if not ok else 3)
                            if ok:
                                _on_scan(scan_btn)
                            return False
                        GLib.idle_add(_done)
                    threading.Thread(target=do_apply, daemon=True).start()

                def _on_delete(_, p=path) -> None:
                    def do_del() -> None:
                        ok, msg = delete_pacnew(p)
                        def _done(ok: bool = ok, msg: str = msg) -> bool:
                            self._show_toast(msg, timeout=5 if not ok else 3)
                            if ok:
                                _on_scan(scan_btn)
                            return False
                        GLib.idle_add(_done)
                    threading.Thread(target=do_del, daemon=True).start()

                apply_btn.connect("clicked", _on_apply)
                del_btn.connect("clicked", _on_delete)
                row.add_suffix(diff_btn)
                row.add_suffix(apply_btn)
                row.add_suffix(del_btn)
                files_grp.add(row)

        def _on_scan(_btn: Gtk.Button) -> None:
            scan_btn.set_sensitive(False)

            def do_scan() -> None:
                found = find_pacnew_files()
                GLib.idle_add(_on_scan_done, found)

            def _on_scan_done(found: list[str]) -> bool:
                _files[0] = found
                scan_btn.set_sensitive(True)
                _rebuild_files()
                return False

            threading.Thread(target=do_scan, daemon=True).start()

        scan_btn.connect("clicked", _on_scan)
        _rebuild_files()
        return box

    def _build_about(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_top(60)
        box.set_margin_bottom(40)
        box.set_margin_start(40)
        box.set_margin_end(40)

        # App icon — PNG preferred, fallback to symbolic
        icon_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "vsreflector-manager.png",
        )
        try:
            pic = Gtk.Picture.new_for_filename(icon_path)
            pic.set_can_shrink(True)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            icon_clamp = Adw.Clamp(maximum_size=128, tightening_threshold=0)
            icon_clamp.set_child(pic)
            icon_clamp.set_halign(Gtk.Align.CENTER)
            icon_clamp.set_margin_bottom(12)
            box.append(icon_clamp)
        except Exception:
            fallback = Gtk.Image.new_from_icon_name("network-server-symbolic")
            fallback.set_pixel_size(128)
            fallback.set_halign(Gtk.Align.CENTER)
            fallback.set_margin_bottom(12)
            box.append(fallback)

        for text, css in (
            ("vsReflector Manager", "title-1"),
            ("Version 1.0.0",       "dim-label"),
            ("Visual manager for Arch Linux pacman mirrors", "dim-label"),
            ("MIT License",         "accent"),
        ):
            lbl = Gtk.Label(label=text)
            lbl.add_css_class(css)
            lbl.set_halign(Gtk.Align.CENTER)
            lbl.set_margin_top(4)
            box.append(lbl)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep1.set_margin_top(24)
        sep1.set_margin_bottom(20)
        box.append(sep1)

        author_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        author_box.set_halign(Gtk.Align.CENTER)
        user_img = Gtk.Image.new_from_icon_name("system-users-symbolic")
        user_img.set_pixel_size(36)
        author_box.append(user_img)
        author_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        author_name = Gtk.Label(label="Víctor Sosa", xalign=0)
        author_name.add_css_class("heading")
        author_role = Gtk.Label(label="Developer  ·  victorsosa.com", xalign=0)
        author_role.add_css_class("dim-label")
        author_vbox.append(author_name)
        author_vbox.append(author_role)
        author_box.append(author_vbox)
        box.append(author_box)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep2.set_margin_top(20)
        sep2.set_margin_bottom(20)
        box.append(sep2)

        def _open(url: str) -> None:
            subprocess.Popen(
                ["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_row.set_halign(Gtk.Align.CENTER)
        for label, url in (
            ("vsReflector Manager", "https://github.com/victorsosaMx/vsReflector-Manager"),
            ("Arch Mirrors", "https://archlinux.org/mirrors/"),
            ("Chaotic AUR", "https://aur.chaotic.cx/"),
            ("victorsosa.com", "https://victorsosa.com/"),
        ):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda _b, u=url: _open(u))
            btn_row.append(btn)
        box.append(btn_row)
        return box


def clear_children(widget: Gtk.Widget) -> None:
    child = widget.get_first_child()
    while child is not None:
        next_child = child.get_next_sibling()
        widget.remove(child)
        child = next_child
