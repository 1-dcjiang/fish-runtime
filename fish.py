#!/usr/bin/env python3
"""fish-runtime: an always-on character with a weighted memory that fades.

The brain of a "大肥鱼"-style resident bot, for hosts that have no plugin runtime:
it owns the clock (a heartbeat decides when to speak), the mood, and the memory
arithmetic — and delegates decay to the persona-and-memory skill's cycle script.

Commands:
  status                    print state, per-status counts, and whether it may speak (and why not)
  tick                      one heartbeat: maybe speak, then run the memory cycle
  run                       loop tick until interrupted (--once runs a single tick)
  say --text "..."          the user said something: recall, reply, maybe record
  verify --ids m-...        the user confirmed a memory: verified+1, mood up

--now gives deterministic runs (run honours it for the first beat only).
Relative paths in the config resolve against the config file's directory.
Stdlib only. No network unless a channel or model is configured.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapters  # noqa: E402

INDEX_LINE = re.compile(
    r"- (?P<id>[A-Za-z0-9-]+) \[(?P<status>[a-z]+) w=(?P<weight>[\d.]+)\] "
    r"(?P<kind>[a-z]+) \| (?P<text>.*)"
)

DEFAULTS = {
    "persona": "memory/persona.json",
    "memory_root": "memory",
    "state_file": "state.json",
    "cycle_script": None,
    "run_cycle_on_tick": True,
    "record_episodes": "auto",
    "model": {"mode": "offline"},
    "channel": {"type": "stdout"},
    "beat": {
        "quiet_hours": [23, 7],
        "min_gap_minutes": 20,
        "nudge_after_hours": 6,
        "nudge_cooldown_hours": 12,
        "max_messages_per_day": 3,
        "min_weight": 0.5,
    },
}

MOOD_DECAY_PER_DAY = 0.6
MOOD_USER_BOOST = 0.15
MOOD_FOLD_DROP = 0.2
MOOD_PROMOTE_RISE = 0.3
MOOD_VERIFY_RISE = 0.2

# "in" / "hi" / "在吗" is not a memory. Recording it costs a file and pushes real memories down.
EPHEMERAL = re.compile(
    r"^(hi|hey|hello|yo|ok|thanks|thx|good night|good morning|bye|"
    r"在吗|在么|你好|您好|哈喽|嗨|早|早安|晚安|拜拜|收到|好的|谢谢|嗯+|啊+|哦+)$",
    re.IGNORECASE,
)
DURABLE_MIN_CHARS = 12


def merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"error: config not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        return merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        print(f"error: config is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(2)


def resolve_paths(cfg: dict, base: Path) -> dict:
    """Config-relative paths, so the runtime can be launched from anywhere."""
    for key in ("persona", "memory_root", "state_file", "cycle_script"):
        value = cfg.get(key)
        if value and not Path(value).is_absolute():
            cfg[key] = str((base / value).resolve())
    return cfg


def parse_timestamp(value) -> datetime | None:
    """One bad string in state.json must not wedge the heartbeat — absent is the safe reading."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        print(f"warning: state timestamp {value!r} is unusable; treated as absent", file=sys.stderr)
        return None


TIMESTAMP_KEYS = ("mood_at", "last_user_at", "last_nudge_at", "started_at")


def load_state(path: Path) -> dict:
    state = {"mood": 0.0, "mood_at": None, "last_user_at": None,
             "last_nudge_at": None, "sent_today": 0, "sent_day": None}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            print(f"warning: state file unreadable ({exc}), starting fresh", file=sys.stderr)
            return state
        if not isinstance(loaded, dict):
            print("warning: state file is not an object, starting fresh", file=sys.stderr)
            return state
        state.update(loaded)
    for key in TIMESTAMP_KEYS:
        if key in state and state[key] and parse_timestamp(state[key]) is None:
            state[key] = None
    # a string where a number belongs must not reach float()/int() deep inside a tick
    for key, cast, default in (("mood", float, 0.0), ("sent_today", int, 0)):
        try:
            state[key] = cast(state[key])
        except (TypeError, ValueError):
            print(f"warning: state {key}={state[key]!r} is not a number; reset to {default}",
                  file=sys.stderr)
            state[key] = default
    if state.get("sent_day") is not None and not isinstance(state["sent_day"], str):
        state["sent_day"] = None
    return state


