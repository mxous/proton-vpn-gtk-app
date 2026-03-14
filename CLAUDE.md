# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Proton VPN GTK4 desktop app for Linux. Python 3.9+, PyGObject/GTK4, GPLv3. The app provides VPN connection management, server browsing, settings, tray indicator, and 2FA authentication. Migrated from GTK3 to GTK4 in v4.14.0.

## Development

This is a namespace package (no `__init__.py` in `proton/`, `proton/vpn/`, `proton/vpn/app/`). Python merges the local source with system-installed `proton.*` packages.

On Arch with the system package installed, edit files in `proton/vpn/app/gtk/` and run from the project directory:

```shell
python -m proton.vpn.app.gtk
```

This uses local source (`.` is added to `sys.path`) while resolving other `proton.*` deps (api-core, etc.) from system packages. The system install stays untouched.

`protonvpn-app` (the `/usr/bin/` entry point) always runs the **system-installed** version, not local source.

## Building & Packaging

Standard Python setuptools project (`setup.py`). Pure-Python package (noarch).

```shell
python setup.py sdist        # source tarball
pip wheel . --no-deps        # wheel
```

Do NOT run `pip install .` or `pip install -e .` outside a venv — it writes to system site-packages and conflicts with the pacman package.

On **Arch Linux**, this is in the `extra` repo as `proton-vpn-gtk-app`. System deps: `gtk4 python-gobject python-cairo dbus-python libnotify proton-vpn-daemon python-proton-core python-proton-vpn-api-core python-proton-vpn-network-manager python-proton-vpn-local-agent`. Optional: `libappindicator-gtk3` (tray icon).

For **Debian/Ubuntu**: `debian/` dir with pybuild. For **Fedora/RPM**: `rpmbuild/SPECS/package.spec.template` with `%pyproject_wheel`/`%pyproject_install`.

The `.desktop` file and SVG icon live in `rpmbuild/SOURCES/`. Version is read from `versions.yml` at build time by `setup.py`.

## Testing & Linting

```shell
pytest                                                    # all unit tests (with coverage)
pytest tests/unit/path/test_file.py                       # single file
pytest tests/unit/path/test_file.py::Class::test_name -v  # single test

flake8 proton/ tests/           # lint (max-line-length=100)
pylint proton/

# Integration tests (requires display or Xvfb)
behave tests/integration/features
xvfb-run -a behave tests/integration/features  # headless
```

Test config is in `setup.cfg`: pytest runs with `--cov=proton`, targets `tests/unit`.

## Architecture

### Bootstrap Flow

`__main__.py` → creates `AsyncExecutor` (asyncio loop in background thread) + `ExceptionHandler` → `Controller.get()` initializes VPN API → `App(Gtk.Application).run()` starts GTK main loop.

### MVC Pattern

- **Controller** (`controller.py`): Single instance. Wraps `proton-vpn-api-core` with methods like `login()`, `connect_to_server()`, `disconnect()`, `save_settings()`. All I/O returns `concurrent.futures.Future`.
- **Views**: GTK4 widgets under `widgets/`. Communicate via GObject signals and the observer pattern (`SettingsWatchers`).
- **No formal Model layer**: Data comes from `proton-vpn-api-core` (server lists, settings, account data).

### Widget Hierarchy

```
App (Gtk.Application)
└── MainWindow (Gtk.ApplicationWindow, 450x650)
    └── MainWidget (Gtk.Overlay)
        ├── LoginWidget | VPNWidget (switched based on auth state)
        ├── NotificationBar
        └── OverlayWidget (loading spinners)
```

Key widget areas:
- `widgets/login/` - Auth flow including 2FA (TOTP + FIDO2 security keys)
- `widgets/vpn/` - Server list, quick connect, connection status, search, port forwarding
- `widgets/vpn/serverlist/city_view/` - Expandable country→city→server tree using `ExpandableRow`
- `widgets/headerbar/menu/settings/` - Settings window with reusable base widgets (`ToggleWidget`, `ComboboxWidget`, `EntryWidget` in `common.py`)
- `widgets/main/tray_indicator.py` - System tray via GNOME AppIndicator extensions (D-Bus detection)

### Async/Threading Model

- `AsyncExecutor` (`utils/executor.py`): Runs asyncio event loop in a dedicated thread. `submit()` handles both coroutines and callables.
- GTK thread safety: Use `GLib.idle_add(callback)` to marshal results back to GTK main loop.
- Helper utilities in `utils/glib.py`: `run_once()`, `run_periodically()`, `add_done_callback()`.

### Settings System

- Settings use dot-notation paths: `"settings.features.netshield"`, `"app_configuration.tray_pinned_servers"`
- `SettingsWatchers` (observer pattern) notifies reactive widgets on changes
- Base widget `ReactiveSetting` has `on_settings_changed(settings)` hook
- Conflict detection in `conflicts.py` for mutually exclusive settings

### Background Services

`services/reconnector/` contains:
- `VPNReconnector` - orchestrates reconnection
- `NetworkMonitor` - NetworkManager D-Bus listener
- `VPNMonitor` - VPN state tracking
- `SessionMonitor` - screen lock detection

### Key Dependencies

- `proton-vpn-api-core` - Core VPN API (connection, server list, settings, session)
- `proton-core` - Proton authentication/session management
- `pygobject` / `pycairo` - GTK4 Python bindings
- `dbus-python` - D-Bus integration (tray, network monitoring)

### Styling

CSS files in `assets/style/`: `main.css` imports `dark_colours.css` and `dark_buttons.css`. Dark theme enforced. Widgets targeted by `set_name()` (e.g., `#vpn-widget`) and `add_css_class()`.

## File Locations

- App logs: `~/.cache/Proton/VPN/logs/`
- User settings: `~/.config/Proton/VPN/`
- Source: `proton/vpn/app/gtk/` (namespace package)
- Tests mirror source structure: `tests/unit/widgets/...` matches `proton/vpn/app/gtk/widgets/...`

## Versioning

Version is in `versions.yml` (top entry). To bump: add a new block at the top of `versions.yml` following the existing format, then run `scripts/build_packages.py` to regenerate debian/rpm changelogs.
