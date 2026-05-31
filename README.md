# Claude Code → Philips Hue

**Glance at your desk lamp instead of your screen.**

Claude Code already tells you when it's done, but only if your eyes are on
the terminal. This hooks into the agent loop and uses a Hue bulb as an
ambient status indicator, so you can keep working in another window (or
across the room) and still know exactly what Claude is doing.

- 🟡 **warm white**: Claude is working
- 🔵 **blue**:       Claude needs you (permission prompt) + 🔔 Submarine chime
- 🟢 **green**:      turn finished cleanly + 🔔 Glass chime
- 🔴 **red**:        turn ended with an error + 🔔 Basso chime

Sounds always play. Lights can be disabled independently via the
`HUE_ENABLED` environment variable (see below).

Stdlib + `requests`, two small Python files, no daemon, no cloud. Talks
directly to your Hue Bridge over the LAN.

Pairs naturally with the [pushover](../pushover) repo: the light gives you
peripheral-vision status while you're at the desk; pushes catch you when
you've walked away.

## What it does

Hooks into Claude Code via `~/.claude/settings.json` and drives a single
Hue bulb through a small color state machine:

| event              | color           | sound     | meaning                                   |
|--------------------|-----------------|-----------|-------------------------------------------|
| `UserPromptSubmit` | warm white      |           | new turn started, Claude is thinking      |
| `PreToolUse`       | warm white      |           | permission approved, back to work         |
| `PostToolUse`      | (no change)     |           | stashes an error flag if the tool failed  |
| `Notification`     | blue            | Submarine | Claude needs your input (e.g. permission) |
| `Stop` (clean)     | green           | Glass     | turn finished, no errors                  |
| `Stop` (errored)   | red             | Basso     | turn finished, something failed           |
| `SessionEnd`       | warm white      |           | Claude exited                             |

