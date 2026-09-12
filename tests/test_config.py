"""Persistence and schema boundaries for user-edited profile JSON."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vsd_deck import config


class ConfigTests(unittest.TestCase):
    def test_defaults_cover_every_control_and_use_independent_bindings(self):
        data = config.validate(config.default_config())
        for profile in data["profiles"]:
            self.assertEqual(set(profile["bindings"]), set(config.CONTROLS))
        desktop, resolve = data["profiles"]
        desktop["bindings"]["key:11"]["label"] = "Changed"
        self.assertEqual(resolve["bindings"]["key:11"]["label"], "CPU")
        self.assertEqual(config.default_config()["profiles"][0]["bindings"]["key:11"]["label"], "CPU")

    def test_missing_file_uses_defaults_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new" / "profiles.json"
            data = config.load(path)
            self.assertEqual(data["active_profile"], "Desktop")
            self.assertFalse(path.parent.exists())

    def test_saved_configuration_round_trips_unicode(self):
        data = config.default_config()
        data["profiles"][0]["bindings"]["key:1"]["label"] = "音量 • Main"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            config.save(path, data)
            self.assertEqual(config.load(path), data)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_atomic_replace_preserves_original_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            original = config.default_config()
            config.save(path, original)
            previous_bytes = path.read_bytes()
            changed = copy.deepcopy(original)
            changed["brightness"] = 20
            with patch("vsd_deck.config.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    config.save(path, changed)
            self.assertEqual(path.read_bytes(), previous_bytes)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_save_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            config.save(path, config.default_config())
            previous_bytes = path.read_bytes()
            with self.assertRaises(ValueError):
                config.save(path, {"version": 999})
            self.assertEqual(path.read_bytes(), previous_bytes)

    def test_loaded_profile_members_must_be_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            for profile in (None, [], "Desktop", 42, True):
                data = config.default_config()
                data["profiles"] = [profile]
                path.write_text(json.dumps(data))
                with self.subTest(profile=profile), self.assertRaises(ValueError):
                    config.load(path)

    def test_bindings_require_fields_read_by_editor(self):
        for missing in ("label", "value"):
            data = config.default_config()
            del data["profiles"][0]["bindings"]["key:1"][missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                config.validate(data)

    def test_weather_requires_coordinate_fields_even_when_disabled(self):
        data = config.default_config()
        data["weather"] = {"units": "celsius"}
        with self.assertRaises(ValueError):
            config.validate(data)

    def test_invalid_nested_binding_shapes_are_rejected(self):
        for binding in (None, [], "noop", {"type": "bogus"}, {"type": "noop", "label": [], "value": ""}):
            data = config.default_config()
            data["profiles"][0]["bindings"]["key:1"] = binding
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                config.validate(data)

    def test_duplicate_names_and_missing_active_profile_are_rejected(self):
        data = config.default_config()
        data["profiles"][1]["name"] = data["profiles"][0]["name"]
        with self.assertRaises(ValueError):
            config.validate(data)
        data = config.default_config()
        data["active_profile"] = "Missing"
        with self.assertRaises(ValueError):
            config.validate(data)

    def test_boolean_brightness_and_invalid_coordinates_are_rejected(self):
        data = config.default_config()
        data["brightness"] = True
        with self.assertRaises(ValueError):
            config.validate(data)
        for latitude, longitude in ((True, 0), (91, 0), (0, -181), (float("nan"), 0), (0, float("inf")), (None, 0)):
            data = config.default_config()
            data["weather"].update(latitude=latitude, longitude=longitude)
            with self.subTest(latitude=latitude, longitude=longitude), self.assertRaises(ValueError):
                config.validate(data)


if __name__ == "__main__":
    unittest.main()
