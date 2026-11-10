"""Tests for the configurator an assistant drives: typed values, no silent typo, no invalid write."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

import setup  # noqa: E402


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "config.bot.json"
        os.environ.pop("FISH_TG_TOKEN", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_setup(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = setup.main(list(argv) + ["--config", str(self.config)])
        return code, out.getvalue(), err.getvalue()

    def test_values_are_typed_from_json(self):
        self.assertEqual(setup.parse_value("true"), True)
        self.assertEqual(setup.parse_value("1200"), 1200)
        self.assertEqual(setup.parse_value("[23,7]"), [23, 7])
        self.assertEqual(setup.parse_value("FISH_TG_TOKEN"), "FISH_TG_TOKEN")

    def test_a_bare_token_name_is_not_quoted_json(self):
        code, out, _err = self.run_setup("set", "platform=[\"telegram\"]",
                                         "telegram.allow_chat_ids=[111]",
                                         "telegram.token_env=FISH_TG_TOKEN")
        self.assertEqual(code, 0)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["telegram"]["token_env"], "FISH_TG_TOKEN")

    def test_an_unknown_key_is_refused_and_nothing_is_written(self):
        code, _out, err = self.run_setup("set", "telegram.allow_chats=[111]")
        self.assertEqual(code, 3)
        self.assertIn("unknown key", err)
        self.assertIn("allow_chat_ids", err)
        self.assertFalse(self.config.exists())

    def test_a_target_outside_the_allowlist_is_refused_and_the_old_file_survives(self):
        code, _out, _err = self.run_setup("set", "platform=[\"telegram\"]",
                                          "telegram.allow_chat_ids=[111]")
        self.assertEqual(code, 0)
        before = self.config.read_text(encoding="utf-8")
        code, _out, err = self.run_setup("set", "proactive.enabled=true",
                                         "proactive.platform=telegram",
                                         "proactive.target=999")
        self.assertEqual(code, 2)
        self.assertIn("is not in the telegram allowlist", err)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_a_missing_token_does_not_block_writing(self):
        code, _out, err = self.run_setup("set", "platform=[\"telegram\"]",
                                          "telegram.allow_chat_ids=[111]")
        self.assertEqual(code, 0, err)
        code, _out, err = self.run_setup("check")
        self.assertEqual(code, 2)
        self.assertIn("FISH_TG_TOKEN is unset", err)

    def test_show_speaks_the_effective_behaviour(self):
        code, _out, _err = self.run_setup("set", "platform=[\"telegram\"]",
                                          "telegram.allow_chat_ids=[111]",
                                          "proactive.enabled=true", "proactive.platform=telegram",
                                          "proactive.target=111", "proactive.interval_seconds=1200",
                                          "beat.quiet_hours=[22,8]", "beat.max_messages_per_day=2")
        self.assertEqual(code, 0)
        _code, out, _err = self.run_setup("show")
        self.assertIn("quiet 22:00–08:00", out)
        self.assertIn("at most 2/day", out)
        self.assertIn("checks every 20 min", out)
        self.assertIn("proactive: on", out)

    def test_defaults_show_it_as_silent(self):
        _code, out, _err = self.run_setup("show")
        self.assertIn("proactive: off", out)
        self.assertIn("every 15 min", out)

    def test_a_wrong_shape_is_a_bad_value_not_a_bad_config(self):
        code, _out, err = self.run_setup("set", "beat.quiet_hours=8")
        self.assertEqual(code, 3)
        self.assertIn("two hours", err)
        self.assertFalse(self.config.exists())

    def test_a_bare_id_where_a_list_belongs_is_refused_with_the_bracketed_form(self):
        code, _out, err = self.run_setup("set", "telegram.allow_chat_ids=123456")
        self.assertEqual(code, 3)
        self.assertIn("[123456789]", err)

    def test_a_corrupt_config_is_reported_not_crashed(self):
        self.config.write_text("{not json", encoding="utf-8")
        code, _out, err = self.run_setup("show")
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)

    def test_check_passes_with_tokens_and_allowlist(self):
        os.environ["FISH_TG_TOKEN"] = "x"
        self.run_setup("set", "platform=[\"telegram\"]", "telegram.allow_chat_ids=[111]")
        code, out, _err = self.run_setup("check")
        self.assertEqual(code, 0)
        self.assertIn("can start", out)


if __name__ == "__main__":
    unittest.main()
