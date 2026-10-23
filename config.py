"""
config.py — настройки Telegram Business Offer Bot.

Заполни BOT_TOKEN и ADMIN_IDS перед запуском.
ADMIN_IDS — числовые Telegram ID через запятую, например: "123456789,987654321".
"""
import os
import sys
import subprocess

def ensure_dependencies():
    requirements = {
        "aiogram": "aiogram>=3.7,<4",
        "aiosqlite": "aiosqlite>=0.20,<1",
    }
    missing = []
    for module, package in requirements.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])

# Dependencies are installed automatically when main.py imports this config.
ensure_dependencies()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8993123451:AAHY8-m5S8NDE9f2ZvN27KxbA4jIP9wHXbg").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "8493536750").split(",")
    if value.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "offers.db")
