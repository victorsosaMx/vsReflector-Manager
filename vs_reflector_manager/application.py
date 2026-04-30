from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio

from vs_reflector_manager.window import MainWindow


class VSReflectorApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="com.vsReflector.Manager",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        Adw.init()

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = MainWindow(application=self)
        window.present()


def main() -> int:
    app = VSReflectorApplication()
    return app.run(None)