def save_state(path: Path, state: dict) -> None:
    """Write through a temp file: a crash mid-write must not leave unparseable state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def heal_future_timestamps(state: dict, now: datetime) -> list[str]:
    """Drop any timestamp ahead of the clock. Returns the keys that were reset.

    A clock that jumped back — or a --now replay earlier than the stored state — otherwise silences the
    character forever: decide() treats a future stamp as absent, so idle never accumulates. Called at the
    entry of every command *and* at the start of each beat, so a jump mid-process heals without a restart.
    """
    healed = []
    for key in TIMESTAMP_KEYS:
        stamp = parse_timestamp(state.get(key))
        if stamp and stamp > now:
            print(f"warning: state {key} is in the future ({stamp.isoformat()}); reset",
                  file=sys.stderr)
            state[key] = now.isoformat() if key == "started_at" else None
            healed.append(key)
    if not state.get("started_at"):
        state["started_at"] = now.isoformat()
    return healed


def mood_now(state: dict, now: datetime) -> float:
    raw = float(state.get("mood", 0.0))
    stamp = state.get("mood_at")
    if not stamp:
        return raw
    try:
        since = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        print(f"warning: mood_at {stamp!r} is not a timestamp, mood used as stored",
              file=sys.stderr)
        return raw
    days = max(0.0, (now - since).total_seconds() / 86400.0)
    value = max(-1.0, min(1.0, raw * (MOOD_DECAY_PER_DAY ** days)))
    return 0.0 if abs(value) < 0.02 else value


def roll_day(state: dict, today: str) -> None:
    """Reset the daily counter when the date changed — outside decide(), so that stays pure."""
    if state.get("sent_day") != today:
        state["sent_day"] = today
        state["sent_today"] = 0


def ensure_index(cfg: dict, memory_root: Path, today: str) -> None:
    """A fresh store has no index yet; build it before the first read."""
    if (memory_root / "INDEX.md").exists():
        return
    run_index(cfg, memory_root, today)


def run_index(cfg: dict, memory_root: Path, today: str) -> str | None:
    """Rebuild INDEX.md, so roles and statuses never lag the entry files. Returns the raw error, if any."""
    script = cfg.get("cycle_script")
    if not script or not Path(script).exists():
        return None
    try:
        result = subprocess.run([sys.executable, str(script), "index", "--root", str(memory_root),
                                 "--today", today], capture_output=True, text=True, timeout=60)
    except Exception as exc:
        print(f"warning: could not rebuild the index: {exc}", file=sys.stderr)
        return str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        note = detail[0] if detail else f"exit {result.returncode}"
        print(f"warning: index rebuild failed: {note}", file=sys.stderr)
        return note
    return None


def read_index(memory_root: Path) -> list[dict]:
    path = memory_root / "INDEX.md"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = INDEX_LINE.match(line.strip())
        if match:
            entries.append({
                "id": match.group("id"),
                "status": match.group("status"),
                "weight": float(match.group("weight")),
                "kind": match.group("kind"),
                "text": match.group("text").strip(),
            })
    return entries


STOPWORDS = {
    # Common words that appear in almost every memory. They are not evidence of a shared topic.
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are",
    "was", "were", "be", "been", "it", "its", "that", "this", "these", "those", "my", "your",
    "i", "you", "we", "they", "he", "she", "do", "does", "did", "not", "no", "but", "so",
    "as", "at", "by", "from", "if", "then", "than", "there", "here", "have", "has", "had",
    "的", "了", "是", "我", "你", "他", "她", "它", "在", "和", "就", "也", "都", "很",
}


def tokens(text: str) -> set[str]:
    """Rough tokenisation that also works for CJK: words plus character bigrams, minus stopwords."""
    clean = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (text or "").lower())
    words = {w for w in clean.split() if len(w) > 1 and w not in STOPWORDS}
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", clean):
        words.update(run[i:i + 2] for i in range(len(run) - 1) if run[i:i + 2] not in STOPWORDS)
    return words


def recall(entries: list[dict], text: str, limit: int = 3) -> list[dict]:
    """Relevant memories only. No text at all falls back to the heaviest — a topic never does.

    Retired entries (`superseded`, `archived`) are not recallable: they still show in INDEX for a
    while, and a replaced preference should not be echoed back as if it were current.
    """
    live = [e for e in entries if e["status"] not in {"superseded", "archived"}]
    if not text:
        return sorted(live, key=lambda e: -e["weight"])[:limit]
    query = tokens(text)
    if not query:
        return []
    scored = []
    for entry in live:
        overlap = len(query & tokens(entry["text"]))
        if overlap:
            scored.append((overlap * 2 + entry["weight"], entry))
    scored.sort(key=lambda pair: -pair[0])
    return [entry for _score, entry in scored][:limit]


def peak_memory(entries: list[dict], min_weight: float) -> dict | None:
    candidates = [e for e in entries
                  if e["status"] not in {"archived", "superseded"} and e["weight"] >= min_weight]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e["weight"])


def in_quiet_hours(now: datetime, quiet: list[int] | None) -> bool:
    if not quiet or len(quiet) != 2:
        return False
    start, end = quiet
    if start == end:
        return False
    hour = now.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def in_the_past(stamp: datetime | None, now: datetime, label: str) -> datetime | None:
    """A timestamp from the future is a corrupted clock, not a reason to stay silent for a day."""
    if stamp is None:
        return None
    if stamp > now:
        print(f"warning: state {label} is in the future ({stamp.isoformat()}); ignored",
              file=sys.stderr)
        return None
    return stamp


def decide(cfg: dict, state: dict, entries: list[dict], now: datetime) -> tuple[bool, str]:
    """Read-only decision: may the character speak now, and why not if not."""
    beat = cfg["beat"]
    if in_quiet_hours(now, beat.get("quiet_hours")):
        return False, "quiet_hours"
    sent_day = state.get("sent_day")
    sent_today = 0 if sent_day != now.date().isoformat() else int(state.get("sent_today", 0))
    if sent_today >= int(beat.get("max_messages_per_day", 3)):
        return False, "daily_cap"
    last_user = in_the_past(parse_timestamp(state.get("last_user_at")), now, "last_user_at")
    if last_user:
        gap = (now - last_user).total_seconds() / 60.0
        if gap < float(beat.get("min_gap_minutes", 20)):
            return False, f"recent_user_message ({int(gap)}m ago)"
    last_nudge = in_the_past(parse_timestamp(state.get("last_nudge_at")), now, "last_nudge_at")
    if last_nudge:
        cool = (now - last_nudge).total_seconds() / 3600.0
        if cool < float(beat.get("nudge_cooldown_hours", 12)):
            return False, f"cooldown ({cool:.1f}h left of {beat.get('nudge_cooldown_hours')}h)"
    idle_from = (in_the_past(parse_timestamp(state.get("last_user_at")), now, "last_user_at")
                 or in_the_past(parse_timestamp(state.get("started_at")), now, "started_at"))
    if idle_from is None:
        # nothing trustworthy to measure from: count idle from now, the state heals on the next save
        idle_from = now
    idle_hours = (now - idle_from).total_seconds() / 3600.0
    if idle_hours < float(beat.get("nudge_after_hours", 6)):
        return False, f"not_idle_enough ({idle_hours:.1f}h)"
    if not peak_memory(entries, float(beat.get("min_weight", 0.5))):
        return False, "nothing_worth_saying"
    return True, f"idle {idle_hours:.1f}h"


def run_cycle(cfg: dict, memory_root: Path, today: str) -> dict | None:
    script = cfg.get("cycle_script")
    if not script:
        print("warning: no cycle_script configured, so memory never decays or folds", file=sys.stderr)
        return None
    path = Path(script)
    if not path.exists():
        print(f"warning: cycle script not found: {path}", file=sys.stderr)
        return None
    command = [sys.executable, str(path), "cycle", "--root", str(memory_root),
               "--today", today, "--json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        print(f"warning: memory cycle failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"warning: memory cycle exited {result.returncode}: {result.stderr.strip()}",
              file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def apply_cycle_mood(state: dict, cycle: dict | None, now: datetime) -> None:
    if not cycle:
        return
    mood = mood_now(state, now)
    for _id in cycle.get("folded", []):
        mood -= MOOD_FOLD_DROP
    for _id in cycle.get("promoted", []):
        mood += MOOD_PROMOTE_RISE
    state["mood"] = max(-1.0, min(1.0, mood))
    state["mood_at"] = now.isoformat()


def new_episode_id(memory_root: Path, now: datetime) -> str:
    day = now.strftime("%Y%m%d")
    prefix = f"m-{day}-"
    used = 0
    entries_dir = memory_root / "entries"
    if entries_dir.is_dir():
        for path in entries_dir.glob(f"{prefix}*.md"):
            try:
                used = max(used, int(path.stem.rsplit("-", 1)[1]))
            except (IndexError, ValueError):
                continue
    return f"{prefix}{used + 1:02d}"


def should_record(cfg: dict, text: str) -> bool:
    policy = str(cfg.get("record_episodes", "auto")).lower()
    if policy == "off":
        return False
    if policy == "always":
        return True
    body = " ".join((text or "").split())
    if len(body) < DURABLE_MIN_CHARS:
        return False
    return not EPHEMERAL.match(body)


def write_episode(memory_root: Path, text: str, now: datetime) -> Path:
    entries_dir = memory_root / "entries"
    entries_dir.mkdir(parents=True, exist_ok=True)
    entry_id = new_episode_id(memory_root, now)
    body = " ".join(text.split())[:400]
    path = entries_dir / f"{entry_id}.md"
    path.write_text(
        "---\n"
        f"id: {entry_id}\n"
        "kind: episode\n"
        "weight: 0.45\n"
        "hits: 0\n"
        "verified: 0\n"
        "status: active\n"
        "decay: 0.92\n"
        f"created: {now.date().isoformat()}\n"
        f"last_seen: {now.date().isoformat()}\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def load_persona(cfg: dict) -> dict:
    fallback = {"name": "fish", "self": "我", "user": "你", "language": "Chinese",
                "register": "casual", "emoji_policy": "none", "flourish": [],
                "refusal_style": "直接说不"}
    path = Path(cfg["persona"])
    if not path.exists():
        return fallback
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: persona card unreadable ({exc}); using the default voice",
              file=sys.stderr)
        return fallback
    if not isinstance(card, dict):
        print("warning: persona card is not an object; using the default voice", file=sys.stderr)
        return fallback
    return card


def cmd_status(cfg: dict, state: dict, now: datetime) -> int:
    memory_root = Path(cfg["memory_root"])
    ensure_index(cfg, memory_root, now.date().isoformat())
    # status is read-only for the memory, but a healed timestamp is worth persisting once: otherwise the
    # same warning fires on every status call and the state on disk stays wrong.
    if heal_future_timestamps(state, now):
        save_state(Path(cfg["state_file"]), state)
    entries = read_index(memory_root)
    peak = peak_memory(entries, float(cfg["beat"].get("min_weight", 0.5)))
    may, reason = decide(cfg, state, entries, now)
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    payload = {
        "now": now.isoformat(timespec="minutes"),
        "mood": round(mood_now(state, now), 2),
        "entries": len(entries),
        "counts": counts,
        "identity": counts.get("identity", 0),
        "peak": peak["id"] if peak else None,
        "may_speak": may,
        "reason": reason,
        "sent_today": state.get("sent_today", 0) if state.get("sent_day") == now.date().isoformat() else 0,
        "channel": cfg["channel"].get("type"),
        "model": cfg["model"].get("mode"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_tick(cfg: dict, state: dict, now: datetime, send=None) -> int:
    """One heartbeat. `send` overrides the configured channel — a bot needs the reply routed to a chat."""
    memory_root = Path(cfg["memory_root"])
    ensure_index(cfg, memory_root, now.date().isoformat())
    # a resident run keeps `state` in memory across beats, so a clock that jumps back mid-process must heal
    # here too — the entry-point heal only covers the first beat of each CLI invocation
    heal_future_timestamps(state, now)
    state.setdefault("started_at", now.isoformat())
    roll_day(state, now.date().isoformat())
    entries = read_index(memory_root)
    may, reason = decide(cfg, state, entries, now)
    persona = load_persona(cfg)

    if may:
        peak = peak_memory(entries, float(cfg["beat"].get("min_weight", 0.5)))
        ref = peak["text"][:90]
        mood = mood_now(state, now)
        lines = [f"- {e['id']} [{e['status']}] {e['kind']} | {e['text']}" for e in entries[:5]]
        text = adapters.ModelClient(cfg["model"], persona).compose(
            ref=ref, mood=mood, proactive=True, memory_lines=lines
        )
        result = (send or adapters.Channel(cfg["channel"]).send)(text)
        if result.get("ok"):
            state["sent_today"] = int(state.get("sent_today", 0)) + 1
            state["sent_day"] = now.date().isoformat()
            state["last_nudge_at"] = now.isoformat()
            print(f"[tick] spoke via {result.get('channel')} (reason: {reason})")
        else:
            print(f"[tick] send failed (not counted toward the daily cap): {result.get('error')}")
    else:
        print(f"[tick] silent ({reason})")

    if cfg.get("run_cycle_on_tick"):
        cycle = run_cycle(cfg, memory_root, now.date().isoformat())
        apply_cycle_mood(state, cycle, now)
        if cycle:
            print(f"[tick] cycle folded={len(cycle.get('folded', []))} "
                  f"promoted={len(cycle.get('promoted', []))} "
                  f"archived={len(cycle.get('archived', []))}")
            entries = read_index(memory_root)
            print(f"[tick] index now {len(entries)} entries")

    save_state(Path(cfg["state_file"]), state)
    return 0


def compose_reply(cfg: dict, state: dict, now: datetime, text: str,
                  record: bool = True) -> dict:
    """Everything one turn needs except the transport: recall, reply text, and the memory writes.

    The transport (stdout, Telegram, a bot) only sends `reply`; all state changes happen here so a
    CLI turn and a chat turn cannot drift apart.
    """
    memory_root = Path(cfg["memory_root"])
    ensure_index(cfg, memory_root, now.date().isoformat())
    roll_day(state, now.date().isoformat())
    persona = load_persona(cfg)

    run_cycle(cfg, memory_root, now.date().isoformat())
    entries = read_index(memory_root)
    hits = recall(entries, text)
    mood = min(1.0, mood_now(state, now) + MOOD_USER_BOOST)

    reply = adapters.ModelClient(cfg["model"], persona).compose(
        ref=hits[0]["text"][:90] if hits else "",
        mood=mood,
        proactive=False,
        memory_lines=[f"- {e['id']} [{e['status']}] {e['kind']} | {e['text']}" for e in hits],
        user_text=text,
    )

    episode = None
    if record and should_record(cfg, text):
        episode = write_episode(memory_root, text, now)
    if hits and cfg.get("cycle_script"):
        ids = [e["id"] for e in hits]
        command = [sys.executable, str(cfg["cycle_script"]), "touch",
                   "--root", str(memory_root), "--ids", *ids,
                   "--today", now.date().isoformat()]
        try:
            touched = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except Exception as exc:
            print(f"warning: could not touch {', '.join(ids)}: {exc}", file=sys.stderr)
            touched = None
        if touched is not None and touched.returncode != 0:
            # a silent no-op here means the reply claimed to remember something it did not
            detail = (touched.stderr or touched.stdout).strip().splitlines()
            note = detail[0] if detail else f"exit {touched.returncode}"
            print(f"warning: touch refused for {', '.join(ids)}: {note}", file=sys.stderr)
    if hits or episode:
        # the cycle rebuilds INDEX before these writes; refresh it so the new state is visible at once
        run_index(cfg, memory_root, now.date().isoformat())

    state["last_user_at"] = now.isoformat()
    state["mood"] = mood
    state["mood_at"] = now.isoformat()
    save_state(Path(cfg["state_file"]), state)
    return {"reply": reply, "hits": [e["id"] for e in hits],
            "episode": episode.name if episode else None}


def cmd_say(cfg: dict, state: dict, now: datetime, text: str, record: bool = True) -> int:
    """Reply to one user message on the configured channel.

    Order is deliberate: settle storage first (cycle → decay, fold, rebuild INDEX), then recall and
    reply, then write the episode/touch and refresh INDEX. Recalling from an already-consistent store
    is what makes the weights mean "how strong is this memory today" instead of "how strong it was
    before the last beat". Remembering the user's own sentence before replying would make the bot
    parrot it back, so that is not done.
    """
    turn = compose_reply(cfg, state, now, text, record=record)
    result = adapters.Channel(cfg["channel"]).send(turn["reply"])

    kept = turn["episode"] or "nothing (nothing durable in it)"
    print(f"[say] recorded {kept}, recalled {', '.join(turn['hits']) or 'nothing'}")
    return 0 if result.get("ok") else 1


def cmd_verify(cfg: dict, state: dict, now: datetime, ids: list[str]) -> int:
    """A confirmation: this memory is independently true. The one thing a runtime cannot infer."""
    memory_root = Path(cfg["memory_root"])
    script = cfg.get("cycle_script")
    if not script or not Path(script).exists():
        print("error: verify needs cycle_script (it owns the memory arithmetic)", file=sys.stderr)
        return 2
    command = [sys.executable, str(script), "verify", "--root", str(memory_root),
               "--ids", *ids, "--today", now.date().isoformat()]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        print(f"warning: verify failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print(f"error: verify exited {result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode
    state["mood"] = min(1.0, mood_now(state, now) + MOOD_VERIFY_RISE)
    state["mood_at"] = now.isoformat()
    save_state(Path(cfg["state_file"]), state)
    print(f"[verify] confirmed: {', '.join(ids)} (mood +{MOOD_VERIFY_RISE})")
    return 0


def cmd_run(cfg: dict, state: dict, now: datetime | None, once: bool) -> int:
    import time

    interval = int(cfg["beat"].get("interval_seconds", 900))
    if not state.get("started_at"):
        state["started_at"] = (now or datetime.now()).isoformat()
    first = True
    while True:
        stamp = now if first else datetime.now().replace(microsecond=0)
        first = False
        try:
            cmd_tick(cfg, state, stamp)
        except KeyboardInterrupt:
            # Ctrl-C during a tick (an HTTP timeout, a subprocess) is still a clean stop
            save_state(Path(cfg["state_file"]), state)
            print("[run] stopped")
            return 0
        except Exception as exc:
            # a resident process must survive one bad tick: log it, keep the heartbeat
            print(f"[run] tick failed: {exc}", file=sys.stderr)
        if once:
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            # Ctrl-C while waiting is the normal way this stops; no traceback, state on disk
            save_state(Path(cfg["state_file"]), state)
            print("[run] stopped")
            return 0


def parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    print(f"error: cannot parse --now {value!r}", file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="always-on character runtime")
    parser.add_argument("command", choices=["status", "tick", "run", "say", "verify"])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--now", help="override the clock (ISO), for tests")
    parser.add_argument("--once", action="store_true", help="run: single tick only")
    parser.add_argument("--text", default="", help="say: what the user said")
    parser.add_argument("--ids", nargs="+", default=[], help="verify: memory ids to confirm")
    parser.add_argument("--no-record", action="store_true",
                        help="say: reply but write no episode")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    cfg = load_config(config_path)
    cfg = resolve_paths(cfg, config_path.resolve().parent)
    state = load_state(Path(cfg["state_file"]))
    now = parse_now(args.now)

    if now is None:
        now = datetime.now().replace(microsecond=0)
    # A timestamp ahead of the clock (a clock that jumped back, or a --now replay) would otherwise
    # silence the character forever: decide() ignores it, so idle can never accumulate. Reset it here,
    # once, where the current time is actually known — decide() stays read-only.
    healed = heal_future_timestamps(state, now)
    if healed and args.command == "status":
        # status writes nothing else, so without this the same warning would repeat on every call
        save_state(Path(cfg["state_file"]), state)

    if args.command == "status":
        return cmd_status(cfg, state, now)
    if args.command == "tick":
        return cmd_tick(cfg, state, now)
    if args.command == "run":
        return cmd_run(cfg, state, now, once=args.once)
    if args.command == "say":
        if not args.text.strip():
            print("error: --text is required", file=sys.stderr)
            return 2
        return cmd_say(cfg, state, now, args.text, record=not args.no_record)
    if args.command == "verify":
        if not args.ids:
            print("error: --ids is required", file=sys.stderr)
            return 2
        return cmd_verify(cfg, state, now, args.ids)
    return 2


if __name__ == "__main__":
    sys.exit(main())
