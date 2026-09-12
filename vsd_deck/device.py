"""N4 Pro USB backend, with all native I/O owned by one worker thread.

The native transport is supplied by the manufacturer's SDK. Its Python N4 Pro
input decoder confuses input codes with image destinations; the mapping here
follows CPP-SDK/src/HotspotDevice/StreamDockN4Pro/streamdockN4Pro.cpp instead.
No firmware, saved configuration, calibration, or reset commands are used.
"""

from __future__ import annotations

import ctypes
import importlib
import io
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Callable


USB_VENDOR_ID = 0x5548
USB_PRODUCT_IDS = (0x1008, 0x1023, 0x1021)
IMAGE_KEYS = tuple(range(1, 15))
CONTROL_LAYOUT = {
    "main_rows": ((1, 2, 3, 4, 5), (6, 7, 8, 9, 10)),
    "touch_keys": (11, 12, 13, 14),
    "knobs": (1, 2, 3, 4),
    "swipes": ("left", "right"),
    "image_sizes": {key: (176, 112) if key >= 11 else (112, 112) for key in IMAGE_KEYS},
}
_IMAGE_DESTINATIONS = {**{key: key + 10 for key in range(1, 6)},
                       **{key: key for key in range(6, 11)},
                       **{key: key - 10 for key in range(11, 15)}}
_ROTATION_CODES = {0xA0: (1, "left"), 0xA1: (1, "right"),
                   0x50: (2, "left"), 0x51: (2, "right"),
                   0x90: (3, "left"), 0x91: (3, "right"),
                   0x70: (4, "left"), 0x71: (4, "right")}
_KNOB_PRESS_CODES = {0x37: 1, 0x35: 2, 0x33: 3, 0x36: 4}
_ALL_CONTROLS = {f"key:{key}" for key in IMAGE_KEYS} | {
    f"knob:{knob}:{action}" for knob in range(1, 5) for action in ("left", "right", "press")
} | {"swipe:left", "swipe:right"}


def normalize_packet(packet: bytes | None) -> str | None:
    """Decode verified N4 Pro packets; releases never trigger button actions.

    Touch zones send a one-shot state=0 event in the C++ SDK. The Python SDK
    documents state=1; accept both, while rejecting state=2 releases.
    """
    if not packet or len(packet) < 11 or packet[:3] != b"ACK" or packet[5:7] != b"OK":
        return None
    code, state = packet[9], packet[10]
    if 1 <= code <= 10:
        return f"key:{code}" if state == 1 else None
    if 0x40 <= code <= 0x43:
        return f"key:{code - 0x40 + 11}" if state in (0, 1) else None
    if code in _ROTATION_CODES:
        knob, direction = _ROTATION_CODES[code]
        return f"knob:{knob}:{direction}" if state in (0, 1) else None
    if code in _KNOB_PRESS_CODES:
        return f"knob:{_KNOB_PRESS_CODES[code]}:press" if state == 1 else None
    if code in (0x38, 0x39) and state in (0, 1):
        return "swipe:left" if code == 0x38 else "swipe:right"
    return None


def normalize_event(event: object) -> str | None:
    """Normalize an already logical SDK event (without importing the SDK)."""
    def value(name):
        item = getattr(event, name, None)
        return getattr(item, "value", item)

    kind = value("event_type")
    if kind == "button" and value("state") == 1:
        key = value("key")
        return f"key:{key}" if key in IMAGE_KEYS else None
    if kind in ("knob_rotate", "knob_press"):
        knob = value("knob_id")
        if knob not in ("knob_1", "knob_2", "knob_3", "knob_4"):
            return None
        if kind == "knob_press":
            return f"knob:{knob[-1]}:press" if value("state") == 1 else None
        direction = value("direction")
        if direction in ("left", "right"):
            return f"knob:{knob[-1]}:{direction}"
    if kind == "swipe" and value("direction") in ("left", "right"):
        return f"swipe:{value('direction')}"
    return None


def _load_sdk():
    """Load the local SDK only when USB mode actually starts."""
    override = os.environ.get("VSD_STREAMDOCK_SDK")
    sdk_src = Path(override) if override else (
        Path(__file__).resolve().parent.parent / "vendor/streamdock-sdk/Python-SDK/src"
    )
    if not (sdk_src / "StreamDock/Transport/LibUSBHIDAPI.py").is_file():
        raise RuntimeError(f"StreamDock SDK missing at {sdk_src}; run the SDK setup script")
    if str(sdk_src) not in sys.path:
        sys.path.insert(0, str(sdk_src))
    return importlib.import_module("StreamDock.Transport.LibUSBHIDAPI")


def enumerate_devices() -> list[dict]:
    """Read-only enumeration of N4 Pro vendor interfaces, for the diagnostic CLI."""
    return _enumerate(_load_sdk())


