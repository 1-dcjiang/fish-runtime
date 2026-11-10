"""Offline tests for the bot layer. No sockets, no tokens, no real clock.

Run from the runtime root:  python -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = RUNTIME_ROOT.parent
SKILL_SCRIPTS = PROJECT_ROOT / "persona-and-memory" / "scripts"

sys.path.insert(0, str(RUNTIME_ROOT))
sys.path.insert(0, str(SKILL_SCRIPTS))

import adapters  # noqa: E402
import bot  # noqa: E402
import fish  # noqa: E402
import ws  # noqa: E402


class FakeHTTP:
    """Records requests and replays canned responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, url, params=None, payload=None, headers=None, timeout=30.0):
        self.calls.append({"url": url, "params": params, "payload": payload})
        return self.responses.pop(0) if self.responses else {"ok": True, "result": []}


class FakeSocket:
    def __init__(self, url, timeout=40):
        self.url = url
        self.sent = []
        self.incoming = []

    def connect(self):
        return self

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self):
        return self.incoming.pop(0) if self.incoming else None

    def close(self):
        pass


class BotFixture(unittest.TestCase):
    now = "2026-11-10T10:00"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.memory = self.dir / "memory"
        self.memory.mkdir()
        (self.memory / "entries").mkdir()
        self.persona = self.dir / "persona.json"
        self.persona.write_text(json.dumps({"name": "Tide", "self": "我", "user": "你",
                                            "language": "Chinese", "register": "casual",
                                            "emoji_policy": "none", "flourish": [],
                                            "refusal_style": "直接说不"}, ensure_ascii=False),
                                encoding="utf-8")
        self.state_file = self.dir / "bot-state.json"
        os.environ["FISH_TG_TOKEN"] = "tg-test"
        os.environ["FISH_DISCORD_TOKEN"] = "dc-test"
        self.cfg = fish.merge(fish.merge(fish.DEFAULTS, bot.BOT_DEFAULTS), {
            "persona": str(self.persona),
            "memory_root": str(self.memory),
            "state_file": str(self.state_file),
            "cycle_script": str(SKILL_SCRIPTS / "memory_cycle.py"),
            "platform": ["telegram", "discord"],
            "telegram": {"allow_chat_ids": ["111"], "allow_user_ids": []},
            "discord": {"allow_channel_ids": ["222"], "allow_user_ids": []},
            "model": {"mode": "offline"},
        })
        self.state = {"mood": 0.0, "mood_at": None, "last_user_at": None,
                      "last_nudge_at": None, "sent_today": 0, "sent_day": None}

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("FISH_TG_TOKEN", None)
        os.environ.pop("FISH_DISCORD_TOKEN", None)

    def write_entry(self, entry_id="m-20261109-01", weight=0.7, body="User prefers Chinese replies.\n"):
        path = self.memory / "entries" / f"{entry_id}.md"
        path.write_text("---\n"
                        f"id: {entry_id}\nkind: preference\nweight: {weight:.2f}\nhits: 0\n"
                        f"verified: 0\nstatus: active\ndecay: 0.92\ncreated: 2026-11-09\n"
                        f"last_seen: 2026-11-09\n---\n\n{body}", encoding="utf-8")
        return path

    def build_index(self):
        import memory_cycle as mc
        self.assertEqual(mc.main(["index", "--root", str(self.memory), "--today", "2026-11-10"]), 0)


class ValidateTests(BotFixture):
    def test_no_allowlist_and_missing_token_are_reported(self):
        os.environ.pop("FISH_TG_TOKEN", None)
        self.cfg["telegram"] = {"token_env": "FISH_TG_TOKEN", "allow_chat_ids": []}
        problems = bot.validate(self.cfg)
        joined = " | ".join(problems)
        self.assertIn("FISH_TG_TOKEN is unset", joined)
        self.assertIn("no allowlist", joined)

    def test_a_complete_config_has_no_problems(self):
        self.assertEqual(bot.validate(self.cfg), [])

    def test_unknown_platform_is_rejected(self):
        self.cfg["platform"] = ["irc"]
        self.assertIn("unknown platform: irc", " | ".join(bot.validate(self.cfg)))


