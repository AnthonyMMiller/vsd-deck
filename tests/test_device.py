import ctypes
from enum import Enum
import io
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vsd_deck import device


def packet(code, state=1):
    return b"ACK\x00\x00OK\x00\x00" + bytes((code, state))


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    if not predicate():
        raise AssertionError("Timed out waiting for device worker")


class InputTests(unittest.TestCase):
    def test_main_button_map_and_releases(self):
        for key in range(1, 11):
            self.assertEqual(device.normalize_packet(packet(key)), f"key:{key}")
            for release in (0, 2):
                self.assertIsNone(device.normalize_packet(packet(key, release)))
        for code in range(11, 16):
            self.assertIsNone(device.normalize_packet(packet(code)))

    def test_strip_knobs_and_swipes(self):
        for index in range(4):
            self.assertEqual(device.normalize_packet(packet(0x40 + index, 0)), f"key:{11 + index}")
            self.assertIsNone(device.normalize_packet(packet(0x40 + index, 2)))
        for code, (knob, direction) in device._ROTATION_CODES.items():
            self.assertEqual(device.normalize_packet(packet(code, 0)), f"knob:{knob}:{direction}")
        for code, knob in device._KNOB_PRESS_CODES.items():
            self.assertEqual(device.normalize_packet(packet(code)), f"knob:{knob}:press")
            self.assertIsNone(device.normalize_packet(packet(code, 0)))
            self.assertIsNone(device.normalize_packet(packet(code, 2)))
        self.assertEqual(device.normalize_packet(packet(0x38, 0)), "swipe:left")
        self.assertEqual(device.normalize_packet(packet(0x39, 0)), "swipe:right")

    def test_reject_short_malformed_unknown_and_touch_coordinate_packets(self):
        for data in (None, b"", b"ACK", b"x" * 20, packet(0xFF), b"ACK\x00ARX\x00\x00\x01\x01"):
            self.assertIsNone(device.normalize_packet(data))

    def test_normalized_sdk_events_ignore_releases(self):
        class Kind(Enum):
            BUTTON = "button"
        event = SimpleNamespace(event_type=Kind.BUTTON, key=3, state=1)
        self.assertEqual(device.normalize_event(event), "key:3")
        event.state = 0
        self.assertIsNone(device.normalize_event(event))
        self.assertIsNone(device.normalize_event(SimpleNamespace(event_type="knob_press", knob_id="knob_2", state=0)))
        self.assertEqual(device.normalize_event(SimpleNamespace(event_type="knob_rotate", knob_id="knob_2", direction="right")), "knob:2:right")


class SimulationTests(unittest.TestCase):
    def test_simulation_never_loads_sdk_and_dispatches_on_worker(self):
        received, statuses, threads = [], [], []
        def receive(event):
            received.append(event)
            threads.append(threading.get_ident())
        deck = device.DeckDevice(receive, statuses.append, simulate=True)
        with patch.object(device, "_load_sdk", side_effect=AssertionError("USB touched")) as loader:
            deck.set_images({1: "/does/not/need/to/exist.png"})
            deck.set_brightness(30)
            deck.start()
            original_thread = deck._thread
            deck.start()
            self.assertIs(deck._thread, original_thread)
            deck.inject_event("key:1")
            deck.inject_event("knob:4:left")
            wait_for(lambda: len(received) == 2)
            deck.stop()
            deck.stop()
            loader.assert_not_called()
        self.assertEqual(received, ["key:1", "knob:4:left"])
        self.assertNotIn(threading.get_ident(), threads)
        self.assertFalse(deck._thread.is_alive())
        self.assertTrue(any("Simulation" in status for status in statuses))

    def test_validation_and_coalesced_image_backlog(self):
        deck = device.DeckDevice(lambda _: None, lambda _: None, simulate=True)
        for n in range(100):
            deck.set_images({key: f"/tmp/{n}-{key}.png" for key in device.IMAGE_KEYS})
        self.assertEqual(len(deck._pending_images), 14)
        self.assertEqual(deck._pending_images[1], "/tmp/99-1.png")
        for images in ({0: "x"}, {15: "x"}, {True: "x"}, {"1": "x"}):
            with self.assertRaises(ValueError):
                deck.set_images(images)
        for brightness in (-1, 101, 3.5, True):
            with self.assertRaises(ValueError):
                deck.set_brightness(brightness)
        with self.assertRaises(ValueError):
            deck.inject_event("key:15")


