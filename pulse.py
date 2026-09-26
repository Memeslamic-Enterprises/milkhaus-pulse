"""Watch the MILKHAUS game servers from outside, and say so when they die.

Runs on GitHub's infrastructure precisely because the homelab cannot report
its own total outage: when the internet at home is gone, everything that
lives there - the game servers AND the Discord bot - goes dark together.
This is the one voice that survives, so it posts to Discord as teto.

The Minecraft probe is a real Server List Ping, not a TCP connect: the
hostname fronts a relay, and a relay accepts connections whether or not the
homelab behind it is alive. Only a status payload proves the actual server
answered.

State (last verdict, whether an outage was announced) rides between runs in
the Actions cache; a transient blip is absorbed by requiring two consecutive
failing runs before anything is said.

Each configured target - DISCORD_* (Budget Mania) and MILKCAT_* (Milkcat's
Support) - is posted to independently, and a failed one is retried next run.
Each prefers its WEBHOOK_URL; with only BOT_TOKEN and CHANNEL_ID it posts
through that bot. Either way the message appears as teto.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

MC_HOST = "mc.milkhaus.net"
MC_PORT = 25565
WEB_URL = "https://mc.milkhaus.net"
# If GitHub's own runner cannot reach Discord, the runner is the problem and
# this run has nothing trustworthy to say.
CONTROL_URL = "https://discord.com/api/v10/gateway"

STATE_FILE = "state/pulse.json"
TIMEOUT = 10
# Failing runs in a row before the outage is announced (cron is */5, so two
# runs is roughly ten minutes of confirmed silence).
RUNS_BEFORE_ANNOUNCING = 2
TARGET_PREFIXES = ("DISCORD_", "MILKCAT_")

DOWN_MESSAGE = (
    "\N{WARNING SIGN} word from outside the walls: the homelab has dropped off "
    "the internet. minecraft isn't answering ({mc}), the web map's dark, and "
    "everything that lives back home - zomboid, the casino, my main brain - is "
    "down with it. i'll shout the moment we're back."
)
UP_MESSAGE = (
    "\N{WHITE HEAVY CHECK MARK} and we're back - the homelab is answering again "
    "after {gap}. minecraft's up, servers live, brain reattached. carry on."
)


def _varint(value: int) -> bytes:
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        out += struct.pack("B", byte | (0x80 if value else 0))
        if not value:
            return out


def _read_varint(sock: socket.socket) -> int:
    value = shift = 0
    while True:
        raw = sock.recv(1)
        if not raw:
            raise ConnectionError("closed mid-varint")
        byte = raw[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
        shift += 7
        if shift > 35:
            raise ValueError("varint too long")


def minecraft_alive(host: str, port: int) -> tuple[bool, str]:
    """(alive, detail) via a real Server List Ping - handshake, status
    request, and a JSON payload that only the true server can produce."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            sock.settimeout(TIMEOUT)
            encoded_host = host.encode()
            handshake = (
                _varint(0)  # packet id
                + _varint(-1 & 0x7FFFFFFF)  # protocol: "whatever you are"
                + _varint(len(encoded_host)) + encoded_host
                + struct.pack(">H", port)
                + _varint(1)  # next state: status
            )
            sock.sendall(_varint(len(handshake)) + handshake)
            sock.sendall(_varint(1) + _varint(0))  # status request
            _length = _read_varint(sock)
            packet_id = _read_varint(sock)
            if packet_id != 0:
                return False, f"odd packet id {packet_id}"
            payload_len = _read_varint(sock)
            payload = b""
            while len(payload) < payload_len:
                chunk = sock.recv(min(4096, payload_len - len(payload)))
                if not chunk:
                    raise ConnectionError("closed mid-status")
                payload += chunk
            status = json.loads(payload.decode("utf-8", "replace"))
            players = status.get("players", {})
            return True, f"{players.get('online', '?')}/{players.get('max', '?')} online"
    except Exception as exc:  # noqa: BLE001 - any failure is the same verdict
        return False, f"{type(exc).__name__}: {exc}"[:120]


