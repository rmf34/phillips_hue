"""
Claude Code hook dispatcher → Philips Hue light + macOS chime.

Wired up from ~/.claude/settings.json. Reads hook event JSON from stdin,
picks the right color and sound, and invokes hue_green.py / afplay.

Lights are gated by the HUE_ENABLED env var (default: true).
Sounds always play.

Throttle: duplicate colors are skipped, and rapid transitions within
HUE_THROTTLE_SECONDS respect a priority order (red > blue > green >
normal) so urgent signals are never swallowed by lower-priority ones.

State machine:
    UserPromptSubmit  -> warm white (Claude working)    + reset throttle
    Notification      -> blue (needs input/permission)  + Submarine chime
    PreToolUse        -> warm white (permission approved, back to work)
    PostToolUse       -> stashes an error flag if the tool failed
    Stop (no errors)  -> green (done cleanly)           + Glass chime
    Stop (with error) -> red   (tool failed this turn)  + Basso chime
    SessionEnd        -> warm white (Claude exited)
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---- config ----
HUE_ENABLED = (os.environ.get("HUE_ENABLED") or "true").lower() in ("1", "true", "yes")

HERE        = Path(__file__).resolve().parent
HUE_SCRIPT  = str(HERE / "hue_green.py")
VENV_PYTHON = str(HERE / ".venv" / "bin" / "python")
HUE_CONFIG  = HERE / ".hue_config.json"

# Default light to drive. Overridable by adding "light": "<id-or-name>" to
# .hue_config.json, which is what most users should do (keeps user state out
# of source).
DEFAULT_LIGHT = "5"

SOUND_SUCCESS      = "/System/Library/Sounds/Glass.aiff"
SOUND_ERROR        = "/System/Library/Sounds/Basso.aiff"
SOUND_NOTIFICATION = "/System/Library/Sounds/Submarine.aiff"

# Minimum seconds between Hue API calls. Within this window, only
# higher-priority colors go through (red > blue > green > normal).
HUE_THROTTLE_SECONDS = 0.3

# Higher number = higher priority. A color change within the throttle window
# is only sent if the new color outranks the current one.
COLOR_PRIORITY: dict[str, int] = {
    "normal": 0,
    "green": 1,
    "blue": 2,
    "red": 3,
}

# State + debug log live under XDG cache so they're per-user and not in
# world-writable /tmp.
CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "claude_hue"

# Set to None to disable the debug log.
DEBUG_LOG: Path | None = CACHE_DIR / "debug.log"


def _ensure_cache_dir() -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _light_id() -> str:
    try:
        cfg = json.loads(HUE_CONFIG.read_text())
        light = cfg.get("light")
        if isinstance(light, (str, int)) and str(light).strip():
            return str(light)
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_LIGHT


def _session_key(data: dict) -> str:
    """Stable per-session suffix so concurrent Claude sessions don't share state."""
    sid = str(data.get("session_id") or "default")
    # session_id is normally a uuid; sanitize defensively in case it isn't.
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:64] or "default"
    return sid


def _error_flag_path(data: dict) -> Path:
    return CACHE_DIR / f"error_flag.{_session_key(data)}"


def _light_state_path(data: dict) -> Path:
    return CACHE_DIR / f"light_state.{_session_key(data)}"


def _read_light_state(data: dict) -> tuple[str, float]:
    """Return (last_color, timestamp) or ("", 0.0) if no state."""
    try:
        raw = _light_state_path(data).read_text()
        parts = raw.strip().split("\n", 1)
        return parts[0], float(parts[1])
    except (OSError, ValueError, IndexError):
        return "", 0.0


def _write_light_state(data: dict, color: str) -> None:
    try:
        _light_state_path(data).write_text(f"{color}\n{time.time()}")
    except OSError:
        pass


def _clear_light_state(data: dict) -> None:
    try:
        _light_state_path(data).unlink(missing_ok=True)
    except OSError:
        pass


def _debug(msg: str) -> None:
    if DEBUG_LOG is None:
        return
    try:
        with DEBUG_LOG.open("a") as f:
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            f.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


def _spawn(cmd: list[str]) -> None:
    """Fire-and-forget — detach so the hook returns immediately."""
    if not Path(cmd[0]).exists():
        _debug(f"spawn skipped, executable missing: {cmd[0]}")
        return
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        _debug(f"spawn failed for {cmd!r}: {e}")


