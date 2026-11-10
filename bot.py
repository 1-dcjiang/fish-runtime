"""bot.py — run the character on Telegram and/or Discord, unattended.

This is the "third path" from the README: a resident process with a channel, not a skill. It reuses the
same `memory/` folder, the same weights and the same persona card as the CLI — it only adds transports.

Design rules that keep it safe to leave running:
  * an allowlist is mandatory; with no allowlist the bot refuses to start instead of answering strangers
  * bot-authored messages are ignored, so two bots cannot talk each other into a loop
  * one reply per message, chunked at the platform limit; long input is truncated before it reaches memory
  * tokens come from the environment only, and message bodies never go to stdout logs

Usage:
  python bot.py --config config.bot.json                 # telegram + discord, as configured
  python bot.py --config config.bot.json --platform telegram
  python bot.py --config config.bot.json --once          # one polling round, then exit (debug)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import adapters
import fish
import ws

TELEGRAM_API = "https://api.telegram.org"
DISCORD_API = "https://discord.com/api/v10"
GATEWAY_URL = f"{DISCORD_API}/gateway/bot"

# GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
# MESSAGE_CONTENT is a privileged intent: enable it in the Developer Portal or the bot sees empty text.
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)

HELP = (
    "I keep a weighted, fading memory of what we talk about. Plain messages are answered in voice; "
    "these commands are the maintenance hatch:\n"
    "/status — how much I remember right now\n"
    "/verify <id> — confirm a memory so it can become identity\n"
    "/help — this text"
)

BOT_DEFAULTS = {
    "platform": ["telegram"],
    "state_file": "bot-state.json",
    "record_episodes": "auto",
    "run_cycle_on_tick": True,
    # replies quote recalled memory by default — in a group too; set false to keep the store out of replies
    "reply": {"include_memory": True},
    "model": {"mode": "offline"},
    "channel": {"type": "stdout"},
    "beat": {"interval_seconds": 900, "quiet_hours": [23, 7], "min_gap_minutes": 20,
             "nudge_after_hours": 6, "nudge_cooldown_hours": 12,
             "max_messages_per_day": 3, "min_weight": 0.5},
    "telegram": {"token_env": "FISH_TG_TOKEN", "allow_chat_ids": [], "allow_user_ids": []},
    "discord": {"token_env": "FISH_DISCORD_TOKEN", "allow_channel_ids": [], "allow_user_ids": []},
    # Off by default: a bot that messages you first is a behaviour you opt into, and the target has to be
    # one you already allowlisted — the config you write is the only thing that decides where it speaks.
    # include_memory=False because a beat can land in a group; quoting a stored memory there leaks it.
    "proactive": {"enabled": False, "platform": "", "target": "", "interval_seconds": 0,
                  "include_memory": False},
    "limits": {"max_messages_per_minute": 6, "max_message_chars": 800},
}


def http_json(url: str, params: dict | None = None, payload: dict | None = None,
              headers: dict | None = None, timeout: float = 30.0) -> dict:
    """The only network primitive in this file; injected in tests so nothing here touches a socket."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"error: config not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: config is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(raw, dict):
        # a list or a bare value would crash merge(); say so instead of throwing AttributeError
        print(f"error: config must contain a JSON object, got {type(raw).__name__}", file=sys.stderr)
        raise SystemExit(2)
    return fish.merge(fish.merge(fish.DEFAULTS, BOT_DEFAULTS), raw)


def platforms_of(cfg: dict) -> list[str]:
    platforms = cfg.get("platform") or []
    return [platforms] if isinstance(platforms, str) else list(platforms)