Errors are detected two ways: explicit failure on a `PostToolUse` (non-zero
exit, `is_error: true`, etc.), and a phrase-level heuristic on Claude's
final message ("I encountered an error", "failed to ...", with negation
handling so "no errors found" doesn't trip it).

## Setup

### 1. Buy the hardware

You need:

- A **Philips Hue Bridge** (the round white puck, or the older v2 square
  one). Roughly **$50 to $60** new, often cheaper used.
- At least one **Hue bulb**. For the full color state machine you want a
  *color* bulb (anything labelled "White and Color Ambiance"). White-only
  bulbs work too, they'll just light up at full brightness instead of
  changing hue, so you only get on/off as the signal.

Plug the Bridge into your router via Ethernet, screw the bulb into a lamp,
and power it on.

### 2. Get the bulb on the network

Install the official **Philips Hue** app:

- iOS: <https://apps.apple.com/app/philips-hue/id1055281310>
- Android: Play Store → "Philips Hue"

Open the app, let it discover your Bridge, follow the pairing prompts
(press the round button on the Bridge when asked), then add your bulb
through the app's *Light setup* flow. Give it a name you'll remember.
Something like `"Desk"` works well.

You only need the app for this step. Once the bulb is paired with the
Bridge, this script talks to the Bridge directly over LAN.

### 3. Set up the Python environment

The Bridge API is HTTP, so we need `requests`:

```bash
cd /path/to/phillips_hue
python3 -m venv .venv
.venv/bin/pip install requests ruff mypy
ln -sf ../../hooks/pre-commit .git/hooks/pre-commit
```

`claude_hook.py` is hard-wired to use `.venv/bin/python` next to itself,
so don't install requests system-wide, keep it in the venv. The last
line installs the pre-commit hook (ruff + mypy + tests run on every
commit).

### 4. Pair this script with the Bridge

Press the round **link button** on top of the Bridge, then within 30
seconds run:

```bash
.venv/bin/python hue_green.py setup
```

Output:

```
Discovering Hue Bridge via meethue.com...
Found Bridge at 192.168.x.x
Requesting API username...
Paired successfully.
Saved config to /path/to/phillips_hue/.hue_config.json
```

This writes `.hue_config.json` next to the script with the Bridge's LAN IP
and an API username (basically a per-app token). **Keep this file
private.** Anyone with it can control your lights from inside your
network. It's already in `.gitignore`; format mirrors
`.hue_config.example.json`:

```json
{
  "bridge_ip": "192.168.1.100",
  "username": "your-bridge-api-username-here",
  "light": "5"
}
```

The `"light"` key is optional and only used by the Claude Code hook —
see step 6.

If discovery fails (no internet, weird DNS), find the Bridge's IP from
your router and edit `bridge_ip` by hand. The username step still works
locally.

### 5. Find your light's ID

```bash
.venv/bin/python hue_green.py list
```

```
ID   Name                         On   Type
----------------------------------------------------------------------
1    Bedroom                      no   Extended color light
3    Office Lamp                  yes  Color temperature light
5    Desk (color)                 yes  Extended color light
```

Pick the one you want to use as the Claude indicator. You can refer to it
by ID (`5`) or name (`"Desk (color)"`, or `"Desk"` for partial matches
when unambiguous).

Sanity-check it:

```bash
.venv/bin/python hue_green.py color green 5
.venv/bin/python hue_green.py color blue  5
.venv/bin/python hue_green.py color red   5
.venv/bin/python hue_green.py color warm  5
.venv/bin/python hue_green.py off         5
```

### 6. Point the hook at your light

Add a `"light"` key to `.hue_config.json` with either the ID or name:

```json
{
  "bridge_ip": "192.168.1.100",
  "username": "...",
  "light": "5"
}
```

`"Desk (color)"` works too, as does any unambiguous partial match like
`"Desk"`. If you don't set this, the hook falls back to `DEFAULT_LIGHT`
in `claude_hook.py`.

### 7. Wire the hook into Claude Code

In `~/.claude/settings.json`, register `claude_hook.py` for every event
the state machine cares about:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"type": "command", "command": "python /path/to/phillips_hue/claude_hook.py"}],
    "PreToolUse":       [{"type": "command", "command": "python /path/to/phillips_hue/claude_hook.py"}],
    "PostToolUse":      [{"type": "command", "command": "python /path/to/phillips_hue/claude_hook.py"}],
    "Notification":     [{"type": "command", "command": "python /path/to/phillips_hue/claude_hook.py"}],
    "Stop":             [{"type": "command", "command": "python /path/to/phillips_hue/claude_hook.py"}],
    "SessionEnd":       [{"type": "command", "command": "python /path/to/phillips_hue/claude_hook.py"}]
  }
}
```

The hook itself uses `.venv/bin/python` to spawn `hue_green.py`, so the
top-level `python` Claude invokes can be anything. `claude_hook.py` only
imports stdlib (`json`, `subprocess`, etc.) — `requests` lives behind
the venv, in `hue_green.py`. So leave the hook command as plain
`python` and don't "fix" it to point at the venv; that's not the bug
you think it is.

Send Claude any message and watch the bulb cycle: warm white → green (or
red) at the end.

### 8. Disable lights (sounds only)

Set the `HUE_ENABLED` environment variable to `false` to turn off all
Hue light commands while keeping the macOS sound chimes active:

```bash
export HUE_ENABLED=false
```

Or with [direnv](https://direnv.net/), add to your `.envrc`:

```bash
export HUE_ENABLED=false
```

The default is `true` (lights on). Accepted truthy values: `true`, `yes`,
`1`.

### Throttle and priority

To protect bulb hardware from excessive API calls, `set_color` throttles
requests:

- **Duplicate skip**: if the light is already the requested color, the
  call is skipped entirely.
- **Priority within throttle window** (0.3 seconds): if a new color
  arrives before the window expires, it only goes through if it outranks
  the current color. Priority order (highest first): red > blue > green >
  normal. This ensures an error (red) or permission prompt (blue) is never
  swallowed by a lower-priority "back to working" (normal).
- The throttle state resets at the start of each turn (`UserPromptSubmit`).

## `hue_green.py` as a standalone tool

The light controller is useful by itself, totally independent of Claude:

```bash
hue_green.py setup                        # one-time pairing
hue_green.py list                         # show all lights
hue_green.py caps "Desk"                  # what can this bulb do?
hue_green.py color green "Office Lamp"
hue_green.py color daylight 3
hue_green.py off "Desk"
```

Color presets:
- **Hues**:    `red`, `orange`, `yellow`, `green`, `cyan`, `blue`, `purple`,
               `magenta`, `pink`, `white`
- **Whites**:  `warm` (2700K), `normal` (= warm @ full bri), `neutral` (4000K),
               `cool` (5500K), `daylight` (6500K)

Color-only bulbs fall back to an xy approximation of warm white when given
a `ct`-based preset; white-only bulbs ignore color presets and just turn
on at full brightness. Run `caps <light>` to see what your bulb supports.

## Files

- `claude_hook.py`: invoked by Claude Code, picks the color for each event
- `hue_green.py`: Hue Bridge client (setup, list, caps, color, off)
- `.hue_config.json`: bridge IP + API username (gitignored, **do not commit**)
- `.hue_config.example.json`: template

## State files

State lives under `~/.cache/claude_hue/` (or `$XDG_CACHE_HOME/claude_hue/`
if set):

- `error_flag.<session_id>`: set by `PostToolUse` when a tool failed,
  consumed (and unlinked) by `Stop`. Lives across hook invocations within
  a single turn. Per-session so concurrent Claude Code sessions don't
  clobber each other's error state.
- `light_state.<session_id>`: tracks the last color sent and its
  timestamp, used by the throttle to skip duplicates and enforce priority.
  Cleared at the start of each turn (`UserPromptSubmit`).
- `debug.log`: append-only log of every event payload. Useful when
  triaging "why didn't the light change?". Throttle skips are also
  logged here. Set `DEBUG_LOG = None` in `claude_hook.py` to silence it.
  Spawn failures (e.g. `.venv/bin/python` missing) also land here, so
  check it first if the bulb stops responding to events.

## Tests

Stdlib `unittest`, no extra deps. Covers the error-detection heuristics and
the light-name resolver:

```bash
.venv/bin/python -m unittest test_hooks.py -v
```

## Tested with

- **Claude Code** 2.1.124
- **Python** 3.12 (venv) + `requests`
- **Hue Bridge** v2 (square), firmware 1.x via the legacy `/api` endpoint
- **Bulb**: Hue White and Color Ambiance E26
- **Ubuntu** 24.04 LTS (the host running Claude Code)

The Bridge's local API (`http://<ip>/api/<username>/...`) has been stable
for years. This code uses the v1 endpoint because it's simpler than the
v2 / CLIP API and every Hue Bridge in the wild speaks it.