class FakeConnection:
    instances = []
    fail_first = False

    def __init__(self, sdk, info):
        self.path = info["path"]
        self.images, self.brightness, self.calls = [], [], []
        self.closed = 0
        self.reads = 0
        self.should_fail = self.fail_first and not self.instances
        self.instances.append(self)
        self._record()

    def _record(self):
        self.calls.append(threading.get_ident())

    def read_event(self):
        self._record()
        time.sleep(0.002)
        self.reads += 1
        if self.should_fail and self.reads == 2:
            raise device.DeviceDisconnected("unplugged in test")
        return "key:2" if self.reads == 1 else None

    def can_write(self):
        self._record()
        return True

    def set_image(self, key, path):
        self._record()
        self.images.append((key, path))

    def set_brightness(self, value):
        self._record()
        self.brightness.append(value)

    def heartbeat(self):
        self._record()

    def close(self):
        self._record()
        self.closed += 1


class WorkerTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances = []
        FakeConnection.fail_first = False
        self.statuses, self.events = [], []
        self.deck = device.DeckDevice(self.events.append, self.statuses.append)
        self.deck.RECONNECT_INTERVAL = 0.015
        self.deck.PRESENCE_INTERVAL = 0.025

    def tearDown(self):
        self.deck.stop()

    def test_disconnect_reconnect_resends_images_and_closes_once_on_owner_thread(self):
        FakeConnection.fail_first = True
        self.deck.set_images({1: "one.png", 11: "touch.png"})
        self.deck.set_brightness(55)
        with patch.object(device, "_load_sdk", return_value=object()), patch.object(device, "_enumerate", return_value=[{"path": "test"}]), patch.object(device, "_NativeConnection", FakeConnection):
            self.deck.start()
            wait_for(lambda: len(FakeConnection.instances) >= 2 and len(FakeConnection.instances[1].images) == 2)
            self.deck.stop()
        first, second = FakeConnection.instances[:2]
        self.assertEqual(first.closed, 1)
        self.assertEqual(second.closed, 1)
        self.assertEqual(second.images, [(1, "one.png"), (11, "touch.png")])
        self.assertEqual(second.brightness, [55])
        self.assertEqual(len(set(first.calls + second.calls)), 1)
        self.assertNotEqual(first.calls[0], threading.get_ident())
        self.assertTrue(any("disconnected" in value for value in self.statuses))
        self.assertFalse(self.deck._thread.is_alive())

    def test_physical_removal_is_detected_when_reads_only_time_out(self):
        present = [{"path": "test"}]
        with patch.object(device, "_load_sdk", return_value=object()), patch.object(device, "_enumerate", side_effect=lambda _: list(present)), patch.object(device, "_NativeConnection", FakeConnection):
            self.deck.start()
            wait_for(lambda: len(FakeConnection.instances) == 1)
            present.clear()
            wait_for(lambda: FakeConnection.instances[0].closed == 1)
            self.deck.stop()
        self.assertTrue(any("unplugged" in value for value in self.statuses))

    def test_filtered_enumeration(self):
        enumerate_mock = Mock(return_value=[])
        sdk = SimpleNamespace(LibUSBHIDAPI=SimpleNamespace(enumerate_devices=enumerate_mock))
        self.assertEqual(device._enumerate(sdk), [])
        self.assertEqual(enumerate_mock.call_args_list, [
            unittest.mock.call(0x5548, 0x1008), unittest.mock.call(0x5548, 0x1023), unittest.mock.call(0x5548, 0x1021)
        ])

    def test_denied_usb_never_announces_connection_or_creates_native_transport(self):
        create = Mock()
        sdk = SimpleNamespace(_transport_lib=SimpleNamespace(transport_create=create))
        self.deck.set_images({1: "one.png"})
        with patch.object(device, "_load_sdk", return_value=sdk), patch.object(device, "_enumerate", return_value=[{"path": "/dev/hidraw8"}]), patch.object(device.os, "open", side_effect=PermissionError("denied")):
            self.deck.start()
            wait_for(lambda: any("USB access denied" in status for status in self.statuses))
            self.deck.stop()
        create.assert_not_called()
        self.assertFalse(any(status.startswith("N4 Pro connected") for status in self.statuses))
        self.assertEqual(self.deck._pending_images, {1: "one.png"})
        self.assertTrue(any("70-vsd-n4-pro.rules" in status for status in self.statuses))


