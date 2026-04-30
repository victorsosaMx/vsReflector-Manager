from __future__ import annotations

import os
import shlex
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from typing import TYPE_CHECKING

from vs_reflector_manager.data import MirrorInfo, TestJob

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
    build_test_jobs,
    generate_mirrorlist,
    list_backups,
    load_mirrors,
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

    def _show_toast(self, message: str, timeout: int = 3) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)

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
            ("chaotic", "Chaotic AUR", "package-x-generic-symbolic"),
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
        self.stack.add_named(self._build_chaotic(), "chaotic")
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
        else:
            self.preview_status_row.set_subtitle("Preview generation failed")
            self.preview_detail_row.set_subtitle(result.message.splitlines()[0])
            self.generated_buffer.set_text("Generation failed.")
            self.diff_buffer.set_text(result.message)
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


    def _build_chaotic(self) -> Gtk.Widget:
        from vs_reflector_manager.chaotic_services import PACMAN_CONF_SNIPPET, SETUP_COMMANDS, detect_state

        self._chaotic_state = detect_state()
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

        self._update_chaotic_ui()
        return box

    def _update_chaotic_ui(self) -> None:
        state = self._chaotic_state
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
        from vs_reflector_manager.chaotic_services import detect_state
        self._chaotic_state = detect_state()
        self._update_chaotic_ui()
        self._show_toast("Chaotic AUR status refreshed.")

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

    def _build_about(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_top(60)
        box.set_margin_bottom(40)
        box.set_margin_start(40)
        box.set_margin_end(40)

        # App icon — PNG preferred, fallback to symbolic
        icon_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "vs-reflector-manager.png",
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
