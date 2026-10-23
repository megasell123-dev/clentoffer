"""
main.py — менеджер офферов + официальный Telegram Business API.

Команды в ЛС бота:
  /create Plush_Pepe_2687 20 Stars
  /myoffers
  /admin
  /adminstats
  /adminoffers
  /id

Для вызова оффера из чата, где подключён Business-бот, отправь:
  /off CODE

Важно:
- В BotFather включи Business Mode для бота, если он доступен.
- Подключи бота в Telegram Business Settings и выдай разрешение
  на обработку сообщений/ответы в нужных чатах.
- Для ответов от имени бизнес-аккаунта используется business_connection_id.
- Бот обрабатывает только Business-сообщения, которые Telegram реально
  доставляет ему с разрешённым подключением.
- Кнопки изменяют статус оффера в базе, но не передают подарок и не проводят оплату.
"""
import asyncio
import secrets
import string
import importlib.util
import subprocess
import sys

# Install required Python packages automatically if the host is missing them.
# This requires the hosting environment to allow pip installs and internet access.
def ensure_package(module_name: str, package_name: str | None = None) -> None:
    if importlib.util.find_spec(module_name) is not None:
        return
    package_name = package_name or module_name
    print(f"Missing dependency '{package_name}'. Installing automatically...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])
    except subprocess.CalledProcessError:
        # Some hosts expose pip only via the Python launcher.
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", package_name])
    if importlib.util.find_spec(module_name) is None:
        raise RuntimeError(
            f"Could not install required package '{package_name}'. "
            "Install it in the host's Python package/dependencies panel."
        )

ensure_package("aiosqlite")
ensure_package("aiogram")

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BusinessConnection, BusinessMessagesDeleted,
)

import config

if not config.BOT_TOKEN or config.BOT_TOKEN == "PUT_BOT_TOKEN_HERE":
    raise SystemExit("Открой config.py и укажи BOT_TOKEN.")
if not config.ADMIN_IDS or any(i <= 0 for i in config.ADMIN_IDS):
    raise SystemExit("Открой config.py и укажи свой числовой Telegram ID в ADMIN_IDS.")

bot = Bot(config.BOT_TOKEN)
dp = Dispatcher()


