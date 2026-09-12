"""Execute configured desktop actions using Linux command-line interfaces.

Commands are argument lists, never shell scripts. Shortcut injection follows the
current display session: wtype on Wayland, xdotool on X11. Resolve shortcuts are
guarded by the focused window's application class, not its editable title.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
from collections.abc import Mapping
from urllib.parse import urlsplit


class ActionError(RuntimeError):
    """An action could not be validated or completed."""


_PACKAGES = {
    "wtype": "wtype",
    "xdotool": "xdotool",
    "playerctl": "playerctl",
    "wpctl": "wireplumber",
    "xdg-open": "xdg-utils",
    "hyprctl": "hyprland",
}
_MODIFIERS = {
    "ctrl": "ctrl", "control": "ctrl",
    "shift": "shift", "alt": "alt", "altgr": "altgr",
    "super": "logo", "win": "logo", "meta": "logo", "logo": "logo",
}
_KEY_ALIASES = {
    "enter": "Return", "return": "Return", "esc": "Escape",
    "escape": "Escape", "space": "space", "spacebar": "space",
    "tab": "Tab", "backspace": "BackSpace", "back_space": "BackSpace",
    "delete": "Delete", "del": "Delete", "insert": "Insert",
    "ins": "Insert", "home": "Home", "end": "End",
    "pageup": "Prior", "page_up": "Prior", "pgup": "Prior", "prior": "Prior",
    "pagedown": "Next", "page_down": "Next", "pgdn": "Next", "next": "Next",
    "left": "Left", "right": "Right", "up": "Up", "down": "Down",
    "print": "Print", "printscreen": "Print", "pause": "Pause", "menu": "Menu",
    "minus": "minus", "-": "minus", "equal": "equal", "=": "equal",
    "plus": "plus", "comma": "comma", ",": "comma",
    "period": "period", ".": "period", "slash": "slash", "/": "slash",
    "backslash": "backslash", "\\": "backslash",
    "semicolon": "semicolon", ";": "semicolon",
    "apostrophe": "apostrophe", "'": "apostrophe",
    "grave": "grave", "`": "grave",
    "bracketleft": "bracketleft", "[": "bracketleft",
    "bracketright": "bracketright", "]": "bracketright",
}
_RESOLVE_CLASSES = {"resolve", "davinciresolve", "davinci-resolve", "com.blackmagicdesign.resolve"}


def parse_shortcut(value: str) -> tuple[list[str], str]:
    """Parse one chord, e.g. Ctrl+Shift+S, into modifiers and an XKB key.

    Letters describe physical shortcut keys; capitals do not imply Shift.
    Use the named key ``plus`` when the plus key itself is desired.
    A restricted vocabulary also prevents xdotool command chaining.
    """
    if not isinstance(value, str) or not value.strip():
        raise ActionError("Enter a shortcut such as Ctrl+Shift+S or Space.")
    parts = [part.strip() for part in value.split("+")]
    if not all(parts):
        raise ActionError("A shortcut needs one key after its modifiers; use 'plus' for the + key.")
    modifiers: list[str] = []
    for part in parts[:-1]:
        modifier = _MODIFIERS.get(part.lower())
        if modifier is None:
            raise ActionError(f"Unknown shortcut modifier: {part}")
        if modifier not in modifiers:
            modifiers.append(modifier)
    raw_key = parts[-1]
    lower_key = raw_key.lower()
    if lower_key in _MODIFIERS:
        raise ActionError("A shortcut must include a key, not only modifiers.")
    if re.fullmatch(r"[a-z0-9]", lower_key):
        key = lower_key
    elif re.fullmatch(r"f([1-9]|[12][0-9]|3[0-5])", lower_key):
        key = lower_key.upper()
    elif lower_key in _KEY_ALIASES:
        key = _KEY_ALIASES[lower_key]
    else:
        raise ActionError(f"Unsupported shortcut key: {raw_key}. Use a letter, number, F key, or named navigation key.")
    return modifiers, key


class ActionRunner:
    """Execute actions and return a brief user-facing success message.

    Helper programs have a bounded timeout. Application and URL launches return
    immediately; success means the process started, not that the app opened.
    Action failures raise :class:`ActionError` for presentation by the caller.
    """

    def __init__(self, timeout: float = 4.0, environ: Mapping[str, str] | None = None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.timeout = timeout
        self.environ = dict(os.environ if environ is None else environ)
        self._lock = threading.Lock()
        self._children: list[subprocess.Popen] = []

    def _session(self) -> str:
        declared = self.environ.get("XDG_SESSION_TYPE", "").lower()
        if declared == "wayland" or self.environ.get("WAYLAND_DISPLAY") or self.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return "wayland"
        if declared == "x11" or self.environ.get("DISPLAY"):
            return "x11"
        return "unknown"

    def diagnostics(self) -> dict:
        """Report availability without launching programs or changing desktop state."""
        found = {name: shutil.which(name, path=self.environ.get("PATH")) for name in _PACKAGES}
        session = self._session()
        backend = {"wayland": "wtype", "x11": "xdotool"}.get(session)
        needed = ["playerctl", "wpctl", "xdg-open"] + ([backend] if backend else [])
        return {
            "session": session,
            "shortcut_backend": backend,
            "tools": found,
            "missing": [name for name in needed if not found[name]],
            "resolve_focus_guard": bool(self.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and found["hyprctl"]),
        }

    def _require(self, command: str) -> str:
        executable = shutil.which(command, path=self.environ.get("PATH"))
        if executable is None:
            package = _PACKAGES.get(command)
            hint = f"Install the Arch package '{package}'." if package else "Check the application name or use its full executable path."
            raise ActionError(f"Cannot find '{command}'. {hint}")
        return executable

    def _run(self, argv: list[str]) -> str:
        command = argv[0]
        argv = [self._require(command), *argv[1:]]
        try:
            result = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, errors="replace", timeout=self.timeout, check=False,
                env=self.environ, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ActionError(f"'{command}' did not respond within {self.timeout:g} seconds.") from exc
        except OSError as exc:
            raise ActionError(f"Cannot run '{command}': {exc.strerror or str(exc)}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "No details returned.").strip()[:400]
            raise ActionError(f"'{command}' failed (exit {result.returncode}): {detail}")
        return result.stdout.strip()

    def _launch(self, argv: list[str]) -> None:
        command = argv[0]
        argv = [self._require(os.path.expanduser(command)), *argv[1:]]
        # Retain process objects and reap finished children on the next launch.
        self._children = [process for process in self._children if process.poll() is None]
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                env=self.environ, shell=False,
            )
        except OSError as exc:
            raise ActionError(f"Cannot launch '{command}': {exc.strerror or str(exc)}") from exc
        self._children.append(process)

    def _check_resolve_focus(self) -> None:
        if not self.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            raise ActionError("Resolve shortcuts require a Hyprland session so the focused application can be checked.")
        raw = self._run(["hyprctl", "activewindow", "-j"])
        try:
            window = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ActionError("Hyprland returned an invalid focused-window response; Resolve shortcut was not sent.") from exc
        if not isinstance(window, dict) or not any(
            isinstance(window.get(field), str) and window[field].lower() in _RESOLVE_CLASSES
            for field in ("class", "initialClass")
        ):
            raise ActionError("Focus the DaVinci Resolve window before using this shortcut.")

    def _shortcut(self, value: str, target: str | None) -> str:
        modifiers, key = parse_shortcut(value)
        if target not in (None, "", "resolve"):
            raise ActionError(f"Unsupported shortcut target: {target}")
        session = self._session()
        if session == "wayland":
            argv = ["wtype"]
            for modifier in modifiers:
                argv.extend(["-M", modifier])
            argv.extend(["-k", key])
            for modifier in reversed(modifiers):
                argv.extend(["-m", modifier])
            self._require("wtype")
        elif session == "x11":
            x_modifiers = [{"logo": "super", "altgr": "ISO_Level3_Shift"}.get(mod, mod) for mod in modifiers]
            chord = "+".join([*x_modifiers, key])
            argv = ["xdotool", "key", "--clearmodifiers", chord]
            self._require("xdotool")
        else:
            raise ActionError("No graphical session detected. Launch VSD Deck from your desktop session to send shortcuts.")
        if target == "resolve":
            self._check_resolve_focus()
        try:
            self._run(argv)
        except ActionError:
            if session == "x11":
                # A killed xdotool can stop between keydown and keyup. wtype's
                # virtual keyboard is destroyed on exit, releasing its keys.
                try:
                    self._run(["xdotool", "keyup", chord])
                except ActionError:
                    pass
            raise
        return f"Sent {value}"

    def execute(self, action: dict) -> str:
        with self._lock:
            return self._execute(action)

    def _execute(self, action: dict) -> str:
        if not isinstance(action, dict):
            raise ActionError("Action must be an object with a type and value.")
        action_type = action.get("type", "noop")
        if action_type == "noop":
            return "No action assigned"
        value = action.get("value")
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ActionError("This action needs a nonempty text value.")
        value = value.strip()
        if action_type == "command":
            try:
                argv = shlex.split(value)
            except ValueError as exc:
                raise ActionError(f"Invalid command quoting: {exc}") from exc
            if not argv or not argv[0]:
                raise ActionError("Enter the application executable followed by optional arguments.")
            self._launch(argv)
            return f"Launched {argv[0]}"
        if action_type == "url":
            try:
                url = urlsplit(value)
                valid = url.scheme.lower() in {"http", "https"} and bool(url.hostname)
            except ValueError:
                valid = False
            if not valid or any(char.isspace() or ord(char) < 32 for char in value):
                raise ActionError("Use a complete http:// or https:// URL without spaces.")
            self._launch(["xdg-open", value])
            return "Opened URL in your browser"
        if action_type == "shortcut":
            return self._shortcut(value, action.get("target"))
        if action_type == "media":
            if value not in {"play-pause", "next", "previous", "stop"}:
                raise ActionError(f"Unknown media action: {value}")
            self._run(["playerctl", value])
            return f"Media: {value}"
        if action_type == "volume":
            if value in {"up", "down"}:
                delta = "5%+" if value == "up" else "5%-"
                self._run(["wpctl", "set-volume", "--limit", "1.0", "@DEFAULT_AUDIO_SINK@", delta])
            elif value in {"mute", "mic-mute"}:
                device = "@DEFAULT_AUDIO_SOURCE@" if value == "mic-mute" else "@DEFAULT_AUDIO_SINK@"
                self._run(["wpctl", "set-mute", device, "toggle"])
            else:
                raise ActionError(f"Unknown volume action: {value}")
            return f"Volume: {value}"
        raise ActionError(f"Unsupported action type: {action_type}")