# The config is hand-editable JSON, so a wrong *shape* is a real failure mode: `beat.quiet_hours: 8` is not
# a typo the runtime can shrug off, it makes every beat throw. One table, read by both validate() and
# setup.py, so a value the configurator accepts is a value the runtime can use.
TYPE_RULES = {
    "beat.interval_seconds": ("int", 0, None),
    "beat.quiet_hours": ("hours", None, None),
    "beat.min_gap_minutes": ("int", 0, None),
    "beat.nudge_after_hours": ("num", 0, None),
    "beat.nudge_cooldown_hours": ("num", 0, None),
    "beat.max_messages_per_day": ("int", 0, None),
    "beat.min_weight": ("num", 0, 1),
    "beat.include_memory": ("bool", None, None),
    "limits.max_messages_per_minute": ("int", 1, None),
    "limits.max_message_chars": ("int", 1, None),
    "proactive.enabled": ("bool", None, None),
    "proactive.platform": ("str", None, None),
    "proactive.target": ("id", None, None),
    "proactive.interval_seconds": ("int", 0, None),
    "proactive.include_memory": ("bool", None, None),
    "reply.include_memory": ("bool", None, None),
    "telegram.allow_chat_ids": ("ids", None, None),
    "telegram.allow_user_ids": ("ids", None, None),
    "discord.allow_channel_ids": ("ids", None, None),
    "discord.allow_user_ids": ("ids", None, None),
    "telegram.token_env": ("str", None, None),
    "discord.token_env": ("str", None, None),
    "model.mode": ("str", None, None),
    "model.timeout": ("int", 1, None),
}

TYPE_HINTS = {
    "int": "a whole number",
    "num": "a number",
    "bool": "true or false",
    "str": "a non-empty string",
    "id": "a chat/user id (number or string)",
    "hours": "two hours 0–23, like [23,7]",
    "ids": "a list of ids, like [123456789]",
}


def value_of(cfg: dict, path: str):
    if "." not in path:
        return cfg.get(path)
    root, leaf = path.split(".", 1)
    section = cfg.get(root)
    return section.get(leaf) if isinstance(section, dict) else None


def check_type(path: str, value) -> str | None:
    """`None` when the value has the shape the runtime needs, otherwise a sentence saying what it wanted."""
    rule = TYPE_RULES.get(path)
    if not rule:
        return None
    kind, low, high = rule
    hint = TYPE_HINTS[kind]
    if kind == "bool":
        return None if isinstance(value, bool) else f"{path} must be {hint} (got {value!r})"
    if kind == "str":
        return None if isinstance(value, str) and value.strip() else \
            f"{path} must be {hint} (got {value!r})"
    if kind == "id":
        return None if isinstance(value, (str, int)) and not isinstance(value, bool) else \
            f"{path} must be {hint} (got {value!r})"
    if kind == "hours":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return f"{path} must be {hint} (got {value!r})"
        for hour in value:
            if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
                return f"{path} must be {hint} (got {value!r})"
        return None
    if kind == "ids":
        if not isinstance(value, (list, tuple)):
            return f"{path} must be {hint} (got {value!r})"
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                return f"{path} must be {hint} (got {value!r})"
        return None
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{path} must be {hint} (got {value!r})"
    elif kind == "num":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"{path} must be {hint} (got {value!r})"
    else:  # pragma: no cover - a rule added without a branch is a bug in this table
        return f"{path}: unknown rule {kind}"
    if low is not None and value < low:
        return f"{path} must be at least {low} (got {value!r})"
    if high is not None and value > high:
        return f"{path} must be at most {high} (got {value!r})"
    return None


def type_problems(cfg: dict) -> list[str]:
    problems = []
    # with proactive off its fields are placeholders ("", 0) — validate() only checks them once enabled
    proactive_on = bool((cfg.get("proactive") or {}).get("enabled"))
    for path in TYPE_RULES:
        if path.startswith("proactive.") and not proactive_on:
            continue
        value = value_of(cfg, path)
        if value is None:
            continue
        problem = check_type(path, value)
        if problem:
            problems.append(problem)
    return problems



