"""Small data providers for deck widgets, with no third-party dependencies.

Weather requests only happen when ``fetch`` is explicitly called with a location.
The application should call it in a background worker and cache successful results.
API reference: https://open-meteo.com/en/docs
Linux counters: https://docs.kernel.org/filesystems/proc.html
"""

from __future__ import annotations

from datetime import datetime, timezone
from http.client import HTTPException
import json
import math
from pathlib import Path
import shutil
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


UNAVAILABLE = "—"


class SystemMonitor:
    """Read CPU activity, RAM usage, and filesystem usage on Linux.

    CPU is busy time as a percentage of elapsed aggregate CPU time, excluding
    idle and I/O wait. The first sample establishes a baseline and returns "—".
    Memory uses MemAvailable, so reclaimable cache is treated as available RAM.
    Disk is the used percentage of the filesystem containing ``disk_path``.
    A failed metric is unavailable without preventing the other two readings.
    """

    def __init__(self, proc_root: str | Path = "/proc", disk_path: str | Path = "/"):
        self.proc_root = Path(proc_root)
        self.disk_path = disk_path
        self._previous_cpu: tuple[int, int] | None = None

    def sample(self) -> dict[str, str]:
        return {"cpu": self._cpu(), "memory": self._memory(), "disk": self._disk()}

    def _cpu(self) -> str:
        try:
            with (self.proc_root / "stat").open(encoding="ascii") as stream:
                fields = stream.readline().split()
            if not fields or fields[0] != "cpu" or len(fields) < 5:
                raise ValueError("Missing aggregate CPU counters")
            # guest and guest_nice are already included in user and nice.
            counters = [int(value) for value in fields[1:9]]
            if any(value < 0 for value in counters):
                raise ValueError("Negative CPU counter")
            total = sum(counters)
            idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
        except (OSError, ValueError, UnicodeError):
            self._previous_cpu = None
            return UNAVAILABLE

        previous = self._previous_cpu
        self._previous_cpu = (total, idle)
        if previous is None:
            return UNAVAILABLE
        elapsed = total - previous[0]
        idle_delta = idle - previous[1]
        # Counters can reset after reboot or CPU hotplug; iowait can decrease.
        if elapsed <= 0 or idle_delta < 0 or idle_delta > elapsed:
            return UNAVAILABLE
        return f"{100 * (elapsed - idle_delta) / elapsed:.0f}%"

    def _memory(self) -> str:
        try:
            values = {}
            for line in (self.proc_root / "meminfo").read_text(encoding="ascii").splitlines():
                key, _, remainder = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    fields = remainder.split()
                    if len(fields) != 2 or fields[1] != "kB":
                        raise ValueError("Invalid memory counter")
                    values[key] = int(fields[0])
            total, available = values["MemTotal"], values["MemAvailable"]
            if total <= 0 or not 0 <= available <= total:
                raise ValueError("Invalid memory usage")
            return f"{100 * (total - available) / total:.0f}%"
        except (OSError, ValueError, KeyError, UnicodeError):
            return UNAVAILABLE

    def _disk(self) -> str:
        try:
            usage = shutil.disk_usage(self.disk_path)
            if usage.total <= 0 or not 0 <= usage.used <= usage.total:
                return UNAVAILABLE
            return f"{100 * usage.used / usage.total:.0f}%"
        except OSError:
            return UNAVAILABLE


class WeatherError(RuntimeError):
    """Weather could not be loaded; callers can display an unavailable state."""


# WMO codes supported by Open-Meteo. These short labels fit small deck screens.
WEATHER_SUMMARIES = {
    0: "Clear sky",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Light freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Rain showers",
    82: "Heavy showers",
    85: "Light snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherClient:
    """Fetch current conditions for user-provided coordinates from Open-Meteo.

    ``fetched_at`` is an ISO 8601 UTC timestamp, recorded after a successful
    response. Requests use a fixed HTTPS endpoint, a bounded response size,
    and a timeout. No location discovery or requests happen in the constructor.
    Invalid settings raise ValueError; network/API/response errors raise
    WeatherError. Display ``attribution`` with a link to ``attribution_url``.
    """

    ENDPOINT = "https://api.open-meteo.com/v1/forecast"
    MAX_RESPONSE_BYTES = 1_048_576

    def __init__(self, timeout: float = 8.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("Weather timeout must be a positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Weather timeout must be a positive number")
        self.timeout = timeout

    def fetch(
        self, latitude: float, longitude: float, units: str = "celsius"
    ) -> dict[str, str]:
        latitude = self._coordinate(latitude, "Latitude", 90)
        longitude = self._coordinate(longitude, "Longitude", 180)
        if units not in ("celsius", "fahrenheit"):
            raise ValueError("Temperature units must be celsius or fahrenheit")
        query = urlencode(
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,weather_code",
                "temperature_unit": units,
                "timezone": "GMT",
                "forecast_days": 1,
            }
        )
        request = Request(
            f"{self.ENDPOINT}?{query}",
            headers={"Accept": "application/json", "User-Agent": "VSD-Deck-Linux/0.1"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except (URLError, OSError, HTTPException) as exc:
            raise WeatherError("Unable to reach Open-Meteo. Check your connection.") from exc
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise WeatherError("Open-Meteo returned an oversized response.")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("error"):
                raise ValueError("API error")
            current = payload["current"]
            if not isinstance(current, dict):
                raise ValueError("Missing current weather")
            temperature = current["temperature_2m"]
            code = current["weather_code"]
            if (
                isinstance(temperature, bool)
                or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature)
            ):
                raise ValueError("Invalid temperature")
            if isinstance(code, bool) or not isinstance(code, int) or code < 0:
                raise ValueError("Invalid weather code")
            symbol = "°F" if units == "fahrenheit" else "°C"
            response_units = payload.get("current_units", {})
            if not isinstance(response_units, dict):
                raise ValueError("Invalid units")
            if response_units.get("temperature_2m", symbol) != symbol:
                raise ValueError("Unexpected temperature units")
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
            raise WeatherError("Open-Meteo returned invalid weather data.") from exc
        return {
            "temperature": f"{temperature:.0f}{symbol}",
            "summary": WEATHER_SUMMARIES.get(code, "Unknown conditions"),
            "attribution": "Open-Meteo",
            "attribution_url": "https://open-meteo.com/",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @staticmethod
    def _coordinate(value: float, label: str, limit: int) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be between {-limit} and {limit}")
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{label} must be between {-limit} and {limit}") from exc
        if not math.isfinite(result) or not -limit <= result <= limit:
            raise ValueError(f"{label} must be between {-limit} and {limit}")
        return result
