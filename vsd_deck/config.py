"""Versioned, atomic profile persistence. Loading never executes actions."""
from __future__ import annotations

import copy
import json
import os
import shlex
from pathlib import Path
import tempfile

CONTROLS = [f"key:{i}" for i in range(1, 15)] + [
    f"knob:{i}:{event}" for i in range(1, 5) for event in ("left", "press", "right")
] + ["swipe:left", "swipe:right"]
ACTION_TYPES = ("noop", "command", "url", "shortcut", "media", "volume", "profile", "monitor", "weather")


def binding(label="Empty", kind="noop", value="", color="#247a89", **extra):
    return {"label": label, "type": kind, "value": value, "color": color, "image": "", **extra}


def default_config():
    desktop = [
        binding("Terminal", "command", "xdg-terminal-exec"),
        binding("Files", "command", "xdg-open " + shlex.quote(str(Path.home()))),
        binding("Browser", "url", "https://www.google.com"),
        binding("Play / Pause", "media", "play-pause"),
        binding("Next track", "media", "next"),
        binding("Volume −", "volume", "down"),
        binding("Mute", "volume", "mute"),
        binding("Volume +", "volume", "up"),
        binding("Mic mute", "volume", "mic-mute"),
        binding("Resolve", "profile", "Resolve", "#9861db"),
        binding("CPU", "monitor", "cpu", "#527ee8"),
        binding("Memory", "monitor", "memory", "#527ee8"),
        binding("Disk", "monitor", "disk", "#527ee8"),
        binding("Weather", "weather", "", "#b67d32"),
    ]
    resolve = [binding(label, "shortcut", key, "#9861db", target="resolve") for label, key in [
        ("Play / Pause", "space"), ("Reverse", "j"), ("Stop", "k"), ("Forward", "l"),
        ("Save", "Ctrl+s"), ("Mark in", "i"), ("Mark out", "o"), ("Undo", "Ctrl+z"),
        ("Redo", "Ctrl+Shift+z"),
    ]] + [binding("Desktop", "profile", "Desktop"), *copy.deepcopy(desktop[10:])]
    profiles = []
    for name, keys in (("Desktop", desktop), ("Resolve", resolve)):
        binds = {control: binding() for control in CONTROLS}
        binds.update({f"key:{i}": item for i, item in enumerate(keys, 1)})
        binds.update({
            "knob:1:left": binding("Volume −", "volume", "down"),
            "knob:1:press": binding("Mute", "volume", "mute"),
            "knob:1:right": binding("Volume +", "volume", "up"),
            "knob:2:left": binding("Previous track", "media", "previous"),
            "knob:2:press": binding("Play / Pause", "media", "play-pause"),
            "knob:2:right": binding("Next track", "media", "next"),
            "swipe:left": binding("Previous profile", "profile", "previous"),
            "swipe:right": binding("Next profile", "profile", "next"),
        })
        profiles.append({"name": name, "bindings": binds})
    return {"version": 1, "active_profile": "Desktop", "brightness": 65,
            "weather": {"latitude": None, "longitude": None, "units": "celsius"},
            "profiles": profiles}


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "vsd-deck" / "profiles.json"


def validate(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unsupported profile file version")
    if not isinstance(data.get("profiles"), list) or not data["profiles"]:
        raise ValueError("At least one profile is required")
    names = []
    for profile in data["profiles"]:
        if not isinstance(profile, dict):
            raise ValueError("Each profile must be an object")
        name = profile.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Profile names must be nonempty and unique")
        names.append(name)
        binds = profile.get("bindings")
        if not isinstance(binds, dict):
            raise ValueError("Missing profile bindings")
        for control, item in binds.items():
            if control not in CONTROLS or not isinstance(item, dict) or item.get("type") not in ACTION_TYPES:
                raise ValueError(f"Invalid binding: {control}")
            for field in ("label", "value", "image", "color"):
                if not isinstance(item.get(field), str):
                    raise ValueError(f"Invalid {field} for {control}")
            color = item.get("color", "#247a89")
            if len(color) != 7 or color[0] != "#" or any(c not in "0123456789abcdefABCDEF" for c in color[1:]):
                raise ValueError("Tile color must be #RRGGBB")
    if data.get("active_profile") not in names:
        raise ValueError("Active profile does not exist")
    if type(data.get("brightness")) is not int or not 0 <= data["brightness"] <= 100:
        raise ValueError("Brightness must be 0–100")
    weather = data.get("weather", {})
    if not isinstance(weather, dict) or weather.get("units") not in ("celsius", "fahrenheit"):
        raise ValueError("Invalid weather settings")
    for field, limit in (("latitude", 90), ("longitude", 180)):
        if field not in weather:
            raise ValueError(f"Missing weather {field}")
        value = weather.get(field)
        if value is not None and (type(value) not in (int, float) or not -limit <= value <= limit):
            raise ValueError(f"Invalid {field}")
    if (weather.get("latitude") is None) != (weather.get("longitude") is None):
        raise ValueError("Set both weather coordinates")
    return data


def load(path):
    path = Path(path)
    if not path.exists():
        return default_config()
    return validate(json.loads(path.read_text()))


def save(path, data):
    validate(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".profiles-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