def web_alive(url: str) -> tuple[bool, str]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "milkhaus-pulse/1.0"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status < 500, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # 4xx means something answered; 5xx from a relay means the origin is gone.
        return exc.code < 500, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"[:60]


def configured_targets() -> tuple[str, ...]:
    targets = []
    for prefix in TARGET_PREFIXES:
        webhook = os.environ.get(f"{prefix}WEBHOOK_URL", "").strip()
        token = os.environ.get(f"{prefix}BOT_TOKEN", "").strip()
        channel = os.environ.get(f"{prefix}CHANNEL_ID", "").strip()
        if webhook or (token and channel):
            targets.append(prefix)
    return tuple(targets)


def post_to_discord(prefix: str, text: str) -> None:
    webhook = os.environ.get(f"{prefix}WEBHOOK_URL", "").strip()
    token = os.environ.get(f"{prefix}BOT_TOKEN", "").strip()
    channel = os.environ.get(f"{prefix}CHANNEL_ID", "").strip()
    if webhook:
        url, headers = webhook, {"Content-Type": "application/json"}
    elif token and channel:
        url = f"https://discord.com/api/v10/channels/{channel}/messages"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (milkhaus-pulse, 1.0)",
        }
    body = json.dumps({"content": text}).encode()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        print(f"posted to {prefix}", response.status)


def post_to_targets(text: str, targets: tuple[str, ...]) -> tuple[list[str], bool]:
    if not configured_targets():
        print("no Discord credentials configured; would have said:", text)
    succeeded = []
    failed = False
    for prefix in targets:
        try:
            post_to_discord(prefix, text)
            succeeded.append(prefix)
        except Exception as exc:  # noqa: BLE001 - targets must be independent
            print(f"{prefix} post failed: {type(exc).__name__}: {exc}")
            failed = True
    return succeeded, failed


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
            return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def humanize(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} minutes"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def main() -> int:
    control_ok, control_detail = web_alive(CONTROL_URL)
    if not control_ok:
        print(f"runner cannot even reach Discord ({control_detail}) - saying nothing")
        return 0

    mc_ok, mc_detail = minecraft_alive(MC_HOST, MC_PORT)
    web_ok, web_detail = web_alive(WEB_URL)
    down = not mc_ok and not web_ok
    print(f"minecraft: {'up' if mc_ok else 'DOWN'} ({mc_detail}) | "
          f"web: {'up' if web_ok else 'DOWN'} ({web_detail})")

    state = load_state()
    if state.get("announced") and "told" not in state:
        state["told"] = ["DISCORD_"]
    state.setdefault("told", [])
    now = time.time()
    targets = configured_targets()
    post_failed = False

    if down:
        streak = int(state.get("streak", 0)) + 1
        state.update({"streak": streak, "since": state.get("since") or now})
        if streak >= RUNS_BEFORE_ANNOUNCING:
            untold = tuple(target for target in targets if target not in state["told"])
            if untold or not targets and not state.get("announced"):
                succeeded, post_failed = post_to_targets(
                    DOWN_MESSAGE.format(mc=mc_detail), untold
                )
                state["told"].extend(succeeded)
            if not state.get("announced"):
                state["announced"] = True
                state["announced_at"] = datetime.now(timezone.utc).isoformat()
    else:
        if state.get("announced"):
            gap = humanize(now - float(state.get("since") or now))
            pending = tuple(target for target in targets if target in state["told"])
            succeeded, post_failed = post_to_targets(UP_MESSAGE.format(gap=gap), pending)
            failed = [target for target in pending if target not in succeeded]
            if failed:
                state.update({"streak": 0, "told": failed})
            else:
                state = {"streak": 0, "announced": False, "since": None, "told": []}
        else:
            state = {"streak": 0, "announced": False, "since": None, "told": []}

    save_state(state)
    return 1 if post_failed else 0


if __name__ == "__main__":
    sys.exit(main())