class RouterTests(BotFixture):
    def router(self, compose=None):
        return bot.Router(self.cfg, self.state,
                          now_fn=lambda: fish.parse_now(self.now), compose=compose)

    def test_strangers_are_ignored_even_with_a_reply_ready(self):
        called = []
        router = self.router(compose=lambda *a, **k: called.append(1) or {"reply": "hi", "hits": [], "episode": None})
        self.assertIsNone(router.handle("telegram", "999", "999", "hello"))
        self.assertEqual(called, [])

    def test_allowlisted_user_gets_a_reply(self):
        router = self.router(compose=lambda *a, **k: {"reply": "在", "hits": ["m-1"], "episode": None})
        self.assertEqual(router.handle("telegram", "111", "5", "在吗"), "在")

    def test_help_does_not_touch_memory(self):
        called = []
        router = self.router(compose=lambda *a, **k: called.append(1))
        self.assertIn("/status", router.handle("telegram", "111", "5", "/help"))
        self.assertEqual(called, [])

    def test_status_reports_counts(self):
        self.write_entry()
        self.build_index()
        router = self.router()
        line = router.handle("telegram", "111", "5", "/status")
        self.assertIn("1 entries", line)
        self.assertIn("mood", line)

    def test_unknown_command_is_answered_not_silent(self):
        router = self.router(compose=lambda *a, **k: {"reply": "x", "hits": [], "episode": None})
        self.assertIn("unknown command", router.handle("telegram", "111", "5", "/frobnicate"))

    def test_rate_limit_drops_the_seventh_message(self):
        router = self.router(compose=lambda *a, **k: {"reply": "ok", "hits": [], "episode": None})
        replies = [router.handle("telegram", "111", "5", f"m{i}") for i in range(8)]
        self.assertEqual(sum(1 for r in replies if r), 6)
        self.assertIsNone(replies[-1])

    def test_long_input_is_truncated_before_memory(self):
        seen = {}

        def compose(cfg, state, now, text, record=True):
            seen["text"] = text
            return {"reply": "ok", "hits": [], "episode": None}

        router = self.router(compose=compose)
        router.cfg["limits"]["max_message_chars"] = 10
        router.handle("telegram", "111", "5", "x" * 50)
        self.assertEqual(len(seen["text"]), 10)


class TelegramTests(BotFixture):
    def make_bot(self, responses, router=None):
        http = FakeHTTP(responses)
        router = router or bot.Router(self.cfg, self.state,
                                      now_fn=lambda: fish.parse_now(self.now))
        sent = []
        bot_ = bot.TelegramBot(self.cfg, router, http=http,
                               send=lambda token, chat, text: sent.append((chat, text)) or {"ok": True})
        return bot_, http, sent

    def update(self, text="hello", chat="111", user="5", bot_flag=False):
        return {"update_id": 77, "message": {"text": text,
                                             "chat": {"id": chat},
                                             "from": {"id": user, "is_bot": bot_flag}}}

    def test_a_text_message_is_answered_and_the_offset_advances(self):
        b, _http, sent = self.make_bot([{"ok": True, "result": [self.update()]}])
        self.assertEqual(b.poll_once(), 1)
        self.assertEqual(sent[0][0], "111")
        self.assertEqual(self.state["telegram_offset"], 78)
        self.assertTrue(self.state_file.exists())

    def test_messages_from_bots_are_ignored(self):
        b, _http, sent = self.make_bot([{"ok": True, "result": [self.update(bot_flag=True)]}])
        self.assertEqual(b.poll_once(), 0)
        self.assertEqual(sent, [])

    def test_edited_messages_are_not_double_answered(self):
        b, _http, sent = self.make_bot([{"ok": True, "result": [
            {"update_id": 90, "edited_message": {"text": "edited", "chat": {"id": "111"},
                                                 "from": {"id": "5"}}}]}])
        self.assertEqual(b.poll_once(), 0)
        self.assertEqual(sent, [])

    def test_poll_failure_is_logged_not_raised(self):
        b, _http, sent = self.make_bot([{"ok": False, "error": "HTTP 401"}])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(b.poll_once(), 0)
        self.assertEqual(sent, [])

    def test_a_long_reply_is_split_below_the_limit(self):
        router = bot.Router(self.cfg, self.state, now_fn=lambda: fish.parse_now(self.now),
                            compose=lambda *a, **k: {"reply": "字" * 5000, "hits": [], "episode": None})
        b, _http, sent = self.make_bot([{"ok": True, "result": [self.update("hi")]}], router=router)
        b.poll_once()
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(text) <= adapters.TELEGRAM_LIMIT for _chat, text in sent))