def _enumerate(sdk) -> list[dict]:
    result = []
    for pid in USB_PRODUCT_IDS:
        result.extend(sdk.LibUSBHIDAPI.enumerate_devices(USB_VENDOR_ID, pid))
    return result


class DeviceDisconnected(OSError):
    pass


class _NativeConnection:
    """One caller over the SDK's native sender queue.

    Native write success means queued, not acknowledged by the hardware. The
    SDK opens HID asynchronously and silently drops commands on open failure,
    so verify access ourselves before allocating its transport.
    """

    def __init__(self, sdk, info: dict):
        self.path = info["path"]
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise PermissionError(
                f"USB access denied for {self.path}. Install "
                "packaging/70-vsd-n4-pro.rules and reconnect the N4 Pro"
            ) from exc
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"N4 Pro USB device {self.path} is unavailable; reconnect the USB cable"
            ) from exc
        else:
            os.close(descriptor)
        self._lib = sdk._transport_lib
        self._handle = ctypes.c_void_p()
        # SDK ctypes omits the trailing hid_bus_type added by HIDAPI 0.13.
        # Allocate the full struct locally; preserve all original field offsets.
        class FullDeviceInfo(sdk._HidDeviceInfo):
            _fields_ = [("bus_type", ctypes.c_int)]

        base = sdk.LibUSBHIDAPI.create_device_info_from_dict(info)
        self._info = FullDeviceInfo()
        for name, _ in sdk._HidDeviceInfo._fields_:
            setattr(self._info, name, getattr(base, name))
        self._info.bus_type = 1  # HID_API_BUS_USB in the bundled hidapi.h
        pointer = ctypes.cast(ctypes.byref(self._info), ctypes.POINTER(sdk._HidDeviceInfo))
        try:
            self._check(self._lib.transport_create(pointer, ctypes.byref(self._handle)), "open device")
            if not self._handle.value:
                raise DeviceDisconnected("SDK returned an empty device handle")
            self._check(self._lib.transport_set_reportSize(self._handle, 513, 1025, 0), "set report sizes")
            self._check(self._lib.transport_wakeup_screen(self._handle), "queue display wake")
            # Do not use get_firmware_version as a timed handshake here: the
            # native implementation calls hid_get_input_report without a timeout
            # and may block this worker's reads and shutdown indefinitely.
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check(result, operation):
        if result != 0:
            raise DeviceDisconnected(f"Cannot {operation} (USB error 0x{result:08x}); check cable and udev access")

    def read_event(self) -> str | None:
        buffer = (ctypes.c_uint8 * 1024)()
        length = ctypes.c_size_t(len(buffer))
        result = self._lib.transport_read(self._handle, buffer, ctypes.byref(length), 40)
        if result & 0xFF000000 == 0x05000000:  # read timeout is an idle device
            return None
        self._check(result, "read device")
        if length.value > len(buffer):
            raise DeviceDisconnected("SDK returned an invalid input length")
        return normalize_packet(bytes(buffer[:length.value]))

    def can_write(self) -> bool:
        """Whether the native sender is enabled; not queue length or delivery."""
        enabled = ctypes.c_int()
        self._check(self._lib.transport_can_write(self._handle, ctypes.byref(enabled)), "check sender")
        return bool(enabled.value)

    def set_image(self, key: int, path: str):
        from PIL import Image

        with Image.open(path) as original:
            picture = original.convert("RGBA").rotate(180).resize(
                CONTROL_LAYOUT["image_sizes"][key], Image.Resampling.LANCZOS
            )
            output = io.BytesIO()
            picture.save(output, format="PNG")
            payload = output.getvalue()
        self._check(self._lib.transport_set_key_image_stream(
            self._handle, payload, len(payload), _IMAGE_DESTINATIONS[key]
        ), "queue button image")
        self._check(self._lib.transport_refresh(self._handle), "queue display refresh")

    def set_brightness(self, percent: int):
        self._check(self._lib.transport_set_key_brightness(self._handle, percent), "queue brightness")

    def heartbeat(self):
        self._check(self._lib.transport_heartbeat(self._handle), "queue heartbeat")

    def close(self):
        if self._handle.value:
            handle, self._handle = self._handle, ctypes.c_void_p()
            self._check(self._lib.transport_destroy(handle), "close device")


