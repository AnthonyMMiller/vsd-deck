"""Qt desktop editor and event coordinator."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import tempfile

from PySide6.QtCore import Qt, Signal, QObject, QTimer, QSize
from PySide6.QtGui import QIcon, QPixmap, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QLineEdit, QFormLayout, QFrame,
    QPlainTextEdit, QFileDialog, QMessageBox, QSlider, QColorDialog, QDialog,
    QDialogButtonBox, QCheckBox, QInputDialog, QSystemTrayIcon, QMenu,
)

from . import config
from .actions import ActionRunner
from .device import DeckDevice
from .render import render_tile
from .widgets import SystemMonitor, WeatherClient


STYLE = """
QWidget { background: #101620; color: #dae6f3; font-family: 'DejaVu Sans'; font-size: 13px; }
QMainWindow { background: #101620; }
QFrame#panel { background: #18212e; border: 1px solid #2c394b; border-radius: 14px; }
QFrame#panel QLabel { background: transparent; }
QLabel#title { font-size: 27px; font-weight: 700; color: #f4f8ff; }
QLabel#subtitle { color: #91a4bb; }
QLabel#section { color: #91a4bb; font-size: 11px; font-weight: 700; }
QPushButton { background: #243247; border: 1px solid #384b63; border-radius: 7px; padding: 9px 13px; }
QPushButton:hover { background: #30425b; border-color: #7cbab8; }
QPushButton:checked { border: 2px solid #81e1c2; background: #2b4b52; }
QPushButton#primary { background: #9be1ca; color: #102623; font-weight: 700; border: none; }
QPushButton#tile { padding: 3px; }
QLineEdit, QComboBox, QPlainTextEdit { background: #111b28; border: 1px solid #34465d; border-radius: 6px; padding: 7px; }
QComboBox QAbstractItemView { background: #18212e; selection-background-color: #345268; }
QPlainTextEdit { color: #a6b7cb; font-family: monospace; font-size: 11px; }
QSlider::groove:horizontal { height: 5px; background: #34465d; border-radius: 2px; }
QSlider::handle:horizontal { background: #9be1ca; width: 14px; margin: -5px 0; border-radius: 7px; }
"""

TYPE_NAMES = {
    "noop": "Unassigned", "command": "Launch program", "url": "Open website",
    "shortcut": "Keyboard shortcut", "media": "Media control", "volume": "Audio control",
    "profile": "Switch profile", "monitor": "System monitor", "weather": "Weather",
}
HINTS = {
    "noop": "This control has no action.",
    "command": 'Program and arguments, e.g. xdg-terminal-exec or flatpak run org.example.App. Quote paths containing spaces. Shell operators are not evaluated.',
    "url": "A complete https:// or http:// address.",
    "shortcut": "Examples: Ctrl+Shift+s, space, Alt+Tab. Sent to the focused application. Resolve actions require Resolve to be focused.",
    "media": "play-pause, next, previous, or stop. Requires playerctl and an MPRIS player.",
    "volume": "up, down, mute, or mic-mute. Uses the default PipeWire output or microphone.",
    "profile": "Enter a profile name, next, or previous.",
    "monitor": "cpu, memory, or disk. Updates every 3 seconds. Disk measures your home filesystem.",
    "weather": "Set coordinates in Weather settings. Updates every 15 minutes; no account or API key required. Data by Open-Meteo.",
}


class Bridge(QObject):
    event = Signal(str)
    status = Signal(str)
    message = Signal(str)
    weather = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, path, simulate=False, start_device=True):
        super().__init__()
        self.path = Path(path)
        self.data = config.load(self.path)
        self.simulate = simulate
        self.dirty = False
        self.selected = "key:1"
        self.cache = tempfile.TemporaryDirectory(prefix="vsd-deck-")
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deck-action")
        self.weather_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deck-weather")
        self.pending_actions = 0
        self.bridge = Bridge()
        self.bridge.event.connect(self.dispatch)
        self.bridge.status.connect(self.device_status)
        self.bridge.message.connect(self.log)
        self.bridge.weather.connect(self.weather_result)
        self.device = DeckDevice(self.bridge.event.emit, self.bridge.status.emit, simulate=simulate)
        self.runner = ActionRunner()
        self.monitor = SystemMonitor(disk_path=Path.home())
        self.stats = self.monitor.sample()
        self.weather_data = None
        self.weather_error = ""
        self.weather_pending = False
        self.weather_generation = 0
        self.buttons = {}
        self.tile_paths = {}
        self.setWindowTitle("VSD Deck · N4 Pro")
        self.resize(1190, 830)
        self.setStyleSheet(STYLE)
        self.build_ui()
        self.refresh_profiles()
        self.select_control("key:1")
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self)
            self.tray.setIcon(self.windowIcon())
            self.tray.setToolTip("VSD Deck · N4 Pro")
            menu = QMenu()
            menu.addAction("Show controls", self.showNormal)
            menu.addAction("Quit", self.close)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda _: self.showNormal())
            self.tray.show()
        self.stats_timer = QTimer(self)
        self.stats_timer.timeout.connect(self.update_stats)
        self.stats_timer.start(3000)
        self.weather_timer = QTimer(self)
        self.weather_timer.timeout.connect(self.fetch_weather)
        self.weather_timer.start(15 * 60 * 1000)
        self.device.set_brightness(self.data["brightness"])
        if start_device:
            self.device.start()
        self.fetch_weather()
        self.log("Simulator: actions are logged without running commands." if simulate else "Ready. Physical controls execute the active profile.")

    @property
    def profile(self):
        return next(p for p in self.data["profiles"] if p["name"] == self.data["active_profile"])

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(25, 22, 25, 18)
        outer.setSpacing(17)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel("VSD Deck")
        title.setObjectName("title")
        heading.addWidget(title)
        subtitle = QLabel("N4 PRO  /  YOUR LINUX CONTROL SURFACE")
        subtitle.setObjectName("subtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.status = QLabel("Simulator" if self.simulate else "Connecting…")
        header.addWidget(self.status)
        reconnect = QPushButton("Reconnect")
        reconnect.clicked.connect(lambda: self.device.reconnect())
        header.addWidget(reconnect)
        self.save_button = QPushButton("Save profiles")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        header.addWidget(self.save_button)
        outer.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("PROFILE"))
        self.profile_picker = QComboBox()
        self.profile_picker.setMinimumWidth(190)
        self.profile_picker.currentTextChanged.connect(self.change_profile)
        toolbar.addWidget(self.profile_picker)
        new = QPushButton("Duplicate profile")
        new.clicked.connect(self.duplicate_profile)
        toolbar.addWidget(new)
        toolbar.addStretch()
        weather = QPushButton("Weather settings")
        weather.clicked.connect(self.weather_settings)
        toolbar.addWidget(weather)
        diag = QPushButton("Diagnostics")
        diag.clicked.connect(self.diagnostics)
        toolbar.addWidget(diag)
        hide = QPushButton("Hide to tray")
        hide.clicked.connect(self.hide_to_tray)
        toolbar.addWidget(hide)
        outer.addLayout(toolbar)

        content = QHBoxLayout()
        content.setSpacing(20)
        panel = QFrame()
        panel.setObjectName("panel")
        deck = QVBoxLayout(panel)
        deck.setContentsMargins(19, 18, 19, 18)
        deck.addWidget(self.section("DISPLAY KEYS · SELECT TO EDIT"))
        grid = QGridLayout()
        grid.setSpacing(9)
        for key in range(1, 11):
            button = self.control_button(f"key:{key}", tile=True)
            button.setFixedSize(107, 113)
            button.setIconSize(QSize(97, 97))
            grid.addWidget(button, (key-1)//5, (key-1)%5)
        deck.addLayout(grid)
        deck.addSpacing(10)
        deck.addWidget(self.section("TOUCH STRIP"))
        strip = QHBoxLayout()
        for key in range(11, 15):
            button = self.control_button(f"key:{key}", tile=True)
            button.setFixedSize(137, 91)
            button.setIconSize(QSize(127, 81))
            strip.addWidget(button)
        deck.addLayout(strip)
        deck.addSpacing(10)
        deck.addWidget(self.section("ROTARY CONTROLS · TURN LEFT / PRESS / TURN RIGHT"))
        knobs = QGridLayout()
        for knob in range(1, 5):
            label = QLabel(f"KNOB {knob}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            knobs.addWidget(label, 0, knob-1)
            row = QHBoxLayout()
            for event, text in (("left", "↶"), ("press", "●"), ("right", "↷")):
                button = self.control_button(f"knob:{knob}:{event}")
                button.setText(text)
                button.setFixedWidth(42)
                row.addWidget(button)
            knobs.addLayout(row, 1, knob-1)
        deck.addLayout(knobs)
        swipes = QHBoxLayout()
        for event, text in (("left", "← Swipe left"), ("right", "Swipe right →")):
            button = self.control_button(f"swipe:{event}")
            button.setText(text)
            swipes.addWidget(button)
        deck.addLayout(swipes)
        deck.addStretch()
        brightness = QHBoxLayout()
        brightness.addWidget(QLabel("Display brightness"))
        self.brightness = QSlider(Qt.Orientation.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness.setValue(self.data["brightness"])
        self.brightness.valueChanged.connect(self.set_brightness)
        brightness.addWidget(self.brightness)
        deck.addLayout(brightness)
        content.addWidget(panel)

        editor_panel = QFrame()
        editor_panel.setObjectName("panel")
        editor = QVBoxLayout(editor_panel)
        editor.setContentsMargins(20, 18, 20, 18)
        self.control_title = QLabel()
        self.control_title.setObjectName("section")
        editor.addWidget(self.control_title)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(115)
        editor.addWidget(self.preview)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.label_edit = QLineEdit()
        self.type_edit = QComboBox()
        for kind, name in TYPE_NAMES.items():
            self.type_edit.addItem(name, kind)
        self.type_edit.currentIndexChanged.connect(self.update_hint)
        self.value_edit = QLineEdit()
        self.image_edit = QLineEdit()
        image_row = QHBoxLayout()
        image_row.addWidget(self.image_edit)
        browse = QPushButton("…")
        browse.setFixedWidth(35)
        browse.clicked.connect(self.choose_image)
        image_row.addWidget(browse)
        self.color_edit = QLineEdit()
        color_row = QHBoxLayout()
        color_row.addWidget(self.color_edit)
        color = QPushButton("Pick")
        color.clicked.connect(self.choose_color)
        color_row.addWidget(color)
        form.addRow("Label", self.label_edit)
        form.addRow("Action", self.type_edit)
        form.addRow("Value", self.value_edit)
        form.addRow("Image", image_row)
        form.addRow("Color", color_row)
        editor.addLayout(form)
        self.resolve_only = QCheckBox("Only send shortcut when Resolve is focused")
        editor.addWidget(self.resolve_only)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setMinimumHeight(75)
        self.hint.setObjectName("subtitle")
        editor.addWidget(self.hint)
        apply = QPushButton("Apply to control")
        apply.setObjectName("primary")
        apply.clicked.connect(self.apply_binding)
        editor.addWidget(apply)
        test = QPushButton("Simulate selected control" if self.simulate else "Run selected control")
        test.clicked.connect(lambda: self.dispatch(self.selected))
        editor.addWidget(test)
        note = QLabel("Apply updates this session. Save profiles keeps your changes for next time.")
        note.setWordWrap(True)
        note.setObjectName("subtitle")
        editor.addWidget(note)
        editor.addStretch()
        content.addWidget(editor_panel, 1)
        outer.addLayout(content, 1)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(100)
        self.activity.setFixedHeight(90)
        outer.addWidget(self.activity)

    def section(self, text):
        widget = QLabel(text)
        widget.setObjectName("section")
        return widget

    def control_button(self, control, tile=False):
        button = QPushButton()
        button.setCheckable(True)
        if tile:
            button.setObjectName("tile")
        button.clicked.connect(lambda: self.select_control(control))
        self.buttons[control] = button
        return button

    def refresh_profiles(self):
        self.profile_picker.blockSignals(True)
        self.profile_picker.clear()
        self.profile_picker.addItems([p["name"] for p in self.data["profiles"]])
        self.profile_picker.setCurrentText(self.data["active_profile"])
        self.profile_picker.blockSignals(False)
        self.refresh_tiles()

    def change_profile(self, name):
        if name and name != self.data["active_profile"]:
            self.data["active_profile"] = name
            self.mark_dirty()
            self.refresh_tiles()
            self.select_control(self.selected)
            self.log(f"Profile: {name}")

    def duplicate_profile(self):
        name, ok = QInputDialog.getText(self, "Duplicate profile", "New profile name")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in (p["name"] for p in self.data["profiles"]):
            QMessageBox.warning(self, "Profile exists", "Choose a unique profile name.")
            return
        profile = copy.deepcopy(self.profile)
        profile["name"] = name
        self.data["profiles"].append(profile)
        self.data["active_profile"] = name
        self.mark_dirty()
        self.refresh_profiles()
        self.select_control(self.selected)

    def detail(self, item):
        if item["type"] == "monitor":
            return self.stats.get(item["value"], "—")
        if item["type"] == "weather":
            if self.weather_data:
                return self.weather_data["temperature"] + (" *" if self.weather_error else "")
            return "Offline" if self.weather_error else "Set location"
        return ""

    def refresh_tiles(self):
        changed = {}
        for control, button in self.buttons.items():
            item = self.profile["bindings"].get(control, config.binding())
            button.setToolTip(f"{control} · {item['label']}\n{TYPE_NAMES[item['type']]}: {item['value']}")
            if control.startswith("key:"):
                key = int(control.split(":")[1])
                try:
                    path = render_tile(item, self.cache.name, key, self.detail(item))
                except (OSError, ValueError) as exc:
                    fallback = {**item, "image": ""}
                    path = render_tile(fallback, self.cache.name, key, "Image missing")
                    button.setToolTip(f"Image error: {exc}")
                if self.tile_paths.get(key) != path:
                    button.setIcon(QIcon(path))
                    self.tile_paths[key] = path
                    changed[key] = path
        if changed:
            self.device.set_images(changed)
        if 1 in self.tile_paths:
            self.setWindowIcon(QIcon(self.tile_paths[1]))
        if hasattr(self, "preview"):
            self.update_preview()

    def update_preview(self):
        if self.selected.startswith("key:"):
            path = self.tile_paths.get(int(self.selected.split(":")[1]))
            if path:
                self.preview.setPixmap(QPixmap(path).scaled(176, 100, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText(self.selected.replace(":", " · ").upper())

    def select_control(self, control):
        self.selected = control
        for name, button in self.buttons.items():
            button.setChecked(name == control)
        item = self.profile["bindings"].get(control, config.binding())
        self.control_title.setText(control.replace(":", " · ").upper())
        self.label_edit.setText(item["label"])
        self.type_edit.setCurrentIndex(self.type_edit.findData(item["type"]))
        self.value_edit.setText(item["value"])
        self.image_edit.setText(item.get("image", ""))
        self.color_edit.setText(item.get("color", "#247a89"))
        self.resolve_only.setChecked(item.get("target") == "resolve")
        self.update_hint()
        self.update_preview()

    def update_hint(self):
        if not hasattr(self, "hint"):
            return
        kind = self.type_edit.currentData()
        self.hint.setText(HINTS[kind])
        self.resolve_only.setEnabled(kind == "shortcut")
        self.value_edit.setEnabled(kind not in ("noop", "weather"))

    def apply_binding(self):
        kind = self.type_edit.currentData()
        item = config.binding(self.label_edit.text().strip() or "Untitled", kind,
                              self.value_edit.text().strip(), self.color_edit.text().strip())
        item["image"] = self.image_edit.text().strip()
        if kind == "shortcut" and self.resolve_only.isChecked():
            item["target"] = "resolve"
        candidate = copy.deepcopy(self.data)
        profile = next(p for p in candidate["profiles"] if p["name"] == candidate["active_profile"])
        profile["bindings"][self.selected] = item
        try:
            config.validate(candidate)
            if item["image"]:
                render_tile(item, self.cache.name, 1)
            if kind == "monitor" and item["value"] not in ("cpu", "memory", "disk"):
                raise ValueError("Monitor value must be cpu, memory, or disk")
            if kind == "profile" and item["value"] not in ("next", "previous", *(p["name"] for p in candidate["profiles"])):
                raise ValueError("Choose an existing profile name, next, or previous")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Check this control", str(exc))
            return
        self.data = candidate
        self.mark_dirty()
        self.refresh_tiles()
        self.log(f"Applied {self.selected}: {item['label']}")

    def choose_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose button image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            self.image_edit.setText(path)

    def choose_color(self):
        color = QColorDialog.getColor(QColor(self.color_edit.text()), self)
        if color.isValid():
            self.color_edit.setText(color.name())

    def mark_dirty(self):
        self.dirty = True
        self.save_button.setText("Save profiles •")

    def save(self):
        try:
            config.save(self.path, self.data)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return False
        self.dirty = False
        self.save_button.setText("Save profiles")
        self.log(f"Saved {self.path}")
        return True

    def dispatch(self, control):
        item = copy.deepcopy(self.profile["bindings"].get(control, config.binding()))
        if item["type"] == "profile":
            names = [p["name"] for p in self.data["profiles"]]
            name = item["value"]
            if name in ("next", "previous"):
                offset = 1 if name == "next" else -1
                name = names[(names.index(self.data["active_profile"])+offset) % len(names)]
            if name in names:
                self.profile_picker.setCurrentText(name)
            return
        if item["type"] in ("monitor", "weather"):
            text = str(self.weather_data or "Configure weather coordinates first") if item["type"] == "weather" else self.detail(item)
            self.log(f"{item['label']}: {text}")
            return
        if self.simulate:
            self.log(f"Simulated {control}: {item['type']} {item['value']}")
            return
        if self.pending_actions >= 16:
            self.log("Action queue full; skipping this event")
            return
        self.pending_actions += 1
        future = self.pool.submit(self.runner.execute, item)
        def complete(result):
            try:
                self.bridge.message.emit(result.result())
            except Exception as exc:
                self.bridge.message.emit(f"Action failed: {exc}")
            finally:
                self.bridge.message.emit("__action_complete__")
        future.add_done_callback(complete)

    def log(self, message):
        if message == "__action_complete__":
            self.pending_actions = max(0, self.pending_actions-1)
            return
        self.activity.appendPlainText(message)
        logging.getLogger("vsd_deck").info(message)

    def device_status(self, message):
        self.status.setText(message)
        self.status.setMaximumWidth(290)
        self.status.setWordWrap(True)
        self.log(message)

    def set_brightness(self, value):
        self.data["brightness"] = value
        self.device.set_brightness(value)
        self.mark_dirty()

    def update_stats(self):
        self.stats = self.monitor.sample()
        self.refresh_tiles()

    def fetch_weather(self):
        settings = self.data["weather"].copy()
        if settings["latitude"] is None or self.weather_pending:
            return
        self.weather_pending = True
        generation = self.weather_generation
        future = self.weather_pool.submit(WeatherClient().fetch, **settings)
        def complete(result):
            try:
                self.bridge.weather.emit((generation, result.result(), None))
            except Exception as exc:
                self.bridge.weather.emit((generation, None, str(exc)))
        future.add_done_callback(complete)

    def weather_result(self, result):
        generation, data, error = result
        self.weather_pending = False
        if generation != self.weather_generation:
            self.fetch_weather()
            return
        self.weather_error = error or ""
        if error:
            self.log(f"Weather unavailable: {error}. * marks the last successful reading.")
        else:
            self.weather_data = data
            self.log(f"Weather: {data['temperature']} · {data['summary']} · Data by Open-Meteo")
        self.refresh_tiles()

    def weather_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Weather · Open-Meteo")
        layout = QVBoxLayout(dialog)
        info = QLabel("Enter your location coordinates. Leave both blank to disable weather.\nCoordinates are sent to Open-Meteo only when enabled.")
        info.setWordWrap(True)
        layout.addWidget(info)
        form = QFormLayout()
        settings = self.data["weather"]
        latitude = QLineEdit("" if settings["latitude"] is None else str(settings["latitude"]))
        longitude = QLineEdit("" if settings["longitude"] is None else str(settings["longitude"]))
        units = QComboBox()
        units.addItems(["celsius", "fahrenheit"])
        units.setCurrentText(settings["units"])
        form.addRow("Latitude (−90 to 90)", latitude)
        form.addRow("Longitude (−180 to 180)", longitude)
        form.addRow("Units", units)
        layout.addLayout(form)
        attribution = QLabel('<a href="https://open-meteo.com/" style="color:#9be1ca">Weather data by Open-Meteo</a> · CC BY 4.0')
        attribution.setOpenExternalLinks(True)
        layout.addWidget(attribution)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            weather = {"latitude": float(latitude.text()) if latitude.text().strip() else None,
                       "longitude": float(longitude.text()) if longitude.text().strip() else None,
                       "units": units.currentText()}
            config.validate({**self.data, "weather": weather})
        except ValueError as exc:
            QMessageBox.warning(self, "Check coordinates", str(exc))
            return
        self.data["weather"] = weather
        self.weather_generation += 1
        self.weather_data = None
        self.weather_error = ""
        self.mark_dirty()
        self.refresh_tiles()
        self.fetch_weather()

    def diagnostics(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Diagnostics")
        dialog.resize(650, 470)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(self.runner.diagnostics(), indent=2) + f"\n\nProfiles: {self.path}\n\nDevice: {self.status.text()}")
        layout.addWidget(text)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def hide_to_tray(self):
        if self.tray:
            self.hide()
        else:
            self.showMinimized()

    def closeEvent(self, event):
        if self.dirty:
            choice = QMessageBox.question(self, "Unsaved profiles", "Save your profile changes before quitting?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            if choice == QMessageBox.StandardButton.Cancel or (choice == QMessageBox.StandardButton.Save and not self.save()):
                event.ignore()
                return
        self.stats_timer.stop()
        self.weather_timer.stop()
        self.device.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.weather_pool.shutdown(wait=False, cancel_futures=True)
        if self.tray:
            self.tray.hide()
        self.cache.cleanup()
        event.accept()
        QApplication.instance().quit()