class DiscordTests(BotFixture):
    def make_bot(self, responses=None):
        http = FakeHTTP(responses)
        sent = []
        router = bot.Router(self.cfg, self.state, now_fn=lambda: fish.parse_now(self.now),
                            compose=lambda *a, **k: {"reply": "在", "hits": [], "episode": None})
        b = bot.DiscordBot(self.cfg, router, http=http,
                           send=lambda token, channel, text: sent.append((channel, text)) or {"ok": True})
        return b, http, sent

    def message(self, content="hello", channel="222", user="9", guild="333", is_bot=False):
        return {"t": "MESSAGE_CREATE", "op": 0, "s": 3, "d": {
            "content": content, "channel_id": channel, "guild_id": guild,
            "author": {"id": user, "bot": is_bot}}}

    def test_a_dm_is_answered(self):
        b, _http, sent = self.make_bot()
        b.handle_payload(self.message(content="在吗", channel="222", guild=None))
        self.assertEqual(sent[0][0], "222")

    def test_a_guild_message_needs_a_mention(self):
        b, _http, sent = self.make_bot()
        b.handle_payload(self.message(content="hello", guild="333"))
        self.assertEqual(sent, [])
        b.handle_payload(self.message(content="<@42> hello", guild="333"))
        self.assertEqual(len(sent), 1)

    def test_bot_authors_are_ignored(self):
        b, _http, sent = self.make_bot()
        b.handle_payload(self.message(content="<@42> hi", is_bot=True))
        self.assertEqual(sent, [])

    def test_channel_outside_the_allowlist_is_ignored(self):
        b, _http, sent = self.make_bot()
        b.handle_payload(self.message(content="<@42> hi", channel="999", guild="333"))
        self.assertEqual(sent, [])

    def test_hello_starts_identifying_with_the_right_intents(self):
        b, _http, sent = self.make_bot()
        conn = FakeSocket("wss://x")
        b.handle_payload({"op": 10, "d": {"heartbeat_interval": 41250}}, conn)
        identify = conn.sent[0]
        self.assertEqual(identify["op"], 2)
        self.assertEqual(identify["d"]["intents"], bot.INTENTS)
        self.assertTrue(bot.INTENTS & (1 << 15), "MESSAGE_CONTENT must be requested")

    def test_gateway_url_is_fetched_with_the_bot_token(self):
        b, http, _sent = self.make_bot([{"url": "wss://gateway.discord.gg"}])
        self.assertEqual(b.gateway_url(), "wss://gateway.discord.gg?v=10&encoding=json")
        self.assertEqual(http.calls[0]["url"], bot.GATEWAY_URL)

    def test_reconnect_request_is_raised_for_the_run_loop(self):
        b, _http, _sent = self.make_bot()
        with self.assertRaises(RuntimeError):
            b.handle_payload({"op": 7, "d": None})


class WebSocketFrameTests(unittest.TestCase):
    def test_roundtrip_short_text(self):
        frame = ws.build_frame("hello".encode())
        fin, opcode, payload, used = ws.parse_frame(frame)
        self.assertTrue(fin)
        self.assertEqual(opcode, ws.OP_TEXT)
        self.assertEqual(payload, b"hello")
        self.assertEqual(used, len(frame))

    def test_roundtrip_medium_and_large_payloads(self):
        for size in (200, 70000):
            frame = ws.build_frame(b"x" * size)
            _fin, _op, payload, used = ws.parse_frame(frame)
            self.assertEqual(len(payload), size)
            self.assertEqual(used, len(frame))

    def test_server_frames_are_unmasked_and_parse(self):
        payload = b"gateway"
        frame = bytes([0x81, len(payload)]) + payload
        fin, opcode, decoded, used = ws.parse_frame(frame)
        self.assertEqual((fin, opcode, decoded, used), (True, ws.OP_TEXT, payload, len(frame)))

    def test_truncated_frame_is_rejected(self):
        with self.assertRaises(ws.WebSocketError):
            ws.parse_frame(ws.build_frame(b"hello")[:4])

    def test_split_message_never_exceeds_the_limit(self):
        chunks = adapters.split_message("line one\n" * 600, 200)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertTrue(all(chunk.strip() for chunk in chunks))
        self.assertGreater(len(chunks), 10)
        self.assertIn("line one", "".join(chunks))