class DeckDevice:
    """Thread-safe device facade. Callbacks must enqueue for the UI, never draw.

    At most one pending path per image key and one brightness value are kept.
    This coalesces refreshed widgets before they enter the native sender queue.
    The last desired images are retained and resent after reconnection.
    """

    RECONNECT_INTERVAL = 2.0
    PRESENCE_INTERVAL = 2.0

    def __init__(self, on_event: Callable[[str], None], on_status: Callable[[str], None], simulate=False):
        self.on_event, self.on_status = on_event, on_status
        self.simulate = bool(simulate)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._reconnect = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_images: dict[int, str] = {}
        self._pending_images: dict[int, str] = {}
        self._brightness: int | None = None
        self._brightness_pending = False
        self._events: queue.Queue[str] = queue.Queue(maxsize=256)
        self._last_status = ""

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="vsd-device", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
            if thread.is_alive():
                self._status("Device is finishing a USB operation; shutdown requested")

    def reconnect(self):
        self._reconnect.set()
        self._wake.set()

    def set_images(self, images: dict[int, str]):
        if any(type(key) is not int or key not in IMAGE_KEYS for key in images):
            raise ValueError("N4 Pro image keys must be integers from 1 through 14")
        updates = {key: os.fspath(path) for key, path in images.items()}
        with self._lock:
            self._latest_images.update(updates)
            self._pending_images.update(updates)
        self._wake.set()

    def set_brightness(self, percent: int):
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError("Brightness must be an integer from 0 through 100")
        with self._lock:
            self._brightness = percent
            self._brightness_pending = True
        self._wake.set()

    def inject_event(self, control: str):
        """Trigger a control in simulation mode, without importing native code."""
        if not self.simulate:
            raise RuntimeError("Input injection is available only in simulation")
        if control not in _ALL_CONTROLS:
            raise ValueError(f"Unknown N4 Pro control: {control}")
        try:
            self._events.put_nowait(control)
        except queue.Full:
            return
        self._wake.set()

    def _status(self, message: str):
        if message != self._last_status:
            self._last_status = message
            try:
                self.on_status(message)
            except Exception:
                pass  # A closing UI must not prevent USB cleanup.

    def _emit(self, event: str | None):
        if event and not self._stop.is_set():
            try:
                self.on_event(event)
            except Exception as exc:
                self._status(f"Input callback failed: {exc}")

    def _close(self, connection):
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                self._status(f"Device close failed: {exc}")

    def _run(self):
        if self.simulate:
            self._status("Simulation mode — USB is disabled")
            while not self._stop.is_set():
                try:
                    self._emit(self._events.get(timeout=0.05))
                except queue.Empty:
                    pass
            self._status("Device stopped")
            return

        sdk = None
        connection = None
        next_connect = next_presence = next_heartbeat = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if self._reconnect.is_set():
                    self._reconnect.clear()
                    self._close(connection)
                    connection = None
                    next_connect = 0.0
                if connection is None:
                    if now < next_connect:
                        self._wake.wait(min(next_connect - now, 0.1))
                        self._wake.clear()
                        continue
                    next_connect = now + self.RECONNECT_INTERVAL
                    try:
                        sdk = sdk or _load_sdk()
                        devices = _enumerate(sdk)
                        if not devices:
                            self._status("N4 Pro not connected — waiting for USB")
                            continue
                        connection = _NativeConnection(sdk, devices[0])
                        with self._lock:
                            self._pending_images = dict(self._latest_images)
                            self._brightness_pending = self._brightness is not None
                        next_presence = now + self.PRESENCE_INTERVAL
                        next_heartbeat = now + 1.0
                        self._status("N4 Pro connected")
                    except Exception as exc:
                        self._status(f"N4 Pro unavailable: {exc}")
                        continue

                try:
                    if now >= next_presence:
                        if not any(info["path"] == connection.path for info in _enumerate(sdk)):
                            raise DeviceDisconnected("N4 Pro unplugged")
                        next_presence = now + self.PRESENCE_INTERVAL
                    if now >= next_heartbeat:
                        connection.heartbeat()
                        next_heartbeat = now + 10.0
                    self._emit(connection.read_event())
                    if self._stop.is_set() or not connection.can_write():
                        continue
                    with self._lock:
                        brightness = self._brightness if self._brightness_pending else None
                        self._brightness_pending = False
                        item = next(iter(self._pending_images.items()), None)
                        if item is not None:
                            del self._pending_images[item[0]]
                    if brightness is not None:
                        connection.set_brightness(brightness)
                    if item is not None:
                        try:
                            connection.set_image(*item)
                        except (FileNotFoundError, ValueError) as exc:
                            self._status(f"Cannot load button {item[0]} image: {exc}")
                        except OSError as exc:
                            if isinstance(exc, DeviceDisconnected):
                                raise
                            self._status(f"Cannot load button {item[0]} image: {exc}")
                except Exception as exc:
                    self._close(connection)
                    connection = None
                    self._status(f"N4 Pro disconnected: {exc}; reconnecting")
                    next_connect = time.monotonic() + self.RECONNECT_INTERVAL
        finally:
            self._close(connection)
            self._status("Device stopped")
