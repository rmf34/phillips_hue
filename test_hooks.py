"""Unit and integration tests for the Claude Code → Hue hook.

Run with:
    .venv/bin/python -m unittest test_hooks.py -v
"""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import claude_hook
import hue_green


class ToolErroredTests(unittest.TestCase):
    """claude_hook.tool_errored — explicit failure signals from PostToolUse."""

    def test_is_error_true(self):
        self.assertTrue(claude_hook.tool_errored({"tool_response": {"is_error": True}}))

    def test_nonzero_exit_code_camel(self):
        self.assertTrue(claude_hook.tool_errored({"tool_response": {"exitCode": 1}}))

    def test_nonzero_exit_code_snake(self):
        self.assertTrue(claude_hook.tool_errored({"tool_response": {"exit_code": 2}}))

    def test_zero_exit_code_is_clean(self):
        self.assertFalse(claude_hook.tool_errored({"tool_response": {"exitCode": 0}}))

    def test_error_string(self):
        self.assertTrue(claude_hook.tool_errored({"tool_response": {"error": "boom"}}))

    def test_status_failed(self):
        self.assertTrue(claude_hook.tool_errored({"tool_response": {"status": "FAILED"}}))

    def test_empty_response_is_clean(self):
        self.assertFalse(claude_hook.tool_errored({"tool_response": {}}))

    def test_missing_response_is_clean(self):
        self.assertFalse(claude_hook.tool_errored({}))

    def test_non_dict_response_is_clean(self):
        # Real payloads sometimes have stringly-typed tool_response.
        self.assertFalse(claude_hook.tool_errored({"tool_response": "ok"}))


class ResponseIndicatesErrorTests(unittest.TestCase):
    """claude_hook.response_indicates_error — phrase heuristic on Claude's reply."""

    def _msg(self, text):
        return {"last_assistant_message": text}

    def test_plain_error_phrase(self):
        self.assertTrue(claude_hook.response_indicates_error(
            self._msg("I encountered an error while reading the file.")))

    def test_failed_to_phrase(self):
        self.assertTrue(claude_hook.response_indicates_error(
            self._msg("Failed to connect to the database.")))

    def test_negation_overrides_phrase(self):
        # "no errors found" must NOT light the bulb red.
        self.assertFalse(claude_hook.response_indicates_error(
            self._msg("Ran the test suite — no errors found.")))

    def test_successfully_overrides_in_same_sentence(self):
        self.assertFalse(claude_hook.response_indicates_error(
            self._msg("Successfully deployed the change.")))

    def test_mixed_outcome_still_flags_error(self):
        # A success in one sentence must NOT mask a failure in another.
        self.assertTrue(claude_hook.response_indicates_error(self._msg(
            "Successfully read the config. Failed to write the cache."
        )))

    def test_mixed_outcome_with_clause_separator(self):
        # Clause separators (semicolons) split sentences too.
        self.assertTrue(claude_hook.response_indicates_error(self._msg(
            "All previous steps ran without errors; unable to finish the last one."
        )))

    def test_empty_message_is_clean(self):
        self.assertFalse(claude_hook.response_indicates_error(self._msg("")))

    def test_missing_message_is_clean(self):
        self.assertFalse(claude_hook.response_indicates_error({}))

    def test_case_insensitive(self):
        self.assertTrue(claude_hook.response_indicates_error(
            self._msg("UNABLE TO open the connection.")))


# Fixture: what get_lights() returns from a real-ish Bridge.
LIGHTS_FIXTURE = {
    "1": {"name": "Bedroom",      "type": "Extended color light"},
    "3": {"name": "Office Lamp",  "type": "Color temperature light"},
    "5": {"name": "Desk (color)", "type": "Extended color light"},
    "7": {"name": "Desk Lamp",    "type": "Extended color light"},
}


