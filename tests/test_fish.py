"""Offline tests for fish-runtime. No network, no real clock.

Run from the runtime root:  python -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = RUNTIME_ROOT.parent
SKILL_SCRIPTS = PROJECT_ROOT / "persona-and-memory" / "scripts"

sys.path.insert(0, str(RUNTIME_ROOT))
sys.path.insert(0, str(SKILL_SCRIPTS))

import adapters  # noqa: E402
import fish  # noqa: E402
import memory_cycle as mc  # noqa: E402


def write_entry(root: Path, entry_id: str, *, weight=0.45, hits=0, verified=0,
                status="active", kind="preference", last_seen="2026-11-09",
                body="User prefers replies in Chinese and code in English.\n") -> Path:
    entries = root / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    path = entries / f"{entry_id}.md"
    path.write_text(
        "---\n"
        f"id: {entry_id}\n"
        f"kind: {kind}\n"
        f"weight: {weight:.2f}\n"
        f"hits: {hits}\n"
        f"verified: {verified}\n"
        f"status: {status}\n"
        "decay: 0.92\n"
        f"created: {last_seen}\n"
        f"last_seen: {last_seen}\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


class FishTest(unittest.TestCase):
    today = "2026-11-10"
    now = "2026-11-10T10:00"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.memory = self.dir / "memory"
        self.memory.mkdir()
        self.state_file = self.dir / "state.json"
        self.persona = self.dir / "persona.json"
        self.persona.write_text(json.dumps({
            "spec": "persona-voice-card", "spec_version": "1.0", "name": "Tide",
            "self": "我", "user": "你", "language": "Chinese", "register": "casual",
            "emoji_policy": "none", "flourish": ["先看约束。"],
            "refusal_style": "直接说做不到，然后给最近的办法",
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, **overrides) -> Path:
        config = {
            "persona": str(self.persona),
            "memory_root": str(self.memory),
            "state_file": str(self.state_file),
            "cycle_script": str(SKILL_SCRIPTS / "memory_cycle.py"),
            "run_cycle_on_tick": True,
            "model": {"mode": "offline"},
            "channel": {"type": "stdout"},
            "beat": {
                "interval_seconds": 900, "quiet_hours": [23, 7], "min_gap_minutes": 20,
                "nudge_after_hours": 6, "nudge_cooldown_hours": 12,
                "max_messages_per_day": 3, "min_weight": 0.5,
            },
        }
        for key, value in overrides.items():
            if isinstance(value, dict):
                config[key] = {**config.get(key, {}), **value}
            else:
                config[key] = value
        path = self.dir / "config.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return path

    def seed_state(self, **state):
        base = {"mood": 0.0, "mood_at": self.now, "last_user_at": None,
                "last_nudge_at": None, "sent_today": 0, "sent_day": self.today,
                "started_at": "2026-11-01T00:00"}
        base.update(state)
        self.state_file.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

    def build_index(self):
        self.assertEqual(mc.main(["index", "--root", str(self.memory), "--today", self.today]), 0)
        return fish.read_index(self.memory)

    def run_cmd(self, argv: list[str]) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(fish.main(argv), 0)
        return buffer.getvalue()


class DecisionTests(FishTest):
    def test_quiet_hours_wrap(self):
        self.assertTrue(fish.in_quiet_hours(datetime(2026, 11, 10, 2, 0), [23, 7]))
        self.assertFalse(fish.in_quiet_hours(datetime(2026, 11, 10, 12, 0), [23, 7]))
        self.assertTrue(fish.in_quiet_hours(datetime(2026, 11, 10, 23, 30), [23, 7]))

    def test_min_gap_blocks(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        entries = self.build_index()
        state = {"last_user_at": "2026-11-10T09:50", "sent_today": 0, "sent_day": self.today}
        may, reason = fish.decide(fish.merge(fish.DEFAULTS, {}), state, entries,
                                  datetime(2026, 11, 10, 10, 0))
        self.assertFalse(may)
        self.assertIn("recent_user_message", reason)

    def test_cooldown_blocks(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        entries = self.build_index()
        state = {"last_user_at": "2026-11-09T20:00", "last_nudge_at": "2026-11-10T08:00",
                 "sent_today": 0, "sent_day": self.today}
        may, reason = fish.decide(fish.merge(fish.DEFAULTS, {}), state, entries,
                                  datetime(2026, 11, 10, 10, 0))
        self.assertFalse(may)
        self.assertIn("cooldown", reason)

    def test_daily_cap(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        entries = self.build_index()
        state = {"last_user_at": "2026-11-09T18:00", "sent_today": 3, "sent_day": self.today}
        may, reason = fish.decide(fish.merge(fish.DEFAULTS, {}), state, entries,
                                  datetime(2026, 11, 10, 10, 0))
        self.assertFalse(may)
        self.assertIn("daily_cap", reason)

    def test_speaks_when_idle(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        entries = self.build_index()
        state = {"last_user_at": "2026-11-09T20:00", "sent_today": 0, "sent_day": self.today}
        may, reason = fish.decide(fish.merge(fish.DEFAULTS, {}), state, entries,
                                  datetime(2026, 11, 10, 10, 0))
        self.assertTrue(may)
        self.assertIn("idle", reason)

    def test_nothing_worth_saying(self):
        write_entry(self.memory, "m-20261109-01", weight=0.20)
        entries = self.build_index()
        state = {"last_user_at": "2026-11-09T20:00", "sent_today": 0, "sent_day": self.today}
        may, reason = fish.decide(fish.merge(fish.DEFAULTS, {}), state, entries,
                                  datetime(2026, 11, 10, 10, 0))
        self.assertFalse(may)
        self.assertEqual(reason, "nothing_worth_saying")


class MoodTests(FishTest):
    def test_decay_toward_baseline(self):
        state = {"mood": -1.0, "mood_at": "2026-11-09T10:00"}
        value = fish.mood_now(state, datetime(2026, 11, 10, 10, 0))
        self.assertAlmostEqual(value, -0.6, places=2)

    def test_full_decay_to_zero(self):
        state = {"mood": 0.8, "mood_at": "2026-11-01T10:00"}
        self.assertAlmostEqual(fish.mood_now(state, datetime(2026, 11, 10, 10, 0)), 0.0, places=2)

    def test_fold_lowers_mood_through_tick(self):
        write_entry(self.memory, "m-20260920-01", weight=0.22, last_seen="2026-09-20")
        self.config()
        self.seed_state()
        self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertLess(state["mood"], 0)
        meta = mc.parse_entry(self.memory / "entries" / "m-20260920-01.md")
        self.assertEqual(meta["status"], "fuzzy")


class RecallTests(FishTest):
    def test_recall_prefers_relevance_then_weight(self):
        write_entry(self.memory, "m-20261109-01", weight=0.55,
                    body="Works on Windows with PowerShell 7, python installed.\n", kind="fact")
        write_entry(self.memory, "m-20261109-02", weight=0.30,
                    body="Prefers short replies before 10am.\n")
        entries = self.build_index()
        hits = fish.recall(entries, "powershell on windows")
        self.assertEqual(hits[0]["id"], "m-20261109-01")

    def test_recall_without_text_falls_back_to_heaviest(self):
        write_entry(self.memory, "m-20261109-01", weight=0.30, body="A.\n")
        write_entry(self.memory, "m-20261109-02", weight=0.90, body="B.\n")
        entries = self.build_index()
        self.assertEqual(fish.recall(entries, "")[0]["id"], "m-20261109-02")


class TickTests(FishTest):
    def test_tick_speaks_and_persists(self):
        write_entry(self.memory, "m-20261109-01", weight=0.90, kind="commitment",
                    body="Keep the memory index under 40 lines.\n")
        self.config()
        self.seed_state(last_user_at="2026-11-09T20:00")
        out = self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertIn("[tick] spoke", out)
        self.assertIn("40 lines", out)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["sent_today"], 1)
        self.assertEqual(datetime.fromisoformat(state["last_nudge_at"]),
                         datetime(2026, 11, 10, 10, 0))

    def test_tick_silent_when_recent_message(self):
        write_entry(self.memory, "m-20261109-01", weight=0.90)
        self.config()
        self.seed_state(last_user_at="2026-11-10T09:55")
        out = self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertIn("silent", out)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["sent_today"], 0)

    def test_failed_send_is_not_counted(self):
        write_entry(self.memory, "m-20261109-01", weight=0.90)
        self.config(channel={"type": "telegram"})
        self.seed_state(last_user_at="2026-11-09T20:00")
        os.environ.pop("FISH_TG_TOKEN", None)
        os.environ.pop("FISH_TG_CHAT", None)
        out = self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertIn("send failed", out)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["sent_today"], 0)
        self.assertIsNone(state["last_nudge_at"])

    def test_missing_config_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            fish.main(["status", "--config", str(self.dir / "nope.json")])
        self.assertEqual(ctx.exception.code, 2)


class SayTests(FishTest):
    def test_say_records_episode_in_skill_format(self):
        write_entry(self.memory, "m-20261109-01", weight=0.55,
                    body="Works on Windows with PowerShell 7.\n", kind="fact")
        self.build_index()
        self.config()
        self.seed_state()
        out = self.run_cmd(["say", "--config", str(self.dir / "config.json"),
                            "--now", self.now, "--text", "今天在 PowerShell 里调通了那个脚本"])
        self.assertIn("[say] recorded", out)
        new_file = sorted((self.memory / "entries").glob("m-20261110-*.md"))[0]
        meta = mc.parse_entry(new_file)
        self.assertEqual(meta["kind"], "episode")
        self.assertEqual(meta["status"], "active")
        self.assertAlmostEqual(meta["weight"], 0.45, places=2)

    def test_say_touches_recalled_memory(self):
        write_entry(self.memory, "m-20261109-01", weight=0.55, hits=1, verified=1,
                    body="Works on Windows with PowerShell 7.\n", kind="fact")
        self.build_index()
        self.config()
        self.seed_state()
        self.run_cmd(["say", "--config", str(self.dir / "config.json"),
                      "--now", self.now, "--text", "PowerShell 又炸了，Windows 上真麻烦"])
        meta = mc.parse_entry(self.memory / "entries" / "m-20261109-01.md")
        self.assertEqual(meta["hits"], 2)
        self.assertGreater(meta["weight"], 0.55)

    def test_say_raises_mood(self):
        write_entry(self.memory, "m-20261109-01", weight=0.55)
        self.build_index()
        self.config()
        self.seed_state(mood=-0.5)
        self.run_cmd(["say", "--config", str(self.dir / "config.json"),
                      "--now", self.now, "--text", "在吗"])
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertAlmostEqual(state["mood"], -0.35, places=2)


class AdapterTests(FishTest):
    def test_offline_voice_uses_persona(self):
        persona = json.loads(self.persona.read_text(encoding="utf-8"))
        text = adapters.offline_reply(persona, "记住那条约束", mood=0.8, proactive=True)
        self.assertIn("你", text)
        self.assertIn("记住那条约束", text)
        self.assertIn("先看约束。", text)

    def test_mood_buckets(self):
        self.assertEqual(adapters.mood_bucket(0.9), "bright")
        self.assertEqual(adapters.mood_bucket(0.0), "steady")
        self.assertEqual(adapters.mood_bucket(-0.9), "low")

    def test_unknown_channel_reports_error(self):
        self.config(channel={"type": "carrier-pigeon"})
        cfg = fish.load_config(self.dir / "config.json")
        result = adapters.Channel(cfg["channel"]).send("hi")
        self.assertFalse(result["ok"])

    def test_cycle_script_missing_is_not_fatal(self):
        write_entry(self.memory, "m-20261109-01", weight=0.90)
        self.build_index()
        self.config(cycle_script=str(self.dir / "missing.py"))
        self.seed_state(last_user_at="2026-11-09T20:00")
        out = self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertIn("[tick] spoke", out)


class IntegrationTests(FishTest):
    """The things that break in real use: paths, parsing, a bad card, a confirmation."""

    def test_relative_paths_resolve_against_the_config_dir(self):
        config = {
            "persona": "persona.json",
            "memory_root": "memory",
            "state_file": "state.json",
            "cycle_script": str(SKILL_SCRIPTS / "memory_cycle.py"),
            "model": {"mode": "offline"},
            "channel": {"type": "stdout"},
            "beat": {"interval_seconds": 900, "quiet_hours": [23, 7]},
        }
        path = self.dir / "relative.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        write_entry(self.memory, "m-20261109-01", weight=0.90)
        self.build_index()
        self.seed_state(last_user_at="2026-11-09T20:00")
        self.run_cmd(["tick", "--config", str(path), "--now", self.now])
        self.assertTrue(self.state_file.exists(), "state must land beside the config, not beside cwd")
        self.assertGreater(json.loads(self.state_file.read_text(encoding="utf-8"))["sent_today"], 0)

    def test_index_with_four_decimal_weights_is_parsed(self):
        (self.memory / "INDEX.md").write_text(
            "# Memory index (generated 2026-11-10)\n\n"
            "- m-20261109-01 [identity w=0.8600] identity | A\n"
            "- m-20261109-02 [active w=0.4712] fact | B\n",
            encoding="utf-8",
        )
        entries = fish.read_index(self.memory)
        self.assertEqual([e["id"] for e in entries], ["m-20261109-01", "m-20261109-02"])
        self.assertAlmostEqual(entries[0]["weight"], 0.86)

    def test_recall_returns_nothing_for_an_unrelated_topic(self):
        write_entry(self.memory, "m-20261109-01", weight=0.95, body="Prefers dark mode.\n")
        write_entry(self.memory, "m-20261109-02", weight=0.90, body="Uses ruff for linting.\n")
        entries = self.build_index()
        self.assertEqual(fish.recall(entries, "今天天气不错"), [],
                         "an unrelated message must not touch the heaviest memories")

    def test_decide_does_not_mutate_state(self):
        write_entry(self.memory, "m-20261109-01", weight=0.90)
        entries = self.build_index()
        state = {"last_user_at": "2026-11-09T20:00", "sent_today": 1,
                 "sent_day": "2026-11-09", "started_at": "2026-11-01T00:00"}
        before = json.dumps(state, sort_keys=True)
        may, _reason = fish.decide(fish.merge(fish.DEFAULTS, {}), state, entries,
                                   datetime(2026, 11, 10, 10, 0))
        self.assertTrue(may)
        self.assertEqual(json.dumps(state, sort_keys=True), before)

    def test_broken_persona_card_falls_back_instead_of_crashing(self):
        self.persona.write_text('{"name": "Tide",', encoding="utf-8")
        write_entry(self.memory, "m-20261109-01", weight=0.90)
        self.build_index()
        self.config()
        self.seed_state(last_user_at="2026-11-09T20:00")
        out = self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertIn("[tick] spoke", out)

    def test_broken_mood_timestamp_uses_the_stored_mood(self):
        state = {"mood": 0.5, "mood_at": "not-a-date"}
        self.assertAlmostEqual(fish.mood_now(state, datetime(2026, 11, 10, 10, 0)), 0.5)

    def test_verify_marks_a_confirmation(self):
        write_entry(self.memory, "m-20261109-01", weight=0.80, hits=2, verified=0,
                    body="User wants Chinese replies.\n")
        self.build_index()
        self.config()
        self.seed_state(mood=0.0)
        out = self.run_cmd(["verify", "--config", str(self.dir / "config.json"),
                            "--now", self.now, "--ids", "m-20261109-01"])
        self.assertIn("[verify] confirmed", out)
        meta = mc.parse_entry(self.memory / "entries" / "m-20261109-01.md")
        self.assertEqual(meta["verified"], 1)
        self.assertGreater(meta["weight"], 0.80)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertGreater(state["mood"], 0)

    def test_verify_unknown_id_fails_loudly(self):
        write_entry(self.memory, "m-20261109-01", weight=0.80)
        self.build_index()
        self.config()
        self.seed_state()
        rc = fish.main(["verify", "--config", str(self.dir / "config.json"),
                        "--now", self.now, "--ids", "m-nope"])
        self.assertNotEqual(rc, 0)

    def test_say_can_skip_recording(self):
        write_entry(self.memory, "m-20261109-01", weight=0.55, body="Prefers tabs.\n")
        self.build_index()
        self.config()
        self.seed_state()
        before = set((self.memory / "entries").glob("*.md"))
        self.run_cmd(["say", "--config", str(self.dir / "config.json"), "--now", self.now,
                      "--text", "记住我用的是制表符缩进而不是空格", "--no-record"])
        self.assertEqual(set((self.memory / "entries").glob("*.md")), before)

    def test_greeting_is_not_recorded_by_default(self):
        write_entry(self.memory, "m-20261109-01", weight=0.55, body="Prefers tabs.\n")
        self.build_index()
        self.config()
        self.seed_state()
        out = self.run_cmd(["say", "--config", str(self.dir / "config.json"), "--now", self.now,
                            "--text", "在吗"])
        self.assertIn("nothing durable", out)
        self.assertEqual(len(list((self.memory / "entries").glob("m-20261110-*.md"))), 0)

    def test_record_policy_always_records_the_greeting(self):
        write_entry(self.memory, "m-20261109-01", weight=0.55, body="Prefers tabs.\n")
        self.build_index()
        self.config(record_episodes="always")
        self.seed_state()
        self.run_cmd(["say", "--config", str(self.dir / "config.json"), "--now", self.now,
                      "--text", "在吗"])
        self.assertEqual(len(list((self.memory / "entries").glob("m-20261110-*.md"))), 1)

    def test_run_once_takes_a_single_beat(self):
        write_entry(self.memory, "m-20261109-01", weight=0.90)
        self.build_index()
        self.config()
        self.seed_state(last_user_at="2026-11-09T20:00")
        self.run_cmd(["run", "--once", "--config", str(self.dir / "config.json"),
                      "--now", self.now])


class RuntimeHygieneTests(FishTest):
    """Bugs a reviewer finds by running it: config keys, Ctrl-C, corrupt state, stale INDEX."""

    def test_model_key_is_honoured(self):
        client = adapters.ModelClient({"mode": "openai", "model": "deepseek-chat"}, {})
        self.assertEqual(client.model, "deepseek-chat")
        legacy = adapters.ModelClient({"mode": "openai", "name": "gpt-4o"}, {})
        self.assertEqual(legacy.model, "gpt-4o")

    def test_ctrl_c_while_waiting_stops_cleanly(self):
        from unittest import mock
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        self.build_index()
        self.config()
        self.seed_state(last_user_at="2026-11-09T20:00")
        buffer = io.StringIO()
        with mock.patch("time.sleep", side_effect=KeyboardInterrupt), redirect_stdout(buffer):
            self.assertEqual(fish.main(["run", "--config", str(self.dir / "config.json"),
                                        "--now", self.now]), 0)
        self.assertIn("[run] stopped", buffer.getvalue())

    def test_a_corrupt_state_timestamp_self_heals(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        self.build_index()
        self.config()
        base = {"mood": 0.0, "mood_at": "garbage", "last_user_at": "yesterday",
                "last_nudge_at": "sometime", "sent_today": 0, "sent_day": self.today,
                "started_at": "also-bad"}
        self.state_file.write_text(json.dumps(base), encoding="utf-8")
        out = self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertIn("[tick]", out)
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIsNone(saved["last_user_at"])
        self.assertTrue(saved["started_at"].startswith("2026-11-10T10:00"))

    def test_superseded_entries_are_not_recalled(self):
        write_entry(self.memory, "m-20261001-01", weight=0.9, status="superseded",
                    body="Old preference about docker builds and the platform flag.\n")
        write_entry(self.memory, "m-20261109-01", weight=0.6, status="active",
                    body="Current preference about docker builds.\n")
        entries = self.build_index()
        hits = fish.recall(entries, "docker builds need the platform flag")
        self.assertEqual([e["id"] for e in hits], ["m-20261109-01"])

    def test_say_leaves_a_fresh_index(self):
        write_entry(self.memory, "m-20261109-01", weight=0.80, body="User prefers Chinese replies.\n")
        self.build_index()
        self.config()
        self.seed_state()
        self.run_cmd(["say", "--config", str(self.dir / "config.json"), "--now", self.now,
                      "--text", "记住我的 docker 构建需要 platform 参数"])
        ids = [e["id"] for e in fish.read_index(self.memory)]
        self.assertIn("m-20261110-01", ids, "the episode must be in INDEX before the next tick")

    def test_shared_function_words_are_not_a_shared_topic(self):
        write_entry(self.memory, "m-20261109-04", weight=0.62, kind="fact",
                    body="Keep memory/INDEX.md under 40 lines and the index will stay readable.\n")
        write_entry(self.memory, "m-20261109-01", weight=0.55,
                    body="Docker builds need the platform flag on this machine.\n")
        entries = self.build_index()
        hits = fish.recall(entries, "remember my docker builds need the --platform linux/amd64 flag")
        self.assertEqual([e["id"] for e in hits], ["m-20261109-01"],
                         "the word 'the' must not make two unrelated memories look related")

    def test_state_with_non_numeric_fields_self_heals(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        self.build_index()
        self.config()
        base = {"mood": "abc", "mood_at": self.now, "last_user_at": "2026-11-09T20:00",
                "last_nudge_at": None, "sent_today": "many", "sent_day": self.today,
                "started_at": "2026-11-01T00:00"}
        self.state_file.write_text(json.dumps(base), encoding="utf-8")
        self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["mood"], 0.0)
        self.assertEqual(saved["sent_today"], 1)

    def test_ctrl_c_during_a_tick_stops_cleanly(self):
        from unittest import mock
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        self.build_index()
        self.config()
        self.seed_state()
        buffer = io.StringIO()
        with mock.patch.object(fish, "cmd_tick", side_effect=KeyboardInterrupt), redirect_stdout(buffer):
            self.assertEqual(fish.main(["run", "--config", str(self.dir / "config.json"),
                                        "--now", self.now]), 0)
        self.assertIn("[run] stopped", buffer.getvalue())

    def test_a_future_timestamp_is_treated_as_absent(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        entries = self.build_index()
        state = {"last_user_at": "2026-11-11T09:00", "sent_today": 0, "sent_day": self.today,
                 "started_at": "2026-11-01T00:00"}
        may, reason = fish.decide(fish.merge(fish.DEFAULTS, {}), state, entries,
                                  datetime(2026, 11, 10, 10, 0))
        self.assertTrue(may, reason)
        self.assertNotIn("-", reason)

    def test_no_cycle_script_is_announced(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        self.build_index()
        self.config(cycle_script=None)
        self.seed_state(last_user_at="2026-11-09T20:00")
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors):
            self.assertEqual(fish.main(["tick", "--config", str(self.dir / "config.json"),
                                        "--now", self.now]), 0)
        self.assertIn("no cycle_script configured", errors.getvalue())

    def test_future_started_at_is_reset_and_then_heals(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        self.build_index()
        self.config()
        self.seed_state(started_at="2026-11-11T00:00")
        first = self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertIn("not_idle_enough", first)
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertTrue(saved["started_at"].startswith("2026-11-10T10:00"),
                        "a future start must be reset, not kept")
        second = self.run_cmd(["tick", "--config", str(self.dir / "config.json"),
                               "--now", "2026-11-10T20:00"])
        self.assertIn("[tick] spoke", second, "one beat later it must be able to speak again")

    def test_missing_state_file_is_not_fatal(self):
        self.assertIsInstance(fish.load_state(self.dir / "missing.json"), dict)

    def test_heal_runs_on_every_beat_so_a_mid_run_jump_recovers(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        self.build_index()
        cfg = fish.load_config(self.config())
        self.seed_state()
        first = self.run_cmd(["tick", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertIn("[tick] spoke", first)
        # the OS clock jumps back a day: the stored stamps now sit in the future, and a running process
        # holds this state in memory instead of reloading it, so the CLI entry point would never see them
        state = fish.load_state(self.state_file)
        state["started_at"] = "2026-11-12T00:00"
        state["last_user_at"] = "2026-11-12T00:00"
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors):
            self.assertEqual(fish.cmd_tick(cfg, state, datetime(2026, 11, 10, 10, 0)), 0)
        self.assertEqual(errors.getvalue().count("started_at is in the future"), 1)
        self.assertTrue(state["started_at"].startswith("2026-11-10T10:00"),
                        "the beat itself must rewrite a future start, not just the CLI entry point")
        self.assertIsNone(state["last_user_at"])
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertTrue(saved["started_at"].startswith("2026-11-10T10:00"))

    def test_status_persists_a_healed_timestamp(self):
        write_entry(self.memory, "m-20261109-01", weight=0.9)
        self.build_index()
        self.config()
        self.seed_state(started_at="2026-11-11T00:00")
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.run_cmd(["status", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertIn("started_at is in the future", errors.getvalue())
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertTrue(saved["started_at"].startswith("2026-11-10T10:00"))
        # the warning must not repeat on the next call: the file is already correct
        second = io.StringIO()
        with redirect_stderr(second):
            self.run_cmd(["status", "--config", str(self.dir / "config.json"), "--now", self.now])
        self.assertNotIn("in the future", second.getvalue())


class FormatTests(unittest.TestCase):
    def test_parse_now_accepts_iso_and_space(self):
        self.assertEqual(fish.parse_now("2026-11-10T10:00"), datetime(2026, 11, 10, 10, 0))
        self.assertEqual(fish.parse_now("2026-11-10 10:00"), datetime(2026, 11, 10, 10, 0))
        self.assertEqual(fish.parse_now("2026-11-10"), datetime(2026, 11, 10, 0, 0))

    def test_parse_now_rejects_garbage(self):
        with self.assertRaises(SystemExit):
            fish.parse_now("yesterday")


if __name__ == "__main__":
    unittest.main()