def set_color(color: str, data: dict) -> None:
    if not HUE_ENABLED:
        return
    last_color, last_time = _read_light_state(data)
    now = time.time()
    if color == last_color:
        _debug(f"throttle: skipping duplicate {color}")
        return
    if now - last_time < HUE_THROTTLE_SECONDS:
        new_pri = COLOR_PRIORITY.get(color, 0)
        old_pri = COLOR_PRIORITY.get(last_color, 0)
        if new_pri <= old_pri:
            _debug(f"throttle: skipping {color} (priority {new_pri} <= {last_color} priority {old_pri})")
            return
    _write_light_state(data, color)
    _spawn([VENV_PYTHON, HUE_SCRIPT, "color", color, _light_id()])


def play(sound_path: str) -> None:
    _spawn(["/usr/bin/afplay", sound_path])


def read_hook_input() -> dict:
    raw = ""
    try:
        raw = sys.stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
    except Exception:
        parsed = {}
    if DEBUG_LOG is not None:
        try:
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            with DEBUG_LOG.open("a") as f:
                f.write(f"\n===== {stamp} =====\n")
                if not raw:
                    f.write("(empty stdin)\n")
                else:
                    f.write(raw)
                    if not raw.endswith("\n"):
                        f.write("\n")
        except OSError:
            pass
    return parsed


def tool_errored(data: dict) -> bool:
    """Best-effort detection of whether the tool call failed."""
    response = data.get("tool_response") or {}
    if not isinstance(response, dict):
        return False
    if response.get("is_error") is True:
        return True
    # Bash tool uses camelCase "exitCode"; older/other tools may use snake_case.
    for key in ("exitCode", "exit_code"):
        val = response.get(key)
        if isinstance(val, int) and val != 0:
            return True
    if response.get("error"):
        return True
    if str(response.get("status", "")).lower() in {"error", "failed"}:
        return True
    return False


# Phrases in Claude's final message that strongly suggest the turn ended in an
# error. Kept conservative to avoid false positives on normal phrasing like
# "no errors found". Lower-cased comparison.
ERROR_PHRASES = (
    "error —",
    "error -",
    "error:",
    "i encountered an error",
    "i got an error",
    "i hit an error",
    "i ran into an error",
    "failed to ",
    "unable to ",
    "could not ",
    "couldn't ",
)
ERROR_NEGATIONS = (
    "no error",
    "without error",
    "no failure",
    "no issue",
    "successfully",
)

_SENTENCE_SPLIT = re.compile(r"[.!?\n;]+")


def response_indicates_error(data: dict) -> bool:
    """Heuristic: does Claude's final message read like an error report?

    Per-sentence: a negation ("successfully", "no error") in one sentence
    only neutralizes that sentence. So "Successfully read X but failed to
    write Y" still flags as an error because the second clause has no
    negation.
    """
    msg = str(data.get("last_assistant_message", "") or "").lower()
    if not msg:
        return False
    for sentence in _SENTENCE_SPLIT.split(msg):
        if not sentence.strip():
            continue
        if any(neg in sentence for neg in ERROR_NEGATIONS):
            continue
        if any(p in sentence for p in ERROR_PHRASES):
            return True
    return False


def main() -> int:
    _ensure_cache_dir()
    data = read_hook_input()
    event = data.get("hook_event_name", "")
    error_flag = _error_flag_path(data)

    if event == "UserPromptSubmit":
        error_flag.unlink(missing_ok=True)   # new turn, clean slate
        _clear_light_state(data)
        set_color("normal", data)

    elif event == "Notification":
        # Skip idle-timeout notifications ("Claude is waiting for your input"),
        # which fire ~60s after any Stop regardless of whether Claude actually
        # needs something. Only signal blue for active needs like permission
        # prompts.
        ntype = str(data.get("notification_type", "")).lower()
        if ntype not in {"idle_prompt", "idle"}:
            set_color("blue", data)
            play(SOUND_NOTIFICATION)

    elif event == "PreToolUse":
        # Fires when a tool is actually about to run (i.e., after any permission
        # approval). If the light was blue waiting for the user, flip it back
        # to "working" now that Claude is moving again.
        set_color("normal", data)

    elif event == "PostToolUse":
        if tool_errored(data):
            try:
                error_flag.write_text("1")
            except OSError as e:
                _debug(f"could not write error flag {error_flag}: {e}")
        # no light change — keep "working" state until Stop

    elif event == "Stop":
        errored = error_flag.exists() or response_indicates_error(data)
        error_flag.unlink(missing_ok=True)
        if errored:
            set_color("red", data)
            play(SOUND_ERROR)
        else:
            set_color("green", data)
            play(SOUND_SUCCESS)

    elif event == "SessionEnd":
        error_flag.unlink(missing_ok=True)
        set_color("normal", data)

    return 0


if __name__ == "__main__":
    sys.exit(main())