## Disclaimer

This software sends commands to your Philips Hue bulbs via the Bridge
API. While throttling and priority logic reduce unnecessary calls, the
authors are not responsible for any damage to your bulbs, Bridge, or
network resulting from use of this tool. Use at your own risk.

## License

[MIT](LICENSE). Fork it, ship it, light up your desk however you want.

## What we learned

### The light has more states than you think

A first pass is "green = done, red = error." That's not enough. Three
things bit us:

1. **Permission prompts.** When Claude asks `Do you want to allow this
   tool?`, it fires a `Notification`. Without a blue state, the light
   stays "working" while Claude is actually waiting on you, which defeats
   the whole point. Hence 🔵.
2. **The light getting stuck on blue after you approve.** `Notification`
   turns it blue, but there's no `NotificationDismiss` event. Solution:
   listen for `PreToolUse` (which fires *after* permission is granted,
   right before the tool actually runs) and flip back to warm white.
3. **Idle-timeout pings.** Claude Code emits a `Notification` ~60s after
   any `Stop` saying "Claude is waiting for your input". That's not a
   real "needs input" signal, just an idle nudge. Filter out
   `notification_type` of `idle` / `idle_prompt` or your light goes blue
   one minute after every clean turn.

### Errors hide in two places

`PostToolUse` carries `tool_response.is_error` / non-zero `exitCode`, so
explicit tool failures are easy. But Claude often *handles* a tool error
gracefully and then writes "I couldn't do X because..." in the final
message, in which case `PostToolUse` already cleared the error and you'd
miss it.

The fix: also scan the last assistant message for error phrasing
(`"i encountered an error"`, `"failed to "`, `"unable to "`, etc.), with
negation guards so `"no errors found"` and `"completed successfully"`
don't false-positive. Either signal flips the light red.

### Resolving lights by name beats hard-coding IDs

The Bridge gives every light a numeric ID (`"5"`), and the obvious thing
to do is hard-code it. But Hue IDs aren't stable: factory-reset a bulb
or re-add it and the number changes. `resolve_light()` accepts either an
ID or a (case-insensitive, partial-match) name, so configs survive bulb
churn:

```json
{ "light": "Desk" }          // works as long as exactly one bulb name contains "desk"
{ "light": "5" }             // works if you trust the ID to stay put
{ "light": "Desk (color)" }  // exact name, most robust
```

### Bridge pairing is push-button, not credentialed

The Hue API doesn't have signups or OAuth. To pair, you POST to
`/api` with a `devicetype` field within 30 seconds of someone physically
pressing the link button on the Bridge. The Bridge mints a random
username and hands it back; that token is now your "API key."

This means: no credentials to leak, but also `.hue_config.json` is the
*only* secret. Anyone on your LAN with that file can control your
lights. Keep it gitignored (already done in this repo).

### Bridge discovery has a cloud fallback

`discovery.meethue.com` returns `[{"id": "...", "internalipaddress":
"192.168.1.x"}]` for any Bridge phoning home from your public IP. It's
the easy path. If it fails (offline, ISP weirdness, multiple WANs), the
Bridge also responds to mDNS (`_hue._tcp`) and SSDP, but it's usually
faster to just check your router's DHCP table and edit `bridge_ip` by
hand. The username step still works locally without internet.

### Color presets vs color temperature presets

Hue has two color models that don't overlap cleanly:

- **`hue` + `sat`** (or `xy`): full-color bulbs, used for red/green/blue
- **`ct`** (mireds, 153 cool to 500 warm): white-temperature bulbs,
  used for warm/neutral/daylight

A color-only bulb that accepts `hue`/`sat` will silently *ignore* a `ct`
command. A white-temperature-only bulb does the inverse. The `cmd_color`
function probes the bulb's reported `type` and either dispatches the
right preset, falls back to an xy approximation, or just turns the bulb
on at full brightness, so configs don't break when you swap a color
bulb for a white one.
