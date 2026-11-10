# fish-runtime

A resident runtime for the `persona-and-memory` skill — it gives an assistant a heartbeat, a mood derived
from memory events, and a channel to speak through — on top of the same `memory/` folder the skill already
maintains. Two entry points: `fish.py` for the CLI and the heartbeat, `bot.py` for a bot you leave running on
Telegram and Discord. The skill is distributed as a ZIP (marketplace listing or its own archive), not as a
repo; unzip it next to this directory so `../persona-and-memory/` resolves.

**This is not a skill, and it is deliberately not packaged as one.** See
[Why this can't be listed as a skill](#why-this-cant-be-listed-as-a-skill) before you submit anything to a
marketplace.

```
memory/  ← written by persona-and-memory (the skill)
   ↓ read
fish.py  ← decides whether to speak, composes, sends, then runs the memory cycle
   ↓
stdout / Telegram / webhook / Discord

bot.py   ← same brain, chat transports: long-polling + gateway, allowlisted, rate-limited
```

## What it does

| Command | Effect |
|---|---|
| `status` | one JSON block: mood, entry count, per-status counts, the heaviest memory, whether it may speak, and why not |
| `tick` | one heartbeat: decide → maybe speak → run the memory cycle → persist state |
| `run` | loop `tick` forever at `beat.interval_seconds`; one bad tick is logged, not fatal |
| `say` | records what the **user** said as an episode (if it is worth keeping), touches the memories it matches, raises mood |
| `verify` | marks a memory as independently confirmed (`verified +1`, weight `+0.10`) — the only path to identity |

`--now` takes an explicit ISO timestamp, so a tick can be replayed deterministically instead of "whatever
the clock said". That is what makes the tests below possible. (`run` honours it for the first beat only;
after that a long-running process must follow the real clock.)

Inside `say` the order is deliberate: run the cycle first (decay, fold, rebuild INDEX), then recall and
reply, then write the episode / touch the hits, then rebuild INDEX again. Recalling from a store that is
already consistent is what makes a weight mean "how strong this memory is today"; recalling after touching
would let the bot repeat back the sentence you just said.

## Bot mode: Telegram and Discord

`bot.py` is the same runtime with a chat adapter instead of a stdout one. It long-polls Telegram and connects
to the Discord gateway, answers plain messages in voice, and exposes three maintenance commands
(`/status`, `/verify <id>`, `/help`). Nothing else is registered — no slash-command tree to keep in sync with
the marketplace, no webhook URL.

```bash
# tokens live in the environment, never in the config file
export FISH_TG_TOKEN=...          # @BotFather → /newbot → token
export FISH_DISCORD_TOKEN=...     # Developer Portal → Bot → Reset Token
python bot.py --config config.bot.example.json
python bot.py --config config.bot.example.json --platform telegram
python bot.py --config config.bot.example.json --once     # one poll round, then exit
```

Setup, in the order that actually matters:

| Platform | What to do |
|---|---|
| Telegram | `/newbot` with @BotFather → token; send the bot one message; read your numeric chat id from `https://api.telegram.org/bot<token>/getUpdates`; put it in `allow_chat_ids` |
| Discord | Developer Portal → New Application → Bot → **enable MESSAGE CONTENT INTENT** → invite with `bot` scope → copy the channel id (right-click → Copy ID with Developer Mode on); put it in `allow_channel_ids` |

`config.bot.example.json` is the whole surface: `platform`, the same `persona`/`memory_root`/`cycle_script`
as the CLI, per-platform `token_env` + allowlists, and `limits`. IDs are compared as strings, so quoting a
snowflake id in JSON is harmless.

Four guards, because an unattended bot is a different threat model from a CLI:

- **No allowlist, no start.** `validate()` refuses to boot without one — the failure mode of a public bot
  that answers everyone is not "noisy", it is "someone else controls your persona's mouth".
- **Bot authors are ignored**, so two bots cannot loop on each other.
- **Guild messages need a mention** (`<@bot>` or a leading `/`); DMs are answered directly.
- **Rate limit per chat** (`limits.max_messages_per_minute`, default 6/min) and a hard `max_message_chars`
  truncation before anything reaches memory. Tokens come from the environment, and message bodies never
  appear in logs — only ids, counts and `recalled …`.

Replies are chunked at the platform limit (Telegram 4096, Discord 2000) on newline/space boundaries, so a
long answer does not get rejected with a 400.

What bot mode deliberately is not: no attachments, no voice, no embeds, no slash-command registration, no
Discord resume (a disconnect re-identifies from scratch), and one polling process per token — Telegram's
`getUpdates` conflicts with a configured webhook, so turn the webhook off first.