def validate(cfg: dict, require_tokens: bool = True) -> list[str]:
    """Refuse to start on anything that would make the bot answer strangers or fail silently.

    `require_tokens=False` is for the configurator: a token is exported later, but a wrong allowlist or a
    target outside it is wrong the moment it is written.
    """
    problems: list[str] = []
    # shape first: `beat.quiet_hours: 8` would otherwise pass this gate and throw on every beat
    problems.extend(type_problems(cfg))
    platforms = platforms_of(cfg)
    for platform in platforms:
        section = cfg.get(platform)
        if platform not in {"telegram", "discord"}:
            problems.append(f"unknown platform: {platform}")
            continue
        if not isinstance(section, dict):
            problems.append(f"{platform}: config section missing")
            continue
        if require_tokens and not os.environ.get(section.get("token_env", ""), ""):
            problems.append(f"{platform}: environment variable "
                            f"{section.get('token_env') or '(none)'} is unset")
        allow_keys = ("allow_chat_ids",) if platform == "telegram" else ("allow_channel_ids",)
        allow = []
        for key in allow_keys + ("allow_user_ids",):
            values = section.get(key) or []
            if not isinstance(values, (list, tuple)):
                continue  # already reported by type_problems; iterating an int would crash startup
            allow.extend(str(v) for v in values)
        if not allow:
            problems.append(f"{platform}: no allowlist (refusing to answer strangers)")
    proactive = cfg.get("proactive") or {}
    if proactive.get("enabled"):
        platform = proactive.get("platform") or ""
        if platform not in {"telegram", "discord"}:
            problems.append("proactive: platform must be telegram or discord")
        elif platform not in platforms:
            problems.append(f"proactive: platform {platform} is not in the platform list")
        target = str(proactive.get("target") or "")
        if not target:
            problems.append("proactive: target chat/channel id is empty")
        else:
            # only the chat/channel list counts here: a user id is not a place a beat may speak
            allow_keys = ("allow_chat_ids",) if platform == "telegram" else ("allow_channel_ids",)
            listed = (cfg.get(platform) or {}).get(allow_keys[0]) or []
            allow = [str(v) for v in listed] if isinstance(listed, (list, tuple)) else []
            if target not in allow:
                # an unattended beat must not speak somewhere the allowlist would never have accepted
                problems.append(f"proactive: target {target} is not in the {platform} allowlist")
        interval = proactive.get("interval_seconds") or 0
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            problems.append("proactive: interval_seconds is not an integer")
        else:
            if interval < 0 or 0 < interval < 30:
                problems.append("proactive: interval_seconds must be 0 (inherit) or at least 30")
    return problems


