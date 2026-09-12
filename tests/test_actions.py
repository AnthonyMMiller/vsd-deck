"""Desktop actions are tested without sending input or launching applications."""

import json
import subprocess
import unittest
from unittest.mock import patch

from vsd_deck.actions import ActionError, ActionRunner, parse_shortcut


WAYLAND = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-1", "HYPRLAND_INSTANCE_SIGNATURE": "test"}


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.which = patch("vsd_deck.actions.shutil.which", side_effect=lambda name, **kwargs: "/usr/bin/" + name).start()
        self.run = patch("vsd_deck.actions.subprocess.run").start()
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        self.popen = patch("vsd_deck.actions.subprocess.Popen").start()
        self.addCleanup(patch.stopall)
        self.runner = ActionRunner(environ=WAYLAND)

    def test_wayland_chord_releases_modifiers_in_reverse_order(self):
        self.runner.execute({"type": "shortcut", "value": "Ctrl+Shift+S"})
        self.assertEqual(self.run.call_args.args[0], ["/usr/bin/wtype", "-M", "ctrl", "-M", "shift", "-k", "s", "-m", "shift", "-m", "ctrl"])
        self.assertFalse(self.run.call_args.kwargs["shell"])

    def test_shortcut_aliases_and_duplicates(self):
        self.assertEqual(parse_shortcut(" Control + Ctrl + Super + PageDown "), (["ctrl", "logo"], "Next"))
        self.assertEqual(parse_shortcut("F12"), ([], "F12"))
        self.assertEqual(parse_shortcut("Ctrl+plus"), (["ctrl"], "plus"))

    def test_malformed_and_command_shaped_shortcuts_are_rejected(self):
        for shortcut in ("Ctrl", "Ctrl++S", "Ctrl+Unknown", "Ctrl+S+J", "exec", "--window 1", "a;shutdown", "F36"):
            with self.subTest(shortcut=shortcut), self.assertRaises(ActionError):
                self.runner.execute({"type": "shortcut", "value": shortcut})
        self.run.assert_not_called()

    def test_x11_shortcut_backend(self):
        ActionRunner(environ={"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}).execute({"type": "shortcut", "value": "Super+Left"})
        self.assertEqual(self.run.call_args.args[0], ["/usr/bin/xdotool", "key", "--clearmodifiers", "super+Left"])

    def test_wayland_does_not_fall_back_to_x11_when_wtype_missing(self):
        self.which.side_effect = lambda name, **kwargs: None if name == "wtype" else "/usr/bin/" + name
        with self.assertRaisesRegex(ActionError, "Arch package 'wtype'"):
            self.runner.execute({"type": "shortcut", "value": "Ctrl+S"})
        self.run.assert_not_called()

    def test_no_display_rejects_shortcut(self):
        with self.assertRaisesRegex(ActionError, "No graphical session"):
            ActionRunner(environ={}).execute({"type": "shortcut", "value": "Space"})
        self.run.assert_not_called()

    def test_command_metacharacters_are_literal_arguments(self):
        command = "example --label 'two words' ';' '$(touch /tmp/unwanted)' '`id`' '>' '&'"
        self.runner.execute({"type": "command", "value": command})
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], ["/usr/bin/example", "--label", "two words", ";", "$(touch /tmp/unwanted)", "`id`", ">", "&"])
        self.assertFalse(kwargs["shell"])
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        self.popen.return_value.wait.assert_not_called()
        self.run.assert_not_called()

    def test_unbalanced_command_quoting_rejected(self):
        with self.assertRaisesRegex(ActionError, "quoting"):
            self.runner.execute({"type": "command", "value": "example 'unfinished"})
        self.popen.assert_not_called()

    def test_missing_program_and_launch_failure_have_helpful_errors(self):
        self.which.side_effect = None
        self.which.return_value = None
        with self.assertRaisesRegex(ActionError, "Cannot find 'example'"):
            self.runner.execute({"type": "command", "value": "example"})
        self.which.return_value = "/usr/bin/example"
        self.popen.side_effect = PermissionError(13, "Permission denied")
        with self.assertRaisesRegex(ActionError, "Permission denied"):
            self.runner.execute({"type": "command", "value": "example"})

    def test_media_failure_preserves_diagnostic(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="No players found")
        with self.assertRaisesRegex(ActionError, "No players found"):
            self.runner.execute({"type": "media", "value": "play-pause"})

    def test_helper_timeout_is_bounded(self):
        self.run.side_effect = subprocess.TimeoutExpired("playerctl", 4)
        with self.assertRaisesRegex(ActionError, "within 4 seconds"):
            self.runner.execute({"type": "media", "value": "next"})
        self.assertEqual(self.run.call_args.kwargs["timeout"], 4)

    def test_xdotool_failure_attempts_key_release(self):
        self.run.side_effect = [subprocess.TimeoutExpired("xdotool", 4), subprocess.CompletedProcess([], 0, stdout="", stderr="")]
        with self.assertRaises(ActionError):
            ActionRunner(environ={"DISPLAY": ":0"}).execute({"type": "shortcut", "value": "Ctrl+Shift+S"})
        self.assertEqual(self.run.call_args_list[1].args[0], ["/usr/bin/xdotool", "keyup", "ctrl+shift+s"])

    def test_volume_is_capped_and_mic_targets_source(self):
        self.runner.execute({"type": "volume", "value": "up"})
        self.assertEqual(self.run.call_args.args[0], ["/usr/bin/wpctl", "set-volume", "--limit", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"])
        self.runner.execute({"type": "volume", "value": "mic-mute"})
        self.assertEqual(self.run.call_args.args[0], ["/usr/bin/wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "toggle"])

    def test_url_only_opens_web_schemes(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https:///missing-host", "https://host/with space"):
            with self.subTest(url=url), self.assertRaises(ActionError):
                self.runner.execute({"type": "url", "value": url})
        self.popen.assert_not_called()
        self.runner.execute({"type": "url", "value": "https://example.org/?a=1&b=2"})
        self.assertEqual(self.popen.call_args.args[0], ["/usr/bin/xdg-open", "https://example.org/?a=1&b=2"])

    def test_resolve_checks_class_immediately_before_shortcut(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 0, stdout=json.dumps({"class": "resolve"}), stderr=""), subprocess.CompletedProcess([], 0, stdout="", stderr="")]
        self.runner.execute({"type": "shortcut", "value": "Ctrl+S", "target": "resolve"})
        self.assertEqual(self.run.call_args_list[0].args[0], ["/usr/bin/hyprctl", "activewindow", "-j"])
        self.assertEqual(self.run.call_args_list[1].args[0][0], "/usr/bin/wtype")

    def test_resolve_rejects_browser_with_resolve_in_title(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout=json.dumps({"class": "firefox", "title": "DaVinci Resolve tutorial"}), stderr="")
        with self.assertRaisesRegex(ActionError, "Focus the DaVinci Resolve"):
            self.runner.execute({"type": "shortcut", "value": "Space", "target": "resolve"})
        self.assertEqual(self.run.call_count, 1)

    def test_resolve_rejects_invalid_focus_response(self):
        for raw in ("not json", "null", "[]", "{}"):
            self.run.return_value = subprocess.CompletedProcess([], 0, stdout=raw, stderr="")
            with self.subTest(raw=raw), self.assertRaises(ActionError):
                self.runner.execute({"type": "shortcut", "value": "Space", "target": "resolve"})
        self.assertEqual(self.run.call_count, 4)

    def test_diagnostics_are_read_only_and_report_missing_packages(self):
        self.which.side_effect = lambda name, **kwargs: None if name == "wtype" else "/usr/bin/" + name
        diagnostics = self.runner.diagnostics()
        self.assertEqual(diagnostics["shortcut_backend"], "wtype")
        self.assertIn("wtype", diagnostics["missing"])
        self.assertTrue(diagnostics["resolve_focus_guard"])
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_unassigned_action_does_nothing(self):
        self.assertEqual(self.runner.execute({"type": "noop"}), "No action assigned")
        self.run.assert_not_called()
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