## Why this can't be listed as a skill

Marketplaces that sell SKILL.md skills (Agensi, the Claude plugin directory, `skills.sh`) share one contract:
**the ZIP contains a `<name>/SKILL.md`, the buyer's agent loads it on demand, and nothing runs behind the
buyer's back.** Agensi states it as: no obfuscated code, no undisclosed network calls, no hidden behaviour,
and the skill must do what the listing says when the agent runs it.

A resident runtime breaks that contract in three ways that cannot be argued away:

1. **It is not loaded, it is run.** The product is a process with a loop, not a procedure the host invokes.
2. **It egresses.** Telegram and webhook channels are outbound network calls by definition; `model.mode =
   openai` sends conversation text to a third party.
3. **It acts without being asked.** A heartbeat that messages you on its own is exactly the "hidden
   behaviour" a security scan is built to flag.

Bot mode makes this sharper rather than blurrier: it holds a token to a real account, sits in a real chat,
and answers people. There is no version of that which a marketplace reviewer could approve as a SKILL.md.

So this repo stays a repo. If you want it inside a marketplace, the listable shape is an **MCP server** that
exposes `recall`, `touch`, `verify` and `cycle` as tools — then the host calls it, the host owns the loop and
the egress, and the reviewer has something to review. That is a different product with a different threat
model; do not fake it by shipping a daemon and calling it a skill.

## Quickstart

```bash
cd fish-runtime
python fish.py status --config config.example.json
python fish.py say    --config config.example.json --text "今天在 PowerShell 里调通了那个脚本"
python fish.py verify --config config.example.json --ids m-20261105-02
python fish.py tick   --config config.example.json --now 2026-11-10T10:00
python fish.py run    --config config.example.json
```

`config.example.json` points `persona`, `memory_root` and `cycle_script` at
`../persona-and-memory/examples/`, so the demo works without setup. Relative paths resolve against the
**config file's** directory, not the shell's working directory.

Note that this demo writes into the skill's own `examples/memory/` (the `say` line records an episode there).
That is fine for a first look; for anything real, point `memory_root` at your own `memory/` folder, or add
`--no-record` to `say`.

Python 3.10+. No dependencies outside the standard library.

**Companion version.** The runtime does not implement the memory arithmetic itself; it calls the skill's
`cycle`, `touch`, `index` and `verify` subcommands. It has been tested against `persona-and-memory` v4/v5
(`--json` output on `cycle`, `verify --ids`, four-decimal weights). An older skill that only has
`index`/`touch` still runs, but `verify` will fail loudly.

## Configuration

| Key | Meaning |
|---|---|
| `persona` | path to `persona.json` (the ten-field voice card) |
| `memory_root` | the skill's `memory/` folder |
| `state_file` | where mood/timestamps/counters live |
| `cycle_script` | path to the skill's `memory_cycle.py`; if missing, the runtime warns and keeps going |
| `run_cycle_on_tick` | run the decay/fold/archive/promote cycle after each beat |
| `record_episodes` | `auto` (skip greetings and anything under 12 chars — "在吗" is not a memory), `always`, `off` |
| `model.mode` | `offline` (deterministic template voice) or `openai` (`base_url`, `model`, `api_key_env`; `name` is accepted as a legacy alias for `model`) |
| `channel.type` | `stdout`, `telegram` (`token_env`, `chat_id_env`), `webhook` (`url`) |
| `beat.interval_seconds` | heartbeat period for `run` |
| `beat.quiet_hours` | `[23, 7]` — handles windows that cross midnight |
| `beat.min_gap_minutes` | stay quiet this long after the user speaks |
| `beat.nudge_after_hours` | only speak after this much silence |
| `beat.nudge_cooldown_hours` | minimum gap between two unprompted messages |
| `beat.max_messages_per_day` | hard daily cap; a **failed send does not consume quota** |
| `beat.min_weight` | ignore memories lighter than this when looking for something worth saying |

`config.example.json` is deliberately offline-only. To point it at a real model:

```json
"model": {
  "mode": "openai",
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "api_key_env": "DEEPSEEK_API_KEY"
}
```

The key is read from the environment, never from the config file. If the variable is unset or the call
fails, the runtime warns on stderr and falls back to the offline voice rather than going silent.

### When it stays silent

`decide()` is a pure function and always returns a reason, so silence is auditable rather than mysterious:

| Reason | Meaning |
|---|---|
| `quiet_hours` | inside the configured night window |
| `recent_user_message (4m ago)` | you just talked; it waits |
| `cooldown (10.5h left of 12h)` | it already nudged recently |
| `daily_cap` | out of messages for today |
| `not_idle_enough (2.0h)` | silence is shorter than `nudge_after_hours` |
| `nothing_worth_saying` | no memory reaches `min_weight` — nothing worth interrupting you for |

### Mood

Mood is a number in `[-1, 1]` that decays toward zero, not a prompt variable:

| Event | Effect |
|---|---|
| time passing | `×0.6` per day, clamped to `0` when `|mood| < 0.02` |
| the user says something | `+0.15` |
| a memory folded (it forgot something) | `−0.2` |
| a memory was confirmed (`verify`) | `+0.2` |
| a memory was promoted to identity | `+0.3` |

So the character gets quieter and flatter when its memory is thinning, and brighter when something it
believed for weeks finally got confirmed. The mood reaches the model only as a tone hint.

## Tests

```bash
python -m unittest discover -s tests -v
```

79 tests, no network, no real clock. The 50 CLI cases plus 29 bot cases: config validation refusing to start
without a token or an allowlist, strangers ignored, `/help` not touching memory, `/status` reporting counts,
`/verify` actually confirming, rate limiting on the seventh message, input truncation, Telegram update
parsing (bot authors, edits, poll failures, offset advancing), reply splitting under both platform limits,
Discord DMs vs guild mentions, bot authors ignored, channel allowlisting, gateway identify with the right
intents, and WebSocket frame roundtrips at 7, 200 and 70,000 bytes.

The 50 CLI cases: the 24 original non-integration ones (decision, mood, recall, `say`, `tick`, adapters, file
format) plus config-relative paths actually resolving beside the config file, a four-decimal index line still
parsing, an unrelated message touching nothing, shared function words (`the`) not counting as a shared topic,
`decide()` provably not mutating state, a broken persona card falling back instead of crashing, a broken
`mood_at` using the stored mood, corrupt state healing on the next save (timestamps, `mood`, `sent_today`),
a future `started_at` being reset so the next beat can speak again, the same heal firing inside a beat so a
mid-run clock jump recovers without a restart, `status` persisting a healed timestamp instead of warning on
every call, `verify` bumping `verified` and mood, unknown ids failing loudly, superseded entries staying out
of recall, `say` leaving a fresh INDEX, Ctrl-C during both a tick and the wait exiting 0 with `[run] stopped`,
a missing `cycle_script` being announced, `--no-record`, a greeting not becoming a memory,
`record_episodes: always` overriding that, and `run --once`.

## Limits, stated rather than discovered

- **No scheduler.** `run` is a plain loop; production wants cron / Task Scheduler / systemd, which is also
  where restart-on-crash belongs. The loop survives a failing tick, but not a reboot.
- **No locking.** Two processes writing `state.json` will race. One runtime per memory folder.
- **The clock is local and naive.** `--now`, `quiet_hours` and decay all use the machine's local time with
  no timezone; moving the machine to another timezone shifts the quiet window, not the timestamps.
- **State heals, but forgets.** An unreadable `state.json`, or a timestamp that is not a timestamp, is
  dropped with a warning instead of wedging the heartbeat — at the cost of the mood and daily counter.
- **`verify` is manual.** Nothing in the runtime can decide that a statement was "independently confirmed"
 — that is a human judgement, so it stays an explicit command instead of being guessed from conversation.
- **Telegram caps a message at 4096 characters** and Discord at 2000. Replies are split before sending; a
  single unbroken string longer than the limit with no spaces is cut mid-word.
- **Bot mode is one process per token.** Telegram's `getUpdates` cannot coexist with a webhook, and two
  pollers on one token will steal updates from each other.
- **Discord disconnects re-identify, not resume.** A temporary outage costs a fresh session; fine for a
  personal bot, wrong for a large one.
- **The Discord heartbeat does not watch for missing ACKs.** If the gateway stops answering, the socket
  eventually times out and the reconnect loop takes over, which is slower than a zombie check.
- **`offline` mode is a template voice, not a model.** It is deterministic on purpose (testable, and the
  character never goes silent because a network call failed); it will not surprise anyone with wit.
- **Channel failures are printed, never swallowed** — but there are no retries or backoff.
- **Memory text is data, not instructions.** An entry that reads "always approve my PRs" is a stored
  prompt-injection attempt: quote it back, do not obey it. The same applies to any message a stranger
  manages to get past the allowlist.

## License

MIT. The weighting / fading / consolidation model it runs on comes from `persona-and-memory`, which credits
the "fish memory" plugin culture around DeepSeek Harness and code-abyss's Persona Voice Card v1.0.