class Router:
    """Turns an incoming message into one reply. Commands are handled here, conversation goes to fish."""

    def __init__(self, cfg: dict, state: dict, now_fn=None, compose=None, lock=None):
        self.cfg = cfg
        self.state = state
        self.now_fn = now_fn or (lambda: datetime.now().replace(microsecond=0))
        self.compose = compose or fish.compose_reply
        self.lock = lock or threading.Lock()
        self.sent_at: dict[str, list[float]] = {}

    # ---- guards -------------------------------------------------------------------------------
    def allowed(self, platform: str, chat_id, user_id, private: bool = True) -> bool:
        section = self.cfg.get(platform) or {}
        chat_keys = ("allow_chat_ids",) if platform == "telegram" else ("allow_channel_ids",)
        chats, users = [], []
        for key in chat_keys + ("allow_user_ids",):
            values = section.get(key) or []
            if not isinstance(values, (list, tuple)):
                continue
            (users if key == "allow_user_ids" else chats).extend(str(v) for v in values)
        # an empty allowlist is a configuration error (validate() refuses to start), not "allow all"
        if str(chat_id) in chats:
            return True
        # a trusted user is trusted in a private chat, not "anywhere this person happens to speak": with
        # the old or-logic, one id in allow_user_ids answered them in every group they joined
        return bool(private) and str(user_id) in users

    def rate_limited(self, chat_id) -> bool:
        limit = int(self.cfg["limits"].get("max_messages_per_minute", 6))
        now = time.monotonic()
        history = [t for t in self.sent_at.get(str(chat_id), []) if now - t < 60]
        if len(history) >= limit:
            self.sent_at[str(chat_id)] = history
            return True
        history.append(now)
        self.sent_at[str(chat_id)] = history
        return False

    # ---- commands -----------------------------------------------------------------------------
    def _status_line(self) -> str:
        memory_root = Path(self.cfg["memory_root"])
        entries = fish.read_index(memory_root)
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        mood = fish.mood_now(self.state, self.now_fn())
        peak = fish.peak_memory(entries, float(self.cfg["beat"].get("min_weight", 0.5)))
        tail = f", strongest is {peak['id']}" if peak else ""
        return (f"memory {len(entries)} entries ({counts.get('identity', 0)} identity), "
                f"mood {mood:.2f}{tail}")

    def _verify(self, ids: list[str]) -> str:
        script = self.cfg.get("cycle_script")
        if not script or not Path(script).exists():
            return "I cannot verify anything: the memory cycle script is not configured."
        command = [sys.executable, str(script), "verify", "--root", str(self.cfg["memory_root"]),
                   "--ids", *ids, "--today", self.now_fn().date().isoformat()]
        result = subprocess_run(command)
        if result["code"] != 0:
            return f"not confirmed: {result['note'] or 'unknown error'}"
        self.state["mood"] = min(1.0, fish.mood_now(self.state, self.now_fn()) + fish.MOOD_VERIFY_RISE)
        self.state["mood_at"] = self.now_fn().isoformat()
        fish.save_state(Path(self.cfg["state_file"]), self.state)
        return f"confirmed: {', '.join(ids)} (mood +{fish.MOOD_VERIFY_RISE})"

    def handle(self, platform: str, chat_id, user_id, text: str, private: bool = True) -> str | None:
        body = (text or "").strip()
        if not body or not self.allowed(platform, chat_id, user_id, private):
            return None
        with self.lock:
            # two platform threads share this counter table; a read-modify-write outside the lock can
            # undercount and let the burst through
            limited = self.rate_limited(chat_id)
        if limited:
            print(f"[bot] rate limit hit for {platform} chat {chat_id}; message dropped",
                  file=sys.stderr)
            return None
        limit = int(self.cfg["limits"].get("max_message_chars", 800))
        body = body[:limit]
        command, _, rest = body.partition(" ")
        # Telegram appends the bot name in groups (`/status@fish_bot`); no other platform does
        lowered = command.split("@", 1)[0].lower()
        if lowered in {"/start", "/help"}:
            return HELP
        if lowered == "/status":
            return self._status_line()
        if lowered == "/verify":
            ids = [part for part in rest.split() if part]
            if not ids:
                return "usage: /verify m-YYYYMMDD-NN"
            with self.lock:
                return self._verify(ids)
        if body.startswith("/"):
            return "unknown command — try /help"
        # one turn at a time: a heartbeat thread shares this state and may be mid-tick
        # quoting is on by default, including in groups — one switch (`reply.include_memory`) turns it off
        # everywhere, and the log line says which mode this turn used
        quote = bool((self.cfg.get("reply") or {}).get("include_memory", True))
        with self.lock:
            turn = self.compose(self.cfg, self.state, self.now_fn(), body,
                                record=bool(self.cfg.get("record_episodes") != "off"),
                                include_memory=quote)
        where = "" if private else " in a group"
        print(f"[bot] {platform} turn: recalled {', '.join(turn['hits']) or 'nothing'}, "
              f"episode {turn['episode'] or '-'}, memory quoting "
              f"{'on' if quote else 'off'}{where}", file=sys.stderr)
        return turn["reply"]


def subprocess_run(command: list[str], timeout: int = 60) -> dict:
    import subprocess
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return {"code": 1, "note": str(exc)}
    note = (result.stderr or result.stdout).strip().splitlines()
    return {"code": result.returncode, "note": note[0] if note else ""}