class ResolveLightTests(unittest.TestCase):
    """hue_green.resolve_light — ID / exact name / partial / ambiguous / miss."""

    def setUp(self):
        patcher = mock.patch.object(hue_green, "get_lights", return_value=LIGHTS_FIXTURE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_resolve_by_numeric_id(self):
        lid, light = hue_green.resolve_light("5")
        self.assertEqual(lid, "5")
        self.assertEqual(light["name"], "Desk (color)")

    def test_resolve_by_exact_name_case_insensitive(self):
        lid, light = hue_green.resolve_light("office lamp")
        self.assertEqual(lid, "3")

    def test_resolve_by_unique_substring(self):
        # "Bedroom" is the only name containing "bed".
        lid, _ = hue_green.resolve_light("bed")
        self.assertEqual(lid, "1")

    def test_ambiguous_substring_raises(self):
        # "desk" matches both "Desk (color)" and "Desk Lamp".
        with self.assertRaises(SystemExit) as ctx:
            hue_green.resolve_light("desk")
        self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_no_match_raises(self):
        with self.assertRaises(SystemExit) as ctx:
            hue_green.resolve_light("kitchen")
        self.assertIn("No light matches", str(ctx.exception))


class BulbCapabilitiesTests(unittest.TestCase):
    """hue_green.bulb_capabilities — type/caps must classify bulbs correctly.

    Regression: a CT-only bulb ("Color temperature light") was previously
    being treated as supporting color because the check was `"color" in
    type`. That made cmd_color silently send hue/sat to bulbs that ignore
    it.
    """

    def test_extended_color_light(self):
        self.assertEqual(
            hue_green.bulb_capabilities({"type": "Extended color light"}),
            (True, True),
        )

    def test_color_only_light(self):
        self.assertEqual(
            hue_green.bulb_capabilities({"type": "Color light"}),
            (True, False),
        )

    def test_color_temperature_light_is_not_color(self):
        # The bug: this used to come back as has_color=True.
        self.assertEqual(
            hue_green.bulb_capabilities({"type": "Color temperature light"}),
            (False, True),
        )

    def test_dimmable_light(self):
        self.assertEqual(
            hue_green.bulb_capabilities({"type": "Dimmable light"}),
            (False, False),
        )

    def test_capabilities_block_takes_precedence(self):
        # A bulb that reports a colorgamut is a color bulb regardless of the
        # type string.
        light = {
            "type": "Mystery bulb",
            "capabilities": {"control": {"colorgamuttype": "C", "ct": {"min": 153}}},
        }
        self.assertEqual(hue_green.bulb_capabilities(light), (True, True))


class CmdColorDispatchTests(unittest.TestCase):
    """hue_green.cmd_color — make sure the right state payload is sent for each
    bulb type. We mock set_state and resolve_light so no Bridge is needed."""

    def setUp(self):
        self.set_state = mock.patch.object(hue_green, "set_state").start()
        self.resolve = mock.patch.object(hue_green, "resolve_light").start()
        self.addCleanup(mock.patch.stopall)

    def _send(self, light, color):
        self.resolve.return_value = ("9", light)
        hue_green.cmd_color(color, "9")
        self.assertEqual(self.set_state.call_count, 1)
        return self.set_state.call_args[0][1]

    def test_color_on_extended_color_light(self):
        light = {"name": "Desk", "type": "Extended color light"}
        state = self._send(light, "red")
        self.assertEqual(state["on"], True)
        self.assertEqual(state["hue"], hue_green.COLOR_PRESETS["red"]["hue"])

    def test_color_on_ct_only_light_falls_back_to_brightness(self):
        # The original bug: a CT-only bulb would receive an hue/sat payload.
        # Now it should only get on+bri.
        light = {"name": "Office", "type": "Color temperature light"}
        state = self._send(light, "red")
        self.assertEqual(state, {"on": True, "bri": hue_green.DEFAULT_BRI})

    def test_white_on_ct_light_uses_ct(self):
        light = {"name": "Office", "type": "Color temperature light"}
        state = self._send(light, "warm")
        self.assertEqual(state["ct"], hue_green.WHITE_PRESETS["warm"]["ct"])

    def test_white_on_color_only_light_uses_xy_fallback(self):
        light = {"name": "Lamp", "type": "Color light"}
        state = self._send(light, "warm")
        self.assertEqual(state.get("xy"), [0.45, 0.41])
        self.assertNotIn("ct", state)

    def test_white_on_dimmable_light_just_brightness(self):
        light = {"name": "Hall", "type": "Dimmable light"}
        state = self._send(light, "warm")
        self.assertEqual(state, {"on": True, "bri": hue_green.DEFAULT_BRI})


class ThrottleTests(unittest.TestCase):
    """set_color throttle: duplicate skip, time window, priority."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(claude_hook, "CACHE_DIR", Path(self.tmpdir)),
            mock.patch.object(claude_hook, "HUE_ENABLED", True),
            mock.patch.object(claude_hook, "_spawn"),
            mock.patch.object(claude_hook, "DEBUG_LOG", None),
        ]
        for p in self._patches:
            p.start()
        self.spawn = claude_hook._spawn
        self.data = {"session_id": "test-session"}

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_duplicate_color_is_skipped(self):
        claude_hook.set_color("normal", self.data)
        claude_hook.set_color("normal", self.data)
        self.assertEqual(self.spawn.call_count, 1)

    def test_different_color_goes_through(self):
        claude_hook.set_color("normal", self.data)
        claude_hook.set_color("blue", self.data)
        self.assertEqual(self.spawn.call_count, 2)

    def test_higher_priority_wins_within_throttle_window(self):
        claude_hook.set_color("normal", self.data)
        state_path = claude_hook._light_state_path(self.data)
        state_path.write_text(f"normal\n{time.time()}")
        claude_hook.set_color("blue", self.data)
        self.assertEqual(self.spawn.call_count, 2)

    def test_lower_priority_blocked_within_throttle_window(self):
        claude_hook.set_color("red", self.data)
        state_path = claude_hook._light_state_path(self.data)
        state_path.write_text(f"red\n{time.time()}")
        claude_hook.set_color("green", self.data)
        self.assertEqual(self.spawn.call_count, 1)

    def test_clear_light_state_allows_same_color(self):
        claude_hook.set_color("normal", self.data)
        claude_hook._clear_light_state(self.data)
        claude_hook.set_color("normal", self.data)
        self.assertEqual(self.spawn.call_count, 2)


class SoundTests(unittest.TestCase):
    """Sounds play regardless of HUE_ENABLED."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(claude_hook, "CACHE_DIR", Path(self.tmpdir)),
            mock.patch.object(claude_hook, "HUE_ENABLED", False),
            mock.patch.object(claude_hook, "_spawn"),
            mock.patch.object(claude_hook, "DEBUG_LOG", None),
        ]
        for p in self._patches:
            p.start()
        self.spawn = claude_hook._spawn

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_play_calls_afplay(self):
        claude_hook.play("/System/Library/Sounds/Glass.aiff")
        self.spawn.assert_called_once_with(["/usr/bin/afplay", "/System/Library/Sounds/Glass.aiff"])

    def test_set_color_skipped_when_disabled(self):
        claude_hook.set_color("green", {"session_id": "x"})
        self.spawn.assert_not_called()


