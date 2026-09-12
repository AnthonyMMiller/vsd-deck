import io
import json
from datetime import datetime
from http.client import IncompleteRead
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from vsd_deck.widgets import SystemMonitor, UNAVAILABLE, WeatherClient, WeatherError


class SystemMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.proc = Path(self.directory.name)
        self.monitor = SystemMonitor(proc_root=self.proc, disk_path=self.proc)
        self.write_cpu("100 0 100 800 0 0 0 0 25 0")
        (self.proc / "meminfo").write_text(
            "MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 600 kB\n"
        )
        disk = patch("vsd_deck.widgets.shutil.disk_usage")
        self.disk = disk.start()
        self.addCleanup(disk.stop)
        self.disk.return_value = SimpleNamespace(total=1000, used=250, free=750)

    def write_cpu(self, counters):
        (self.proc / "stat").write_text(f"cpu  {counters}\ncpu0 0 0 0 0\n")

    def test_first_sample_unknown_cpu_then_delta_excludes_guest_double_count(self):
        self.assertEqual(
            self.monitor.sample(), {"cpu": UNAVAILABLE, "memory": "40%", "disk": "25%"}
        )
        # 100 ticks of activity, 100 ticks idle; guest increases within user time.
        self.write_cpu("180 0 120 900 0 0 0 0 100 0")
        self.assertEqual(self.monitor.sample()["cpu"], "50%")

    def test_iowait_is_not_busy_cpu_time(self):
        self.monitor.sample()
        self.write_cpu("150 0 100 900 50 0 0 0 25 0")
        self.assertEqual(self.monitor.sample()["cpu"], "25%")

    def test_no_elapsed_time_and_reset_are_unavailable(self):
        self.monitor.sample()
        self.assertEqual(self.monitor.sample()["cpu"], UNAVAILABLE)
        self.write_cpu("1 0 1 8 0 0 0 0")
        self.assertEqual(self.monitor.sample()["cpu"], UNAVAILABLE)
        self.write_cpu("2 0 2 16 0 0 0 0")
        self.assertEqual(self.monitor.sample()["cpu"], "20%")

    def test_bad_cpu_counter_does_not_hide_memory_and_resets_baseline(self):
        self.monitor.sample()
        self.write_cpu("broken")
        self.assertEqual(self.monitor.sample()["cpu"], UNAVAILABLE)
        self.assertEqual(self.monitor.sample()["memory"], "40%")
        self.write_cpu("100 0 100 800 0 0 0 0")
        self.assertEqual(self.monitor.sample()["cpu"], UNAVAILABLE)

    def test_memory_requires_available_and_valid_counters(self):
        for contents in (
            "MemTotal: 1000 kB\nMemFree: 100 kB\n",
            "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
            "MemTotal: 1000 kB\nMemAvailable: -1 kB\n",
            "MemTotal: 1000 kB\nMemAvailable: 2000 kB\n",
            "MemTotal: invalid kB\nMemAvailable: 100 kB\n",
        ):
            with self.subTest(contents=contents):
                (self.proc / "meminfo").write_text(contents)
                self.assertEqual(self.monitor.sample()["memory"], UNAVAILABLE)

    def test_missing_proc_and_disk_permission_error_are_unavailable(self):
        (self.proc / "stat").unlink()
        (self.proc / "meminfo").unlink()
        self.disk.side_effect = PermissionError("blocked")
        self.assertEqual(self.monitor.sample(), dict.fromkeys(("cpu", "memory", "disk"), UNAVAILABLE))

    def test_disk_zero_size_is_unavailable(self):
        self.disk.return_value = SimpleNamespace(total=0, used=0, free=0)
        self.assertEqual(self.monitor.sample()["disk"], UNAVAILABLE)


class WeatherClientTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("vsd_deck.widgets.urlopen")
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = WeatherClient(timeout=4)

    def respond(self, payload):
        self.urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def test_constructor_does_not_contact_network(self):
        self.urlopen.assert_not_called()

    def test_current_weather_query_units_attribution_and_timestamp(self):
        self.respond(
            {"current": {"temperature_2m": 72.4, "weather_code": 2},
             "current_units": {"temperature_2m": "°F"}}
        )
        weather = self.client.fetch(33.45, -112.07, units="fahrenheit")
        self.assertEqual(weather["temperature"], "72°F")
        self.assertEqual(weather["summary"], "Partly cloudy")
        self.assertEqual(weather["attribution"], "Open-Meteo")
        self.assertEqual(weather["attribution_url"], "https://open-meteo.com/")
        self.assertIsNotNone(datetime.fromisoformat(weather["fetched_at"]).utcoffset())
        request = self.urlopen.call_args.args[0]
        split = urlsplit(request.full_url)
        self.assertEqual((split.scheme, split.netloc, split.path),
                         ("https", "api.open-meteo.com", "/v1/forecast"))
        self.assertEqual(parse_qs(split.query), {
            "latitude": ["33.45"], "longitude": ["-112.07"],
            "current": ["temperature_2m,weather_code"],
            "temperature_unit": ["fahrenheit"], "timezone": ["GMT"],
            "forecast_days": ["1"],
        })
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], 4)

    def test_celsius_and_clear_sky_code_zero(self):
        self.respond({"current": {"temperature_2m": -5.2, "weather_code": 0}})
        weather = self.client.fetch(0, 0)
        self.assertEqual(weather["temperature"], "-5°C")
        self.assertEqual(weather["summary"], "Clear sky")

    def test_future_code_is_explicitly_unknown(self):
        self.respond({"current": {"temperature_2m": 20, "weather_code": 100}})
        self.assertEqual(self.client.fetch(90, 180)["summary"], "Unknown conditions")

    def test_invalid_configuration_never_contacts_network(self):
        for latitude, longitude, units in (
            (91, 0, "celsius"), (-91, 0, "celsius"),
            (0, 181, "celsius"), (0, -181, "celsius"),
            (float("nan"), 0, "celsius"), (0, float("inf"), "celsius"),
            (None, 0, "celsius"), (True, 0, "celsius"),
            ("1&latitude=20", 0, "celsius"), (0, 0, "kelvin"),
        ):
            with self.subTest(latitude=latitude, longitude=longitude, units=units):
                with self.assertRaises(ValueError):
                    self.client.fetch(latitude, longitude, units)
        self.urlopen.assert_not_called()

    def test_timeout_must_be_positive_and_finite(self):
        for value in (0, -1, float("inf"), float("nan"), None, True, "4"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WeatherClient(timeout=value)

    def test_network_failures_have_a_consistent_error(self):
        for failure in (URLError("offline"), TimeoutError(), OSError(), IncompleteRead(b"")):
            with self.subTest(failure=failure):
                self.urlopen.side_effect = failure
                with self.assertRaisesRegex(WeatherError, "reach Open-Meteo"):
                    self.client.fetch(0, 0)

    def test_malformed_or_missing_weather_is_not_reported_as_clear(self):
        for payload in (
            [], {}, {"error": True, "reason": "bad request"}, {"current": []},
            {"current": {}}, {"current": {"temperature_2m": None, "weather_code": 0}},
            {"current": {"temperature_2m": "20", "weather_code": 0}},
            {"current": {"temperature_2m": float("nan"), "weather_code": 0}},
            {"current": {"temperature_2m": True, "weather_code": 0}},
            {"current": {"temperature_2m": 20, "weather_code": None}},
            {"current": {"temperature_2m": 20, "weather_code": 1.5}},
            {"current": {"temperature_2m": 20, "weather_code": False}},
            {"current": {"temperature_2m": 20, "weather_code": -1}},
            {"current": {"temperature_2m": 20, "weather_code": 0},
             "current_units": {"temperature_2m": "°F"}},
        ):
            with self.subTest(payload=payload):
                self.respond(payload)
                with self.assertRaisesRegex(WeatherError, "invalid weather data"):
                    self.client.fetch(0, 0)

    def test_non_json_response(self):
        self.urlopen.return_value = io.BytesIO(b"<html>upstream unavailable</html>")
        with self.assertRaises(WeatherError):
            self.client.fetch(0, 0)

    def test_oversized_response(self):
        self.urlopen.return_value = io.BytesIO(b" " * (WeatherClient.MAX_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(WeatherError, "oversized"):
            self.client.fetch(0, 0)


if __name__ == "__main__":
    unittest.main()