class NativeAdapterTests(unittest.TestCase):
    def test_missing_usb_never_creates_native_transport(self):
        create = Mock()
        sdk = SimpleNamespace(_transport_lib=SimpleNamespace(transport_create=create))
        with patch.object(device.os, "open", side_effect=FileNotFoundError("gone")):
            with self.assertRaisesRegex(FileNotFoundError, "reconnect the USB cable"):
                device._NativeConnection(sdk, {"path": "/dev/hidraw8"})
        create.assert_not_called()

    def test_extended_hid_struct_and_idempotent_cleanup(self):
        class Info(ctypes.Structure):
            _fields_ = [("path", ctypes.c_char_p)]
        calls = []
        def create(info, output):
            address = ctypes.cast(info, ctypes.c_void_p).value
            self.assertEqual(ctypes.c_int.from_address(address + ctypes.sizeof(Info)).value, 1)
            self.assertEqual(info.contents.path, b"test")
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(123)
            calls.append("create")
            return 0
        def record(name):
            def call(*args):
                calls.append(name)
                return 0
            return call
        sdk = SimpleNamespace(
            _HidDeviceInfo=Info,
            LibUSBHIDAPI=SimpleNamespace(create_device_info_from_dict=lambda _: Info(b"test")),
            _transport_lib=SimpleNamespace(
                transport_create=create,
                transport_set_reportSize=record("report sizes"),
                transport_wakeup_screen=record("wake screen"),
                transport_destroy=record("destroy"),
            ),
        )
        with patch.object(device.os, "open", return_value=42) as preflight, patch.object(device.os, "close") as close_fd:
            connection = device._NativeConnection(sdk, {"path": "test"})
        preflight.assert_called_once_with("test", device.os.O_RDWR | device.os.O_NONBLOCK)
        close_fd.assert_called_once_with(42)
        connection.close()
        connection.close()
        self.assertEqual(calls, ["create", "report sizes", "wake screen", "destroy"])

    def test_image_destinations_dimensions_and_rotation(self):
        from PIL import Image
        connection = object.__new__(device._NativeConnection)
        connection._handle = ctypes.c_void_p(123)
        payloads = []
        def send(handle, data, length, destination):
            payloads.append((destination, Image.open(io.BytesIO(data)).copy()))
            return 0
        connection._lib = SimpleNamespace(transport_set_key_image_stream=send, transport_refresh=lambda _: 0)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "icon.png"
            picture = Image.new("RGBA", (112, 112), "black")
            picture.putpixel((0, 0), (255, 0, 0, 255))
            picture.save(path)
            for key in device.IMAGE_KEYS:
                connection.set_image(key, str(path))
        self.assertEqual([entry[0] for entry in payloads], [11, 12, 13, 14, 15, 6, 7, 8, 9, 10, 1, 2, 3, 4])
        self.assertEqual(payloads[0][1].getpixel((111, 111)), (255, 0, 0, 255))
        self.assertEqual(payloads[10][1].size, (176, 112))

    def test_read_timeout_is_idle_but_other_error_disconnects(self):
        connection = object.__new__(device._NativeConnection)
        connection._handle = ctypes.c_void_p(123)
        connection._lib = SimpleNamespace(transport_read=lambda *_: 0x05000302)
        self.assertIsNone(connection.read_event())
        connection._lib.transport_read = lambda *_: 0x02000204
        with self.assertRaises(device.DeviceDisconnected):
            connection.read_event()

    def test_checked_native_error_is_not_silently_ignored(self):
        with self.assertRaises(device.DeviceDisconnected):
            device._NativeConnection._check(0x02000202, "open device")


if __name__ == "__main__":
    unittest.main()
