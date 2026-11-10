"""setup.py — configure the bot without hand-editing JSON.

Non-interactive on purpose: the person who owns the bot says what they want in plain language, an assistant
translates that into `key=value` assignments and runs this. Every write is validated first and typed from
JSON, so `true`, `1200`, `[23, 7]` and a bare token name all land as the right kind of value.

  python setup.py show                          # what will this bot actually do?
  python setup.py set proactive.enabled=true \
                    proactive.interval_seconds=1200 \
                    beat.quiet_hours=[23,7] \
                    beat.max_messages_per_day=2
  python setup.py check                         # validate; exit 2 with the exact reason

Exit codes: 0 ok · 2 configuration is invalid · 3 bad key or value (nothing written).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import bot
import fish

DEFAULT_CONFIG = "config.bot.json"

# Paths a person is allowed to set through this tool. Anything else is a typo that would otherwise be
# silently ignored by the runtime (the classic "documented key nobody reads" failure).
SCALARS = {
    "platform", "persona", "memory_root", "state_file", "cycle_script", "record_episodes",
    "model.mode", "model.model", "model.base_url", "model.api_key_env",
    "channel.type", "channel.url", "channel.token_env", "channel.chat_id_env",
}
SECTIONS = {
    "telegram": {"token_env", "allow_chat_ids", "allow_user_ids"},
    "discord": {"token_env", "allow_channel_ids", "allow_user_ids"},
    "beat": {"interval_seconds", "quiet_hours", "min_gap_minutes", "nudge_after_hours",
             "nudge_cooldown_hours", "max_messages_per_day", "min_weight", "include_memory"},
    "limits": {"max_messages_per_minute", "max_message_chars"},
    "proactive": {"enabled", "platform", "target", "interval_seconds", "include_memory"},
    "model": {"mode", "model", "name", "base_url", "api_key_env"},
    "channel": {"type", "url", "token_env", "chat_id_env"},
}


class ConfigError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def defaults() -> dict:
    return fish.merge(fish.merge(fish.DEFAULTS, bot.BOT_DEFAULTS), {})


def load(path: Path) -> dict:
    if not path.exists():
        return defaults()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(2, f"{path} is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        raise ConfigError(2, f"{path} must contain a JSON object")
    return fish.merge(defaults(), raw)


def save(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_value(text: str):
    """JSON if it parses, otherwise a bare string — so a token variable name needs no quoting."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def check_path(path: str) -> None:
    if path in SCALARS:
        return
    root = path.split(".")[0]
    if root not in SECTIONS:
        raise ConfigError(3, f"unknown key: {path} (try `python setup.py show` for the real ones)")
    if "." not in path:
        raise ConfigError(3, f"{path} is a section; set a field inside it, e.g. {path}.enabled")
    leaf = path.split(".", 1)[1]
    if leaf not in SECTIONS[root]:
        known = ", ".join(sorted(SECTIONS[root]))
        raise ConfigError(3, f"unknown key: {path} (known in {root}: {known})")


def apply_assignment(cfg: dict, assignment: str) -> tuple[str, object, object]:
    if "=" not in assignment:
        raise ConfigError(3, f"expected key=value, got {assignment!r}")
    key, _, raw = assignment.partition("=")
    key = key.strip()
    check_path(key)
    before = current(cfg, key)
    value = parse_value(raw.strip())
    if "." in key:
        root, leaf = key.split(".", 1)
        section = cfg.get(root)
        if not isinstance(section, dict):
            section = {}
        section = {**section, leaf: value}
        cfg[root] = section
    else:
        cfg[key] = value
    return key, before, value


def current(cfg: dict, path: str):
    if "." not in path:
        return cfg.get(path)
    root, leaf = path.split(".", 1)
    return (cfg.get(root) or {}).get(leaf)


def structural_problems(cfg: dict) -> list[str]:
    """Validation without the token check: a token is exported later, a wrong target is wrong now."""
    return bot.validate(cfg, require_tokens=False)


def token_status(cfg: dict) -> list[str]:
    lines = []
    for platform in bot.platforms_of(cfg):
        section = cfg.get(platform) or {}
        env = section.get("token_env", "")
        lines.append(f"  {platform}: token ${env} {'set' if os.environ.get(env) else 'NOT set'}")
    return lines


def describe(cfg: dict) -> str:
    """The effective behaviour, in the words a person would use to ask for it."""
    lines = [f"platform: {', '.join(bot.platforms_of(cfg)) or '(none)'}"]
    for platform in bot.platforms_of(cfg):
        section = cfg.get(platform) or {}
        key = "allow_chat_ids" if platform == "telegram" else "allow_channel_ids"
        allow = section.get(key) or []
        users = section.get("allow_user_ids") or []
        lines.append(f"  {platform}: answers in {allow or '(nobody — refuses to start)'}"
                     + (f", plus users {users}" if users else ""))
    beat = cfg["beat"]
    quiet = beat.get("quiet_hours") or [23, 7]
    try:
        quiet_text = f"{int(quiet[0]):02d}:00–{int(quiet[1]):02d}:00"
    except (TypeError, ValueError, IndexError):
        quiet_text = f"(malformed: {quiet!r})"
    lines.append(f"beat: every {int(beat.get('interval_seconds', 900)) // 60} min, quiet {quiet_text}, "
                 f"at most {beat.get('max_messages_per_day')}/day, "
                 f"needs {beat.get('nudge_after_hours')}h idle, "
                 f"{beat.get('min_gap_minutes')} min since your last message")
    proactive = cfg.get("proactive") or {}
    if proactive.get("enabled"):
        interval = int(proactive.get("interval_seconds") or 0) or int(beat.get("interval_seconds", 900))
        lines.append(f"proactive: on → {proactive.get('platform')} {proactive.get('target')}, "
                     f"checks every {interval // 60} min, "
                     f"memory quotes {'on' if proactive.get('include_memory') else 'off'}")
    else:
        lines.append("proactive: off (it never speaks first)")
    limits = cfg["limits"]
    lines.append(f"limits: {limits.get('max_messages_per_minute')} replies/min per chat, "
                 f"incoming text cut at {limits.get('max_message_chars')} chars")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="configure fish-runtime's bot without editing JSON")
    parser.add_argument("command", choices=["show", "set", "check"])
    parser.add_argument("assignments", nargs="*", help="set: key=value ...")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)

    path = Path(args.config)
    try:
        cfg = load(path)
        if args.command == "set":
            if not args.assignments:
                raise ConfigError(3, "set needs at least one key=value")
            for assignment in args.assignments:
                key, before, after = apply_assignment(cfg, assignment)
                print(f"{key}: {json.dumps(before, ensure_ascii=False)} -> "
                      f"{json.dumps(after, ensure_ascii=False)}")
            problems = structural_problems(cfg)
            if problems:
                for problem in problems:
                    print(f"error: {problem}", file=sys.stderr)
                print("error: nothing written", file=sys.stderr)
                return 2
            save(path, cfg)
            print(f"written: {path}")
            print(describe(cfg))
            return 0
        if args.command == "show":
            print(describe(cfg))
            print("tokens:")
            print("\n".join(token_status(cfg)) or "  (none)")
            problems = bot.validate(cfg)
            for problem in problems:
                print(f"warning: {problem}", file=sys.stderr)
            return 0
        # check
        problems = bot.validate(cfg)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        if problems:
            return 2
        print("ok: this config can start")
        return 0
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
