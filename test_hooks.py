"""Unit tests for the bits of logic that actually branch.

Run with:
    .venv/bin/python -m unittest test_hooks.py -v
"""
from __future__ import annotations

import unittest
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


if __name__ == "__main__":
    unittest.main()
