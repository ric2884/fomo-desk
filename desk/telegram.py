"""Telegram notifications + owner commands. Without a token, messages are printed to the console only.

The token goes in a file called `.env` in the project folder:   TELEGRAM_BOT_TOKEN=123456:ABC...
The first person to send /start to the bot becomes the owner; everyone else is ignored."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from .config import ROOT

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot{}/{}"


def read_env(path: Path = ROOT / ".env") -> dict[str, str]:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Telegram:
    def __init__(self, kv_get, kv_set):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN") or read_env().get("TELEGRAM_BOT_TOKEN")
        self.kv_get, self.kv_set = kv_get, kv_set
        self.client = httpx.AsyncClient(timeout=40) if self.token else None

    @property
    def owner(self) -> int | None:
        v = self.kv_get("owner_chat_id")
        return int(v) if v else None

    async def send(self, text: str) -> None:
        print(f"[telegram] {text}")
        if not self.client or not self.owner:
            return
        try:
            await self.client.post(API.format(self.token, "sendMessage"),
                                   json={"chat_id": self.owner, "text": text, "disable_web_page_preview": True})
        except Exception as e:  # noqa: BLE001 — never let a notification crash the desk
            log.warning("telegram send failed: %r", e)

    async def poll(self) -> list[str]:
        """Long-poll for commands from the owner. Returns command words like '/status'."""
        if not self.client:
            return []
        offset = int(self.kv_get("tg_offset") or 0)
        try:
            r = await self.client.get(API.format(self.token, "getUpdates"),
                                      params={"timeout": 25, "offset": offset})
            updates = r.json().get("result", [])
        except Exception as e:  # noqa: BLE001
            log.warning("telegram poll failed: %r", e)
            return []
        cmds = []
        for u in updates:
            self.kv_set("tg_offset", str(u["update_id"] + 1))
            msg = u.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            text = (msg.get("text") or "").strip().split()
            if not chat or not text:
                continue
            cmd = text[0].split("@")[0].lower()
            if self.owner is None and cmd == "/start":
                self.kv_set("owner_chat_id", str(chat))
                await self.send("✅ You are now the owner of this desk. Commands: /status /report /majors /pause /resume")
                continue
            if chat == self.owner:
                cmds.append(cmd)
        return cmds
