"""Exercise actual Qt widgets and persistence while keeping actions dry-run."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QPA_PLATFORMTHEME"] = "basic"

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from vsd_deck.app import MainWindow
from vsd_deck import config


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "profiles.json"
        self.window = MainWindow(self.path, simulate=True, start_device=False)

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        self.temp.cleanup()

    def test_edit_apply_save_and_reload_profile(self):
        self.window.buttons["key:6"].click()
        self.window.label_edit.setText("My audio")
        self.window.apply_binding()
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.window.profile["bindings"]["key:6"]["label"], "My audio")
        self.assertTrue(self.window.save())
        loaded = config.load(self.path)
        self.assertEqual(loaded["profiles"][0]["bindings"]["key:6"]["label"], "My audio")
        self.assertFalse(self.window.dirty)

    def test_simulator_never_executes_actions(self):
        with patch.object(self.window.runner, "execute") as execute:
            self.window.dispatch("key:1")
            self.window.dispatch("knob:1:right")
        execute.assert_not_called()
        self.assertIn("Simulated knob:1:right", self.window.activity.toPlainText())

    def test_profile_switch_key_and_swipe(self):
        self.window.dispatch("key:10")
        self.assertEqual(self.window.profile["name"], "Resolve")
        self.window.dispatch("swipe:right")
        self.assertEqual(self.window.profile["name"], "Desktop")

    def test_bad_edit_keeps_previous_binding(self):
        before = self.window.profile["bindings"]["key:1"].copy()
        self.window.color_edit.setText("oops")
        with patch("vsd_deck.app.QMessageBox.warning") as warning:
            self.window.apply_binding()
        warning.assert_called_once()
        self.assertEqual(self.window.profile["bindings"]["key:1"], before)

    def test_missing_image_falls_back_and_all_keys_have_icons(self):
        self.window.profile["bindings"]["key:1"]["image"] = "/nonexistent/image.png"
        self.window.refresh_tiles()
        for key in range(1, 15):
            self.assertFalse(self.window.buttons[f"key:{key}"].icon().isNull())
        self.assertIn("Image error", self.window.buttons["key:1"].toolTip())

    def test_stale_weather_request_cannot_replace_new_location(self):
        self.window.weather_generation = 2
        with patch.object(self.window, "fetch_weather") as fetch:
            self.window.weather_result((1, {"temperature": "25°C"}, None))
        self.assertIsNone(self.window.weather_data)
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
