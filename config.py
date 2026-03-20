
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_user_id: int
    timezone: str = "Europe/Moscow"

def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing")
    admin = os.getenv("ADMIN_USER_ID","").strip()
    if not admin or not admin.lstrip("-").isdigit():
        raise RuntimeError("ADMIN_USER_ID is missing or invalid")
    tz = os.getenv("TIMEZONE","Europe/Moscow").strip() or "Europe/Moscow"
    return Config(bot_token=token, admin_user_id=int(admin), timezone=tz)
