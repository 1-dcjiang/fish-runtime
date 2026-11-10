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
    "model": {"mode": "offline"},
    "channel": {"type": "stdout"},
    "beat": {"interval_seconds": 900, "quiet_hours": [23, 7], "min_gap_minutes": 20,
             "nudge_after_hours": 6, "nudge_cooldown_hours": 12,
             "max_messages_per_day": 3, "min_weight": 0.5},
    "telegram": {"token_env": "FISH_TG_TOKEN", "allow_chat_ids": [], "allow_user_ids": []},
    "discord": {"token_env": "FISH_DISCORD_TOKEN", "allow_channel_ids": [], "allow_user_ids": []},
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
    return fish.merge(fish.merge(fish.DEFAULTS, BOT_DEFAULTS), raw)


def validate(cfg: dict) -> list[str]:
    """Refuse to start on anything that would make the bot answer strangers or fail silently."""
    problems: list[str] = []
    platforms = cfg.get("platform") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    for platform in platforms:
        section = cfg.get(platform)
        if platform not in {"telegram", "discord"}:
            problems.append(f"unknown platform: {platform}")
            continue
        if not isinstance(section, dict):
            problems.append(f"{platform}: config section missing")
            continue
        if not os.environ.get(section.get("token_env", ""), ""):
            problems.append(f"{platform}: environment variable "
                            f"{section.get('token_env') or '(none)'} is unset")
        allow_keys = ("allow_chat_ids",) if platform == "telegram" else ("allow_channel_ids",)
        allow = []
        for key in allow_keys + ("allow_user_ids",):
            allow.extend(section.get(key) or [])
        if not allow:
            problems.append(f"{platform}: no allowlist (refusing to answer strangers)")
    return problems


class Router:
    """Turns an incoming message into one reply. Commands are handled here, conversation goes to fish."""

    def __init__(self, cfg: dict, state: dict, now_fn=None, compose=None):
        self.cfg = cfg
        self.state = state
        self.now_fn = now_fn or (lambda: datetime.now().replace(microsecond=0))
        self.compose = compose or fish.compose_reply
        self.sent_at: dict[str, list[float]] = {}

    # ---- guards -------------------------------------------------------------------------------
    def allowed(self, platform: str, chat_id, user_id) -> bool:
        section = self.cfg.get(platform) or {}
        allow = []
        for key in ("allow_chat_ids", "allow_channel_ids", "allow_user_ids"):
            allow.extend(str(v) for v in (section.get(key) or []))
        # an empty allowlist is a configuration error (validate() refuses to start), not "allow all"
        return bool(allow) and (str(chat_id) in allow or str(user_id) in allow)

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

    def handle(self, platform: str, chat_id, user_id, text: str) -> str | None:
        body = (text or "").strip()
        if not body or not self.allowed(platform, chat_id, user_id):
            return None
        if self.rate_limited(chat_id):
            print(f"[bot] rate limit hit for {platform} chat {chat_id}; message dropped",
                  file=sys.stderr)
            return None
        limit = int(self.cfg["limits"].get("max_message_chars", 800))
        body = body[:limit]
        command, _, rest = body.partition(" ")
        lowered = command.lower()
        if lowered in {"/start", "/help"}:
            return HELP
        if lowered == "/status":
            return self._status_line()
        if lowered == "/verify":
            ids = [part for part in rest.split() if part]
            return self._verify(ids) if ids else "usage: /verify m-YYYYMMDD-NN"
        if body.startswith("/"):
            return "unknown command — try /help"
        turn = self.compose(self.cfg, self.state, self.now_fn(), body,
                            record=bool(self.cfg.get("record_episodes") != "off"))
        print(f"[bot] {platform} turn: recalled {', '.join(turn['hits']) or 'nothing'}, "
              f"episode {turn['episode'] or '-'}", file=sys.stderr)
        return turn["reply"]


def subprocess_run(command: list[str], timeout: int = 60) -> dict:
    import subprocess
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return {"code": 1, "note": str(exc)}
    note = (result.stderr or result.stdout).strip().splitlines()
    return {"code": result.returncode, "note": note[0] if note else ""}


class TelegramBot:
    """Long polling. No webhook, so it works behind NAT and needs no public address."""

    def __init__(self, cfg: dict, router: Router, http=http_json, send=None):
        self.cfg = cfg
        self.router = router
        self.http = http
        self.send = send or adapters.send_telegram
        self.token = os.environ.get(cfg["telegram"].get("token_env", "FISH_TG_TOKEN"), "")
        self.state = router.state

    def poll_once(self) -> int:
        offset = int(self.state.get("telegram_offset") or 0)
        params = {"timeout": 20, "allowed_updates": json.dumps(["message"])}
        if offset:
            params["offset"] = offset
        data = self.http(f"{TELEGRAM_API}/bot{self.token}/getUpdates", params=params)
        if not data.get("ok"):
            print(f"[telegram] poll failed: {data.get('error') or data.get('description')}",
                  file=sys.stderr)
            return 0
        handled = 0
        for update in data.get("result") or []:
            self.state["telegram_offset"] = int(update.get("update_id", 0)) + 1
            if self.dispatch(update):
                handled += 1
        if data.get("result"):
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
        reply = self.router.handle("telegram", chat_id, user_id, text)
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
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                # a single bad poll (DNS, 502, a rotated token) must not kill the process
                print(f"[telegram] poll error: {exc}", file=sys.stderr)
                time.sleep(3)
            time.sleep(1)


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
        # In a guild, only answer when addressed: "@bot ..." or a leading command.
        if message.get("guild_id") and not content.startswith("<@") and not content.startswith("/"):
            return None
        if content.startswith("<@"):
            content = content.split(">", 1)[-1].strip()
        reply = self.router.handle("discord", channel_id, author.get("id"), content)
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
                while not self.stopping.is_set():
                    raw = conn.recv()
                    if raw is None:
                        break
                    self.handle_payload(json.loads(raw), conn)
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
        return 2

    state = fish.load_state(Path(cfg["state_file"]))
    router = Router(cfg, state)

    try:
        if args.once:
            if "telegram" in platforms:
                TelegramBot(cfg, router).poll_once()
            return 0
        threads: list[threading.Thread] = []
        if "telegram" in platforms:
            threads.append(threading.Thread(target=TelegramBot(cfg, router).run, daemon=True))
        if "discord" in platforms:
            threads.append(threading.Thread(target=DiscordBot(cfg, router).run, daemon=True))
        for thread in threads:
            thread.start()
        print("[bot] running; Ctrl-C stops", file=sys.stderr)
        while any(thread.is_alive() for thread in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        fish.save_state(Path(cfg["state_file"]), state)
        print("[bot] stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