class MainDispatchTests(unittest.TestCase):
    """Integration: feed event JSON to main(), verify spawn calls."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(claude_hook, "CACHE_DIR", Path(self.tmpdir)),
            mock.patch.object(claude_hook, "HUE_ENABLED", True),
            mock.patch.object(claude_hook, "_spawn"),
            mock.patch.object(claude_hook, "DEBUG_LOG", None),
        ]
        for p in self._patches:
            p.start()
        self.spawn = claude_hook._spawn

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _feed(self, event_data: dict) -> int:
        raw = json.dumps(event_data)
        with mock.patch("sys.stdin", io.StringIO(raw)):
            return claude_hook.main()

    def _spawn_colors(self) -> list[str]:
        """Extract just the color argument from set_color spawn calls."""
        return [
            call.args[0][3]
            for call in self.spawn.call_args_list
            if len(call.args[0]) >= 4 and call.args[0][1].endswith("hue_green.py")
        ]

    def _spawn_sounds(self) -> list[str]:
        """Extract sound file paths from play spawn calls."""
        return [
            call.args[0][1]
            for call in self.spawn.call_args_list
            if call.args[0][0] == "/usr/bin/afplay"
        ]

    def test_clean_turn_lifecycle(self):
        self._feed({"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
        self._feed({"hook_event_name": "PreToolUse", "session_id": "s1"})
        self._feed({"hook_event_name": "PostToolUse", "session_id": "s1",
                     "tool_response": {"exitCode": 0}})
        self._feed({"hook_event_name": "Stop", "session_id": "s1",
                     "last_assistant_message": "Done."})
        self.assertIn("green", self._spawn_colors())
        self.assertNotIn("red", self._spawn_colors())
        self.assertIn(claude_hook.SOUND_SUCCESS, self._spawn_sounds())
        self.assertNotIn(claude_hook.SOUND_ERROR, self._spawn_sounds())

    def test_error_turn_lifecycle(self):
        self._feed({"hook_event_name": "UserPromptSubmit", "session_id": "s2"})
        self._feed({"hook_event_name": "PostToolUse", "session_id": "s2",
                     "tool_response": {"exitCode": 1}})
        self._feed({"hook_event_name": "Stop", "session_id": "s2",
                     "last_assistant_message": "All good."})
        self.assertIn("red", self._spawn_colors())
        self.assertNotIn("green", self._spawn_colors())
        self.assertIn(claude_hook.SOUND_ERROR, self._spawn_sounds())

    def test_response_heuristic_error(self):
        self._feed({"hook_event_name": "UserPromptSubmit", "session_id": "s3"})
        self._feed({"hook_event_name": "Stop", "session_id": "s3",
                     "last_assistant_message": "I encountered an error reading the file."})
        self.assertIn("red", self._spawn_colors())
        self.assertIn(claude_hook.SOUND_ERROR, self._spawn_sounds())

    def test_notification_triggers_blue_and_sound(self):
        self._feed({"hook_event_name": "UserPromptSubmit", "session_id": "s4"})
        self.spawn.reset_mock()
        self._feed({"hook_event_name": "Notification", "session_id": "s4",
                     "notification_type": "permission"})
        self.assertIn("blue", self._spawn_colors())
        self.assertIn(claude_hook.SOUND_NOTIFICATION, self._spawn_sounds())

    def test_pretooluse_after_blue_resets_to_normal(self):
        self._feed({"hook_event_name": "UserPromptSubmit", "session_id": "s4b"})
        self._feed({"hook_event_name": "Notification", "session_id": "s4b",
                     "notification_type": "permission"})
        # Simulate the user taking time to approve (throttle window expires).
        state_path = claude_hook._light_state_path({"session_id": "s4b"})
        state_path.write_text(f"blue\n{time.time() - 1.0}")
        self.spawn.reset_mock()
        self._feed({"hook_event_name": "PreToolUse", "session_id": "s4b"})
        self.assertIn("normal", self._spawn_colors())

    def test_idle_notification_is_ignored(self):
        self._feed({"hook_event_name": "UserPromptSubmit", "session_id": "s5"})
        self.spawn.reset_mock()
        self._feed({"hook_event_name": "Notification", "session_id": "s5",
                     "notification_type": "idle_prompt"})
        self.assertEqual(self._spawn_colors(), [])
        self.assertEqual(self._spawn_sounds(), [])

    def test_unknown_event_is_noop(self):
        self._feed({"hook_event_name": "SomeFutureEvent", "session_id": "s6"})
        self.assertEqual(self._spawn_colors(), [])
        self.assertEqual(self._spawn_sounds(), [])

    def test_session_end_cleans_up_error_flag(self):
        self._feed({"hook_event_name": "UserPromptSubmit", "session_id": "s7"})
        self._feed({"hook_event_name": "PostToolUse", "session_id": "s7",
                     "tool_response": {"is_error": True}})
        error_flag = claude_hook._error_flag_path({"session_id": "s7"})
        self.assertTrue(error_flag.exists())
        self._feed({"hook_event_name": "SessionEnd", "session_id": "s7"})
        self.assertFalse(error_flag.exists())

    def test_error_flag_does_not_leak_across_turns(self):
        self._feed({"hook_event_name": "UserPromptSubmit", "session_id": "s8"})
        self._feed({"hook_event_name": "PostToolUse", "session_id": "s8",
                     "tool_response": {"exitCode": 1}})
        self._feed({"hook_event_name": "Stop", "session_id": "s8",
                     "last_assistant_message": "Done."})
        self.spawn.reset_mock()
        self._feed({"hook_event_name": "UserPromptSubmit", "session_id": "s8"})
        self._feed({"hook_event_name": "Stop", "session_id": "s8",
                     "last_assistant_message": "All clear."})
        self.assertIn("green", self._spawn_colors())
        self.assertNotIn("red", self._spawn_colors())


class SessionKeyTests(unittest.TestCase):
    """_session_key sanitization."""

    def test_normal_uuid(self):
        key = claude_hook._session_key({"session_id": "abc-123-def"})
        self.assertEqual(key, "abc-123-def")

    def test_path_separators_stripped(self):
        key = claude_hook._session_key({"session_id": "../../etc/passwd"})
        self.assertNotIn("/", key)

    def test_missing_session_id_uses_default(self):
        self.assertEqual(claude_hook._session_key({}), "default")

    def test_empty_session_id_uses_default(self):
        self.assertEqual(claude_hook._session_key({"session_id": ""}), "default")

    def test_long_session_id_is_truncated(self):
        key = claude_hook._session_key({"session_id": "a" * 200})
        self.assertLessEqual(len(key), 64)


if __name__ == "__main__":
    unittest.main()