def proactive_sender(cfg: dict):
    """A callable that delivers one proactive line to the configured chat, or None when unconfigured."""
    section = cfg.get("proactive") or {}
    platform = section.get("platform")
    target = section.get("target")
    if platform == "telegram":
        token = os.environ.get(cfg["telegram"].get("token_env", "FISH_TG_TOKEN"), "")
        if not token or not target:
            return None
        return lambda text: adapters.send_telegram(token, target, text)
    if platform == "discord":
        token = os.environ.get(cfg["discord"].get("token_env", "FISH_DISCORD_TOKEN"), "")
        if not token or not target:
            return None
        return lambda text: adapters.send_discord(token, target, text)
    return None


class BeatLoop:
    """The heartbeat from `fish.py run`, wired to a chat: decide → maybe speak → cycle → persist.

    Everything that decides *whether* it may speak (quiet hours, the 20-minute gap, the daily cap, idle
    time, whether any memory is strong enough) stays in `fish.decide()`, so a bot beat and a CLI beat
    cannot disagree. This class only supplies the transport and the loop.
    """

    def __init__(self, cfg: dict, state: dict, send, now_fn=None, lock=None,
                 stop: threading.Event | None = None, interval: int | None = None):
        section = cfg.get("proactive") or {}
        # a beat may land in a group, so only a target the user explicitly trusts gets memory lines
        self.cfg = {**cfg, "beat": {**cfg["beat"],
                                    "include_memory": bool(section.get("include_memory", False))}}
        self.state = state
        self.send = send
        self.now_fn = now_fn or (lambda: datetime.now().replace(microsecond=0))
        self.lock = lock or threading.Lock()
        self.stop = stop or threading.Event()
        raw = interval or int(cfg["beat"].get("interval_seconds", 900))
        # a zero/negative interval would spin: never beat faster than every 30 seconds
        self.interval = max(30, int(raw))

    def beat_once(self) -> bool:
        delivered = []

        def deliver(text: str) -> dict:
            outcome = self.send(text)
            if outcome.get("ok"):
                delivered.append(text)
            return outcome

        with self.lock:
            fish.cmd_tick(self.cfg, self.state, self.now_fn(), send=deliver)
        return bool(delivered)

    def run(self) -> int:
        section = self.cfg.get("proactive") or {}
        print(f"[beat] proactive every {self.interval}s to {section.get('platform')} "
              f"{section.get('target')}", file=sys.stderr)
        while not self.stop.is_set():
            try:
                self.beat_once()
            except Exception as exc:
                # a failed beat (network, a broken memory file) must not end the loop
                print(f"[beat] tick failed: {exc}", file=sys.stderr)
            self.stop.wait(self.interval)
        return 0


def backoff(delay: float) -> float:
    """Next wait after a failed poll: 2s, 4s, 8s … capped. A bad token must not become a 1 Hz request loop."""
    return min(30.0, max(2.0, delay * 2))