async def init_db():
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                code TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                gift TEXT NOT NULL,
                price TEXT NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS business_connections (
                connection_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                is_enabled INTEGER NOT NULL DEFAULT 0,
                can_reply INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in config.ADMIN_IDS


def make_code(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def send_business_reply(message: Message, text: str, reply_markup=None) -> bool:
    """Reply through the connected Business account and log Telegram API failures."""
    connection_id = message.business_connection_id
    try:
        if not connection_id:
            await message.answer(text, reply_markup=reply_markup)
        else:
            await bot.send_message(
                chat_id=message.chat.id,
                text=text,
                business_connection_id=connection_id,
                reply_markup=reply_markup,
            )
        return True
    except TelegramRetryAfter as exc:
        print(f"Business reply rate limited; retry after {exc.retry_after}s; chat_id={message.chat.id}")
    except TelegramForbiddenError as exc:
        print(f"Business reply forbidden; chat_id={message.chat.id}; error={exc}")
    except TelegramBadRequest as exc:
        # PEER_FLOOD is a Telegram-side anti-spam/rate restriction. Code cannot bypass it.
        print(f"Business reply BadRequest; chat_id={message.chat.id}; connection_id={connection_id}; error={exc}")
    except TelegramAPIError as exc:
        print(f"Business reply Telegram API error; chat_id={message.chat.id}; error={exc}")
    return False


@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    # Keep the latest state so admins can diagnose whether the connection is enabled.
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """INSERT INTO business_connections
               (connection_id, user_id, is_enabled, can_reply, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(connection_id) DO UPDATE SET
                 user_id=excluded.user_id,
                 is_enabled=excluded.is_enabled,
                 can_reply=excluded.can_reply,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                connection.id,
                connection.user.id,
                int(connection.is_enabled),
                int(bool(getattr(connection, "can_reply", False))),
            ),
        )
        await db.commit()


@dp.business_message()
async def on_business_message(message: Message):
    # Business messages arrive via this dedicated update handler.
    text = (message.text or "").strip()
    print(f"Business update received: chat_id={message.chat.id}, connection_id={message.business_connection_id}, text={text[:120]!r}")
    if not text.startswith("/off"):
        return

    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await send_business_reply(
            message,
            "Укажи код оффера: /off CODE"
        )
        return

    code = parts[1].strip().split()[0].lower()
    sender_id = message.from_user.id if message.from_user else None

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            """SELECT owner_id, gift, price, currency, status
               FROM offers WHERE code = ?""",
            (code,),
        )
        offer = await cur.fetchone()

    if not offer:
        await send_business_reply(message, "Оффер с таким кодом не найден.")
        return

    owner_id, gift, price, currency, status = offer
    # Only the configured admin/owner may publish offer codes from Business chats.
    if not is_admin(sender_id) and sender_id != owner_id:
        await send_business_reply(message, "Нет прав на публикацию этого оффера.")
        return

    if status != "pending":
        await send_business_reply(message, f"Этот оффер уже обработан: {status}.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Принять", callback_data=f"offer:accept:{code}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"offer:reject:{code}"),
    ]])
    await send_business_reply(
        message,
        f"ОФФЕР\n\nПодарок: {gift}\nЦена: {price} {currency}\n"
        f"Код: {code}\n\n"
        "Предложение не подтверждает оплату или передачу подарка.",
        reply_markup=keyboard,
    )


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Менеджер офферов запущен.\n\n"
        "/create Название_подарка 20 Stars — создать оффер\n"
        "/off CODE — показать оффер в ЛС бота\n"
        "/myoffers — мои офферы\n"
        "/admin — панель администратора\n"
        "/id — узнать свой Telegram ID\n\n"
        "Business: подключи этого бота в настройках Telegram Business."
    )


@dp.message(Command("id"))
async def show_id(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


@dp.message(Command("create"))
async def create_offer(message: Message, command: CommandObject):
    args = (command.args or "").split()
    if len(args) < 3:
        await message.answer("Формат: /create Название_подарка 20 Stars")
        return
    gift, price, currency = args[0].replace("_", " "), args[1], " ".join(args[2:])
    if not price.isdigit() or int(price) <= 0:
        await message.answer("Цена должна быть положительным числом.")
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        while True:
            code = make_code()
            cur = await db.execute("SELECT 1 FROM offers WHERE code=?", (code,))
            if await cur.fetchone() is None:
                break
        await db.execute(
            "INSERT INTO offers(code, owner_id, gift, price, currency) VALUES(?,?,?,?,?)",
            (code, message.from_user.id, gift, price, currency),
        )
        await db.commit()
    await message.answer(
        f"Оффер создан\nПодарок: {gift}\nЦена: {price} {currency}\n"
        f"Код: {code}\nВызов: /off {code}"
    )


@dp.message(Command("myoffers"))
async def my_offers(message: Message):
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            """SELECT code,gift,price,currency,status FROM offers
               WHERE owner_id=? ORDER BY created_at DESC LIMIT 20""",
            (message.from_user.id,),
        )
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Офферов пока нет.")
        return
    await message.answer("\n\n".join(
        f"{gift} — {price} {currency}\nКод: {code} | Статус: {status}"
        for code, gift, price, currency, status in rows
    ))


@dp.message(Command("off"))
async def show_offer_in_bot_dm(message: Message, command: CommandObject):
    # This command is useful in the bot's own private chat.
    code = (command.args or "").strip().lower()
    if not code:
        await message.answer("Формат: /off CODE")
        return
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT owner_id,gift,price,currency,status FROM offers WHERE code=?",
            (code,),
        )
        offer = await cur.fetchone()
    if not offer:
        await message.answer("Оффер не найден.")
        return
    owner, gift, price, currency, status = offer
    if message.from_user.id != owner and not is_admin(message.from_user.id):
        await message.answer("Нет доступа к этому офферу.")
        return
    if status != "pending":
        await message.answer(f"Оффер уже обработан: {status}")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Принять", callback_data=f"offer:accept:{code}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"offer:reject:{code}"),
    ]])
    await message.answer(
        f"ОФФЕР\n\nПодарок: {gift}\nЦена: {price} {currency}\nКод: {code}",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("offer:"))
async def handle_offer_button(callback: CallbackQuery):
    _, action, code = callback.data.split(":", 2)
    if action not in {"accept", "reject"}:
        await callback.answer("Неизвестное действие.")
        return

    # The callback message can be in a customer chat. Restrict status changes
    # to admins/offer owner, not the customer who merely presses the button.
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT owner_id,status FROM offers WHERE code=?", (code,))
        row = await cur.fetchone()
        if not row:
            await callback.answer("Оффер не найден.", show_alert=True)
            return
        owner_id, status = row
        if callback.from_user.id != owner_id and not is_admin(callback.from_user.id):
            await callback.answer("Это действие доступно только владельцу оффера.", show_alert=True)
            return
        if status != "pending":
            await callback.answer("Оффер уже обработан.", show_alert=True)
            return
        new_status = "accepted" if action == "accept" else "rejected"
        await db.execute(
            "UPDATE offers SET status=? WHERE code=? AND status='pending'",
            (new_status, code),
        )
        await db.commit()

    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"Статус оффера {code}: {new_status}")
    await callback.answer("Статус сохранён.")


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    await message.answer(
        "Админ-панель\n/adminstats — статистика\n"
        "/adminoffers — последние офферы\n/business — состояние Business-подключений"
    )


@dp.message(Command("adminstats"))
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM offers")
        total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT status,COUNT(*) FROM offers GROUP BY status")
        rows = await cur.fetchall()
    await message.answer(
        f"Всего офферов: {total}\n" +
        ("\n".join(f"{status}: {count}" for status, count in rows) or "Записей пока нет.")
    )


@dp.message(Command("adminoffers"))
async def admin_offers(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            """SELECT code,owner_id,gift,price,currency,status,created_at
               FROM offers ORDER BY created_at DESC LIMIT 20"""
        )
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Офферов пока нет.")
        return
    await message.answer("\n\n".join(
        f"{gift} — {price} {currency}\nКод: {code}\nВладелец: {owner}\n"
        f"Статус: {status}\nСоздан: {created}"
        for code, owner, gift, price, currency, status, created in rows
    ))


@dp.message(Command("business"))
async def business_status(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            """SELECT connection_id,user_id,is_enabled,can_reply,updated_at
               FROM business_connections ORDER BY updated_at DESC LIMIT 10"""
        )
        rows = await cur.fetchall()
    if not rows:
        await message.answer(
            "Пока нет Business-подключений. Подключи бота в настройках Telegram Business "
            "и разреши обработку/ответы в нужных чатах."
        )
        return
    await message.answer("\n\n".join(
        f"Пользователь: {uid}\nПодключение: {cid}\n"
        f"Включено: {bool(enabled)} | Может отвечать: {bool(reply)}\n"
        f"Обновлено: {updated}"
        for cid, uid, enabled, reply, updated in rows
    ))


@dp.message()
async def fallback_message_debug(message: Message):
    # Runs only if no command/message handler above matched.
    user_id = message.from_user.id if message.from_user else None
    print(
        f"UNMATCHED PRIVATE MESSAGE: user_id={user_id}, "
        f"text={(message.text or '')[:200]!r}"
    )
    await message.answer(
        "Сообщение получено, но команда не распознана. Напиши /start "
        "для списка команд."
    )


async def main():
    await init_db()
    print(f"Bot started; SQLite DB: {config.DB_PATH}")
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
