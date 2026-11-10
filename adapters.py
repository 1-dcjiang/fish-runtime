"""Adapters: model (offline stub or OpenAI-compatible) and channels (stdout, telegram, webhook).

Stdlib only. No network calls unless a real channel/model is configured, which is
what makes the whole runtime testable offline.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

MOOD_BUCKETS = {
    "bright": ["在。", "我记着呢，", "刚想起一件事——"],
    "steady": ["嗯，", "说一句吧：", "有条旧的记忆浮上来了："],
    "low": ["……", "不太想说话，但：", "我有点想不起来了，只剩下这句："],
}


def mood_bucket(mood: float) -> str:
    if mood > 0.5:
        return "bright"
    if mood < -0.5:
        return "low"
    return "steady"


def is_chinese(language: str) -> bool:
    return any(word in (language or "").lower() for word in ("chinese", "中文", "汉语", "zh"))


def offline_reply(persona: dict, ref: str, mood: float, proactive: bool, seed: int = 0) -> str:
    """Deterministic voice. No model, no network — used for tests and for --offline runs."""
    zh = is_chinese(persona.get("language", ""))
    bucket = mood_bucket(mood)
    lead = MOOD_BUCKETS[bucket][seed % len(MOOD_BUCKETS[bucket])]
    name_self = persona.get("self", "I")
    user = persona.get("user", "you")
    flourish = (persona.get("flourish") or [""])[0]

    if zh:
        if proactive:
            body = f"{user}，{lead}{ref}"
        else:
            body = f"{lead}{ref}"
    else:
        if proactive:
            body = f"{user} — {lead} {ref}"
        else:
            body = f"{lead} {ref}"
    if flourish and bucket == "bright":
        body = f"{body} {flourish}"
    return f"{body}（{name_self}）" if zh else f"{body} — {name_self}"


class ModelClient:
    """OpenAI-compatible chat completions, or a deterministic offline voice."""

    def __init__(self, config: dict, persona: dict):
        self.config = config or {}
        self.persona = persona
        self.mode = self.config.get("mode", "offline")
        self.base_url = self.config.get("base_url", "https://api.openai.com/v1").rstrip("/")
        # `model` is the documented key; `name` is accepted because the first cut used it
        self.model = self.config.get("model") or self.config.get("name") or "gpt-4o-mini"
        self.timeout = int(self.config.get("timeout", 30))

    def compose(self, ref: str, mood: float, proactive: bool, memory_lines: list[str],
                user_text: str = "") -> str:
        if self.mode != "openai":
            return offline_reply(self.persona, ref, mood, proactive)
        key = os.environ.get(self.config.get("api_key_env", "OPENAI_API_KEY"))
        if not key:
            print(f"warning: {self.config.get('api_key_env', 'OPENAI_API_KEY')} is unset; "
                  "using the offline voice", file=sys.stderr)
            return offline_reply(self.persona, ref, mood, proactive)
        prompt = self._prompt(memory_lines, user_text, proactive)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system()},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 120,
            "temperature": 0.7,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            # never let a network failure silence the character — but say that it happened
            print(f"warning: model call failed ({exc}); using the offline voice", file=sys.stderr)
            return offline_reply(self.persona, ref, mood, proactive)

    def _system(self) -> str:
        card = self.persona
        lines = [
            f"You are {card.get('name', 'the character')}.",
            f"You refer to yourself as {card.get('self', 'I')} and to the user as {card.get('user', 'you')}.",
            f"Language policy: {card.get('language', 'match the user')}.",
            f"Register: {card.get('register', 'casual')}. Emoji: {card.get('emoji_policy', 'none')}.",
            f"How you decline: {card.get('refusal_style', 'plainly')}.",
            "Voice only: you never invent facts or memories. Only the memory lines given are real.",
        ]
        flourish = card.get("flourish") or []
        if flourish:
            lines.append("Phrases you may reuse verbatim: " + "; ".join(flourish[:5]))
        return "\n".join(lines)

    def _prompt(self, memory_lines: list[str], user_text: str, proactive: bool) -> str:
        head = "The user has been silent for a while. Write 1-2 short sentences that start the conversation." \
            if proactive else "Reply to the user's message."
        memories = "\n".join(memory_lines) if memory_lines else "(empty)"
        return (
            f"{head}\n\nMemory index (the only real memories you have):\n{memories}\n\n"
            f"User message: {user_text or '(none)'}\n\n"
            "Use at most one memory, do not list them, do not say 'according to my memory'. "
            "If nothing fits, just greet in voice."
        )


class Channel:
    """Where the character speaks. stdout for tests, telegram/webhook for real life."""

    def __init__(self, config: dict):
        self.config = config or {}
        self.type = self.config.get("type", "stdout")

    def send(self, text: str) -> dict:
        if self.type == "stdout":
            print(text)
            return {"channel": "stdout", "ok": True}
        if self.type == "telegram":
            token = os.environ.get(self.config.get("token_env", "FISH_TG_TOKEN"))
            chat_id = os.environ.get(self.config.get("chat_id_env", "FISH_TG_CHAT"))
            if not token or not chat_id:
                # Do not echo the message body into logs: it may be the user's private text.
                print(f"[channel] telegram not configured ({self.config.get('token_env')}/"
                      f"{self.config.get('chat_id_env')} unset); 1 message dropped",
                      file=sys.stderr)
                return {"channel": "telegram", "ok": False, "error": "missing token or chat id"}
            return self._post_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat_id, "text": text},
                "telegram",
            )
        if self.type == "webhook":
            url = self.config.get("url")
            if not url:
                return {"channel": "webhook", "ok": False, "error": "no url"}
            return self._post_json(url, {"text": text, "source": "fish-runtime"}, "webhook")
        return {"channel": self.type, "ok": False, "error": f"unknown channel {self.type}"}

    @staticmethod
    def _post_json(url: str, payload: dict, name: str) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return {"channel": name, "ok": 200 <= response.status < 300}
        except Exception as exc:
            return {"channel": name, "ok": False, "error": str(exc)}