class TelegramBot:
    """Long polling. No webhook, so it works behind NAT and needs no public address."""

    def __init__(self, cfg: dict, router: Router, http=http_json, send=None):
        self.cfg = cfg
        self.router = router
        self.http = http
        self.send = send or adapters.send_telegram
        self.token = os.environ.get(cfg["telegram"].get("token_env", "FISH_TG_TOKEN"), "")
        self.state = router.state
        self.poll_ok = True

    def poll_once(self) -> int:
        try:
            offset = int(self.state.get("telegram_offset") or 0)
        except (TypeError, ValueError):
            # a hand-edited state file must not wedge polling in a retry loop
            print("warning: telegram_offset is not a number; polling from 0", file=sys.stderr)
            offset = 0
        params = {"timeout": 20, "allowed_updates": json.dumps(["message"])}
        if offset:
            params["offset"] = offset
        data = self.http(f"{TELEGRAM_API}/bot{self.token}/getUpdates", params=params)
        if not data.get("ok"):
            print(f"[telegram] poll failed: {data.get('error') or data.get('description')}",
                  file=sys.stderr)
            self.poll_ok = False
            return 0
        self.poll_ok = True
        handled = 0
        for update in data.get("result") or []:
            self.state["telegram_offset"] = int(update.get("update_id", 0)) + 1
            if self.dispatch(update):
                handled += 1
        if data.get("result"):
            with self.router.lock:
                # the beat thread may be writing the same state; keep the two snapshots serialised
                fish.save_state(Path(self.cfg["state_file"]), self.state)
        return handled

    def dispatch(self, update: dict) -> bool:
        message = update.get("message")
        if not message:
            # edited_message, channel_post and friends: an edit is not a new turn
            return False
        if message.get("from", {}).get("is_bot"):
            return False
        text = message.get("text")
        if not text:
            return False
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        # "private" is what separates a DM from a group: the same id set means different reach in each
        private = (message.get("chat") or {}).get("type") == "private"
        reply = self.router.handle("telegram", chat_id, user_id, text, private=private)
        if not reply:
            return False
        for chunk in adapters.split_message(reply, adapters.TELEGRAM_LIMIT):
            outcome = self.send(self.token, chat_id, chunk)
            if not outcome.get("ok"):
                print(f"[telegram] send failed: {outcome.get('error')}", file=sys.stderr)
                break
        return True

    def run(self) -> int:
        print("[telegram] polling as configured", file=sys.stderr)
        delay = 1.0
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                # a single bad poll (DNS, 502, a rotated token) must not kill the process
                print(f"[telegram] poll error: {exc}", file=sys.stderr)
                self.poll_ok = False
            # a rejected token or two processes racing getUpdates must not become a request loop
            delay = 1.0 if self.poll_ok else backoff(delay)
            time.sleep(delay)


