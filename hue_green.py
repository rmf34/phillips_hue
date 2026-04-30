"""
Control a Philips Hue light from Python.

You can refer to a light by its numeric ID *or* by its name (case-insensitive,
quote it if it has spaces). Examples:

    python hue_green.py setup                       # one-time pairing
    python hue_green.py list                        # show all lights
    python hue_green.py caps "Office Lamp"          # what can this bulb do?
    python hue_green.py color green "Office Lamp"
    python hue_green.py color warm  3
    python hue_green.py off "Office Lamp"

Built-in color names:
    Colors  : red, orange, yellow, green, cyan, blue, purple, magenta, pink, white
    Whites  : warm (2700K), normal (= warm), neutral (4000K), cool (5500K), daylight (6500K)

Requires: pip install requests
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).resolve().parent / ".hue_config.json"
APP_NAME = "hue_green_script"

# ---------- color presets ----------
# Color presets use hue (0-65535) + sat (0-254). Bulbs that don't support color
# will silently ignore these and only the brightness will change.
COLOR_PRESETS: dict[str, dict] = {
    "red":     {"hue": 0,     "sat": 254},
    "orange":  {"hue": 8500,  "sat": 254},
    "yellow":  {"hue": 12750, "sat": 254},
    "green":   {"hue": 25500, "sat": 254},
    "cyan":    {"hue": 35000, "sat": 254},
    "blue":    {"hue": 46920, "sat": 254},
    "purple":  {"hue": 50000, "sat": 254},
    "magenta": {"hue": 56100, "sat": 254},
    "pink":    {"hue": 60000, "sat": 180},
    "white":   {"sat": 0},  # let the bulb pick its native white
}

# White presets use ct (mireds, 153 cool .. 500 warm). Color-only bulbs that
# don't support ct will fall back to hue/sat (same xy as ~2700K warm white).
WHITE_PRESETS: dict[str, dict] = {
    "warm":     {"ct": 366},                 # ~2700K
    "normal":   {"ct": 366, "bri": 254},     # warm @ 100% brightness
    "neutral":  {"ct": 250},   # ~4000K
    "cool":     {"ct": 182},   # ~5500K
    "daylight": {"ct": 153},   # ~6500K
}

DEFAULT_BRI = 254  # 0-254; 254 == 100%


# ---------- config ----------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            "No Hue config found. Run `python hue_green.py setup` first "
            "(after pressing the Bridge link button)."
        )
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"Saved config to {CONFIG_PATH}")


# ---------- bridge discovery + pairing ----------

def discover_bridge_ip() -> str:
    print("Discovering Hue Bridge via meethue.com...")
    r = requests.get("https://discovery.meethue.com/", timeout=10)
    r.raise_for_status()
    bridges = r.json()
    if not isinstance(bridges, list) or not bridges:
        raise SystemExit(
            "No Bridge found. Check it's powered on and on the same network, "
            "or set bridge_ip manually in .hue_config.json."
        )
    first = bridges[0]
    if not isinstance(first, dict) or "internalipaddress" not in first:
        raise SystemExit(
            "Discovery returned an unexpected response shape. "
            "Set bridge_ip manually in .hue_config.json."
        )
    ip = first["internalipaddress"]
    print(f"Found Bridge at {ip}")
    return ip


def pair_with_bridge(ip: str) -> str:
    hostname = socket.gethostname()[:19]
    body = {"devicetype": f"{APP_NAME}#{hostname}"}
    print("Requesting API username... (press the Bridge link button if you haven't)")
    r = requests.post(f"http://{ip}/api", json=body, timeout=10)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list) or not payload:
        raise SystemExit(
            "Bridge returned an unexpected pairing response. "
            "Try again after pressing the link button."
        )
    data = payload[0]
    if not isinstance(data, dict):
        raise SystemExit("Bridge returned an unexpected pairing response.")
    if "error" in data:
        desc = data["error"].get("description", "unknown error")
        raise SystemExit(
            f"Pairing failed: {desc}. "
            "Press the link button on the Bridge and try again within 30s."
        )
    if "success" not in data or "username" not in data["success"]:
        raise SystemExit("Bridge returned no username. Try again.")
    print("Paired successfully.")
    return data["success"]["username"]


def cmd_setup() -> None:
    ip = discover_bridge_ip()
    for attempt in range(3):
        try:
            username = pair_with_bridge(ip)
            save_config({"bridge_ip": ip, "username": username})
            return
        except SystemExit as e:
            if attempt == 2:
                raise
            print(e)
            print("Retrying in 10 seconds... press the link button now.")
            time.sleep(10)


# ---------- API helpers ----------

def api(path: str) -> str:
    cfg = load_config()
    return f"http://{cfg['bridge_ip']}/api/{cfg['username']}{path}"


def get_lights() -> dict:
    r = requests.get(api("/lights"), timeout=5)
    r.raise_for_status()
    return r.json()


def bulb_capabilities(light: dict) -> tuple[bool, bool]:
    """Return (has_color, has_ct) for a Bridge light dict.

    Prefers the explicit `capabilities.control` block when present, falls
    back to the `type` string. The `type` fallback uses word-boundary checks
    so `"Color temperature light"` isn't treated as a color bulb.
    """
    caps = light.get("capabilities", {}).get("control", {})
    kind = light.get("type", "").lower()
    has_color = (
        "colorgamut" in caps
        or "colorgamuttype" in caps
        or "extended color" in kind
        or kind == "color light"
    )
    has_ct = (
        "ct" in caps
        or "extended color" in kind
        or "color temperature" in kind
        or kind == "temperature light"
    )
    return has_color, has_ct


def resolve_light(identifier: str) -> tuple[str, dict]:
    """Accept either '3' or 'Office Lamp' and return (id, light_dict)."""
    lights = get_lights()
    # Numeric ID match
    if identifier in lights:
        return identifier, lights[identifier]
    # Case-insensitive name match
    target = identifier.strip().lower()
    matches = [(lid, l) for lid, l in lights.items()
               if l.get("name", "").lower() == target]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        # Fall back to substring search to be friendly
        partial = [(lid, l) for lid, l in lights.items()
                   if target in l.get("name", "").lower()]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise SystemExit(
                f"No light matches '{identifier}'. Run `list` to see options."
            )
        names = ", ".join(repr(l.get("name", "?")) for _, l in partial)
        raise SystemExit(f"'{identifier}' is ambiguous. Matches: {names}")
    names = ", ".join(repr(l.get("name", "?")) for _, l in matches)
    raise SystemExit(f"Multiple lights named '{identifier}': {names}")


def set_state(light_id: str, state: dict) -> None:
    r = requests.put(api(f"/lights/{light_id}/state"), json=state, timeout=5)
    r.raise_for_status()
    for item in r.json():
        if "error" in item:
            print(f"  error: {item['error']['description']}")
        else:
            for k, v in item.get("success", {}).items():
                print(f"  set {k} -> {v}")


# ---------- commands ----------

def cmd_list() -> None:
    lights = get_lights()
    if not lights:
        print("No lights found.")
        return
    print(f"{'ID':<4} {'Name':<28} {'On':<4} {'Type'}")
    print("-" * 70)
    for light_id, light in lights.items():
        print(f"{light_id:<4} {light.get('name', '?'):<28} "
              f"{'yes' if light.get('state', {}).get('on') else 'no':<4} "
              f"{light.get('type', '?')}")


def cmd_caps(identifier: str) -> None:
    light_id, light = resolve_light(identifier)
    kind = light.get("type", "?")
    caps = light.get("capabilities", {}).get("control", {})
    has_color, has_ct = bulb_capabilities(light)
    print(f"Light #{light_id}: {light.get('name', '?')}")
    print(f"  Type        : {kind}")
    if "ct" in caps:
        print(f"  Color temp  : {caps['ct'].get('min')}-{caps['ct'].get('max')} mireds")
    if "colorgamuttype" in caps:
        print(f"  Color gamut : {caps['colorgamuttype']}")
    print(f"  Supports color: {'yes' if has_color else 'no'}")
    print(f"  Supports white temp: {'yes' if has_ct else 'no'}")


def cmd_color(color_name: str, identifier: str) -> None:
    color_name = color_name.lower()
    light_id, light = resolve_light(identifier)
    has_color, has_ct = bulb_capabilities(light)

    if color_name in COLOR_PRESETS:
        if not has_color:
            print(f"Note: '{light.get('name', '?')}' doesn't support color "
                  f"(type: {light.get('type')}). Setting brightness only.")
            set_state(light_id, {"on": True, "bri": DEFAULT_BRI})
            return
        state = {"on": True, "bri": DEFAULT_BRI, **COLOR_PRESETS[color_name]}
    elif color_name in WHITE_PRESETS:
        if has_ct:
            state = {"on": True, "bri": DEFAULT_BRI, **WHITE_PRESETS[color_name]}
        elif has_color:
            # color-only bulb: approximate warm white via xy
            state = {"on": True, "bri": DEFAULT_BRI, "xy": [0.45, 0.41]}
        else:
            state = {"on": True, "bri": DEFAULT_BRI}
    else:
        raise SystemExit(
            f"Unknown color '{color_name}'. Choices: "
            f"{', '.join(sorted({*COLOR_PRESETS, *WHITE_PRESETS}))}"
        )
    set_state(light_id, state)


def cmd_off(identifier: str) -> None:
    light_id, _ = resolve_light(identifier)
    set_state(light_id, {"on": False})


# ---------- CLI ----------

USAGE = """\
usage:
  python hue_green.py setup
  python hue_green.py list
  python hue_green.py caps  <light_id_or_name>
  python hue_green.py color <color> <light_id_or_name>
  python hue_green.py off   <light_id_or_name>

Backwards-compatible shortcuts (used by the Claude Code hooks):
  python hue_green.py green  <light>     # same as: color green
  python hue_green.py normal <light>     # same as: color normal
"""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(USAGE); return 1
    cmd = argv[1]
    try:
        if cmd == "setup":
            cmd_setup()
        elif cmd == "list":
            cmd_list()
        elif cmd == "caps" and len(argv) >= 3:
            cmd_caps(argv[2])
        elif cmd == "color" and len(argv) >= 4:
            cmd_color(argv[2], argv[3])
        elif cmd == "off" and len(argv) >= 3:
            cmd_off(argv[2])
        # legacy shortcuts so existing Claude Code hooks keep working
        elif cmd in COLOR_PRESETS or cmd in WHITE_PRESETS:
            if len(argv) < 3:
                print(USAGE); return 1
            cmd_color(cmd, argv[2])
        else:
            print(USAGE); return 1
    except requests.RequestException as e:
        print(f"Network error talking to Bridge: {e}"); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