class IntegrationTests(BotFixture):
    def test_a_real_turn_through_telegram_remembers(self):
        self.write_entry(body="User works on Windows and prefers PowerShell.\n")
        self.build_index()
        sent = []
        http = FakeHTTP([{"ok": True, "result": [{
            "update_id": 5, "message": {"text": "记住我用 PowerShell 而不是 bash",
                                        "chat": {"id": "111"}, "from": {"id": "5"}}}]}])
        router = bot.Router(self.cfg, self.state, now_fn=lambda: fish.parse_now(self.now))
        b = bot.TelegramBot(self.cfg, router, http=http,
                            send=lambda token, chat, text: sent.append(text) or {"ok": True})
        with redirect_stdout(io.StringIO()):
            b.poll_once()
        self.assertTrue(sent and sent[0])
        episodes = list((self.memory / "entries").glob("m-20261110-*.md"))
        self.assertEqual(len(episodes), 1)
        index = (self.memory / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn(episodes[0].stem, index)

    def test_verify_command_confirms_a_memory(self):
        self.write_entry(weight=0.8)
        self.build_index()
        router = bot.Router(self.cfg, self.state, now_fn=lambda: fish.parse_now(self.now))
        reply = router.handle("telegram", "111", "5", "/verify m-20261109-01")
        self.assertIn("confirmed", reply)
        import memory_cycle as mc
        self.assertGreaterEqual(mc.parse_entry(self.memory / "entries" / "m-20261109-01.md")["verified"], 1)


class ProactiveTests(BotFixture):
    """The heartbeat wired to a chat: decide() still owns every reason to stay silent."""

    def make_loop(self, sent, ok=True, now=None):
        self.cfg["proactive"] = {"enabled": True, "platform": "telegram", "target": "111",
                                 "interval_seconds": 900}
        sender = lambda text: (sent.append(text), {"ok": ok, "channel": "telegram"})[1]
        return bot.BeatLoop(self.cfg, self.state, sender,
                            now_fn=lambda: fish.parse_now(now or self.now))

    def test_validate_rejects_proactive_without_a_target(self):
        self.cfg["proactive"] = {"enabled": True, "platform": "telegram", "target": ""}
        self.assertIn("proactive: target chat/channel id is empty", " | ".join(bot.validate(self.cfg)))

    def test_validate_rejects_a_target_outside_the_allowlist(self):
        self.cfg["proactive"] = {"enabled": True, "platform": "telegram", "target": "999"}
        self.assertIn("is not in the telegram allowlist", " | ".join(bot.validate(self.cfg)))

    def test_a_configured_target_validates(self):
        self.cfg["proactive"] = {"enabled": True, "platform": "telegram", "target": "111"}
        self.assertEqual(bot.validate(self.cfg), [])

    def test_an_idle_character_speaks_on_its_own(self):
        self.write_entry(weight=0.9)
        self.build_index()
        self.state["last_user_at"] = "2026-11-09T20:00"
        self.state["started_at"] = "2026-11-01T00:00"
        sent = []
        loop = self.make_loop(sent)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(loop.beat_once())
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.state["sent_today"], 1)
        self.assertTrue(self.state["last_nudge_at"].startswith("2026-11-10T10:00"))

    def test_a_recent_user_message_keeps_it_quiet(self):
        self.write_entry(weight=0.9)
        self.build_index()
        self.state["last_user_at"] = "2026-11-10T09:50"
        self.state["started_at"] = "2026-11-01T00:00"
        sent = []
        loop = self.make_loop(sent)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(loop.beat_once())
        self.assertEqual(sent, [])

    def test_the_daily_cap_survives_proactive_beats(self):
        self.write_entry(weight=0.9)
        self.build_index()
        self.state["last_user_at"] = "2026-11-09T20:00"
        self.state["started_at"] = "2026-11-01T00:00"
        self.state["sent_today"] = 3
        self.state["sent_day"] = "2026-11-10"
        sent = []
        with redirect_stdout(io.StringIO()):
            self.make_loop(sent).beat_once()
        self.assertEqual(sent, [])

    def test_a_failed_send_does_not_count_toward_the_cap(self):
        self.write_entry(weight=0.9)
        self.build_index()
        self.state["last_user_at"] = "2026-11-09T20:00"
        self.state["started_at"] = "2026-11-01T00:00"
        sent = []
        with redirect_stdout(io.StringIO()):
            self.assertFalse(self.make_loop(sent, ok=False).beat_once())
        self.assertEqual(self.state["sent_today"], 0)
        self.assertIsNone(self.state["last_nudge_at"])

    def test_no_sender_without_a_token(self):
        os.environ.pop("FISH_TG_TOKEN", None)
        self.cfg["proactive"] = {"enabled": True, "platform": "telegram", "target": "111"}
        self.assertIsNone(bot.proactive_sender(self.cfg))


if __name__ == "__main__":
    unittest.main()