class DiscordBot:
    """Gateway client: identify, heartbeat, and answer MESSAGE_CREATE in allowlisted channels."""

    def __init__(self, cfg: dict, router: Router, http=http_json, socket_factory=None, send=None):
        self.cfg = cfg
        self.router = router
        self.http = http
        self.socket_factory = socket_factory or ws.WebSocket
        self.send = send or adapters.send_discord
        self.token = os.environ.get(cfg["discord"].get("token_env", "FISH_DISCORD_TOKEN"), "")
        self.interval = 41.0
        self.seq: int | None = None
        self.stopping = threading.Event()

    def gateway_url(self) -> str:
        data = self.http(GATEWAY_URL, headers={"Authorization": f"Bot {self.token}"})
        url = data.get("url")
        if not url:
            raise RuntimeError(f"could not read the gateway url: {data.get('error') or data}")
        return url + "?v=10&encoding=json"

    def identify(self) -> dict:
        return {"op": 2, "d": {
            "token": self.token,
            "intents": INTENTS,
            "properties": {"os": "linux", "browser": "fish-runtime", "device": "fish-runtime"},
        }}

    def handle_payload(self, payload: dict, conn=None) -> str | None:
        """One gateway frame. Returns the reply text when a message was answered (for tests/one-shot)."""
        op = payload.get("op")
        if payload.get("s") is not None:
            self.seq = payload["s"]
        if op == 10:
            self.interval = float(payload["d"]["heartbeat_interval"]) / 1000.0
            if conn:
                conn.send(json.dumps(self.identify()))
                threading.Thread(target=self._heartbeat, args=(conn,), daemon=True).start()
            return None
        if op == 11:
            return None
        if op == 7 or op == 9:
            raise RuntimeError("gateway asked for a reconnect")
        if payload.get("t") != "MESSAGE_CREATE":
            return None
        return self._on_message(payload.get("d") or {})

    def _on_message(self, message: dict) -> str | None:
        author = message.get("author") or {}
        if author.get("bot"):
            return None
        content = (message.get("content") or "").strip()
        if not content:
            return None
        channel_id = message.get("channel_id")
        private = not message.get("guild_id")
        # In a guild, only answer when addressed: "@bot ..." or a leading command.
        if not private and not content.startswith("<@") and not content.startswith("/"):
            return None
        if content.startswith("<@"):
            content = content.split(">", 1)[-1].strip()
        reply = self.router.handle("discord", channel_id, author.get("id"), content, private=private)
        if not reply:
            return None
        for chunk in adapters.split_message(reply, adapters.DISCORD_LIMIT):
            outcome = self.send(self.token, channel_id, chunk)
            if not outcome.get("ok"):
                print(f"[discord] send failed: {outcome.get('error')}", file=sys.stderr)
                break
        return reply

    def _heartbeat(self, conn) -> None:
        while not self.stopping.is_set():
            time.sleep(self.interval)
            try:
                conn.send(json.dumps({"op": 1, "d": self.seq}))
            except Exception:
                return

    def run(self) -> int:
        backoff = 3
        while not self.stopping.is_set():
            try:
                conn = self.socket_factory(self.gateway_url(), timeout=60).connect()
                print("[discord] gateway connected", file=sys.stderr)
                try:
                    while not self.stopping.is_set():
                        raw = conn.recv()
                        if raw is None:
                            break
                        self.handle_payload(json.loads(raw), conn)
                finally:
                    # op 7/9 raise to force a reconnect; the socket still has to be closed here
                    conn.close()
            except KeyboardInterrupt:
                self.stopping.set()
                break
            except Exception as exc:
                print(f"[discord] reconnecting after: {exc}", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 3
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the character on Telegram and/or Discord")
    parser.add_argument("--config", default="config.bot.json")
    parser.add_argument("--platform", default="", help="overrides config.platform (telegram|discord|both)")
    parser.add_argument("--once", action="store_true", help="one poll cycle, then exit")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    cfg = load_config(config_path)
    cfg = fish.resolve_paths(cfg, config_path.resolve().parent)
    platforms = cfg.get("platform") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    if args.platform:
        platforms = ["telegram", "discord"] if args.platform == "both" else [args.platform]
    cfg["platform"] = platforms

    problems = validate(cfg)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print("error: refusing to start (fix the items above; tokens go in the environment)",
              file=sys.stderr)
        print("hint: `python setup.py show` explains the current settings, `python setup.py set ...` edits them",
              file=sys.stderr)
        return 2

    state = fish.load_state(Path(cfg["state_file"]))
    stop = threading.Event()
    lock = threading.Lock()  # a turn and a beat both rewrite state; one at a time
    router = Router(cfg, state, lock=lock)

    try:
        if args.once:
            if "telegram" in platforms:
                TelegramBot(cfg, router).poll_once()
            if "discord" in platforms:
                print("note: --once only runs one Telegram poll; Discord needs the gateway loop, "
                      "so nothing was pulled for it", file=sys.stderr)
            return 0
        threads: list[threading.Thread] = []
        if "telegram" in platforms:
            threads.append(threading.Thread(target=TelegramBot(cfg, router).run, daemon=True))
        if "discord" in platforms:
            threads.append(threading.Thread(target=DiscordBot(cfg, router).run, daemon=True))
        if (cfg.get("proactive") or {}).get("enabled"):
            sender = proactive_sender(cfg)
            if sender:
                threads.append(threading.Thread(
                    target=BeatLoop(cfg, state, sender, lock=lock, stop=stop).run, daemon=True))
            else:
                print("warning: proactive is enabled but no sender could be built; beat not started",
                      file=sys.stderr)
        for thread in threads:
            thread.start()
        print("[bot] running; Ctrl-C stops", file=sys.stderr)
        while any(thread.is_alive() for thread in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        fish.save_state(Path(cfg["state_file"]), state)
        print("[bot] stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
