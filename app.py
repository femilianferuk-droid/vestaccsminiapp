"""
Vest Account Mini App — single-file Flask-приложение.

Один файл, который содержит:
  - Flask-бэк с валидацией Telegram initData (HMAC-SHA256)
  - REST API для каталога / категорий / профиля / баланса
  - HTML-шаблон мини-аппа со встроенными CSS и JS
  - Модели SQLAlchemy, идентичные схеме bot.py (ОБЩАЯ БД)

Дизайн:
  - Профиль сделан отдельной полноценной СТРАНИЦЕЙ (не модалкой)
  - Есть страница пополнения баланса с тремя способами
    (СБП / Crypto / Промокод — активируется прямо здесь)
  - Баланс берётся из общей таблицы users.balance и авто-обновляется
    при возвращении юзера из бота и при переключении вкладок

Запуск:
    pip install -r requirements.txt
    cp .env.example .env   # заполни BOT_TOKEN и DATABASE_URL
    python app.py
"""
import os
import hmac
import hashlib
import json
import re
import io
import asyncio
import urllib.request
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from functools import wraps
from typing import Optional

from flask import (
    Flask, request, jsonify, render_template_string, abort, send_file
)
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text,
    create_engine, select,
)
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv

# Telethon — для получения кода подтверждения и выдачи сессии.
# Те же api_id / api_hash, что и в боте (ОБЩАЯ сессия, общий ключ).
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

# ===== КОНФИГ =====
# Telethon API — ТЕ ЖЕ значения, что и в bot.py (vestaccpunt),
# чтобы сессия из бота валидировалась в мини-аппе без расхождений.
API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8608742695:AAGlbLTlGniqZvwl9nE6IJBzj4UboWXN03A",
)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://bothost_db_d9dbd53f40eb:pa0bg7BK4-HmRor5Fpn3X58gh8kB_0a2OJMIle5kFSQ@node1.pghost.ru:15818/bothost_db_d9dbd53f40eb",
)
if "+asyncpg" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("+asyncpg", "")
SECRET_KEY = os.getenv("FLASK_SECRET", "change-me-in-prod")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in .env")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["JSON_AS_ASCII"] = False

# ===== МОДЕЛИ (зеркалят схему из bot.py — ОБЩАЯ БД) =====
Base = declarative_base()


class User(Base):
    # ⚠️ Схема СТРОГО совпадает с bot.py — никаких лишних колонок,
    # иначе UPDATE при апсерте упадёт с "column does not exist".
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    balance = Column(Float, default=0.0)
    hold_balance = Column(Float, default=0.0)
    total_spent = Column(Float, default=0.0)
    total_earned = Column(Float, default=0.0)
    is_admin = Column(Boolean, default=False)
    rating = Column(Float, default=5.0)
    reviews_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, nullable=False)
    country = Column(String(50), default="США")
    session_string = Column(Text, nullable=True)
    session_json = Column(Text, nullable=True)
    is_sold = Column(Boolean, default=False)
    is_verified = Column(Boolean, default=False)
    price = Column(Float, default=20.0)
    origin = Column(String(50), nullable=True)
    seller_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Listing(Base):
    __tablename__ = "listings"
    id = Column(Integer, primary_key=True)
    seller_id = Column(BigInteger, nullable=False)
    account_id = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, default="")
    price = Column(Float, nullable=False)
    origin = Column(String(50), nullable=True)
    country = Column(String(50), nullable=True)
    status = Column(String(20), default="active")
    buyer_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    sold_at = Column(DateTime, nullable=True)


class PriceSettings(Base):
    __tablename__ = "price_settings"
    id = Column(Integer, primary_key=True)
    country = Column(String(50), unique=True, nullable=False)
    price = Column(Float, default=20.0)
    updated_at = Column(DateTime, default=datetime.utcnow)


class PromoCode(Base):
    """Промокоды (общая таблица с bot.py)."""
    __tablename__ = "promo_codes"
    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False)
    amount = Column(Float, default=0.0)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PromoUsage(Base):
    """Использование промокодов (общая таблица с bot.py).

    ⚠️ Имя колонки ОБЯЗАТЕЛЬНО created_at (а не used_at) — бот уже
    создал таблицу с этим именем, и любые INSERT с used_at падают с
    "column does not exist", из-за чего фронт видит BAD JSON.
    """
    __tablename__ = "promo_usages"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    promo_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Payment(Base):
    """Платежи (общая таблица) — для истории пополнений в профиле."""
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    amount = Column(Float, nullable=False)
    payment_id = Column(String(255), unique=True)
    status = Column(String(50), default="pending")
    method = Column(String(50))
    type = Column(String(50), default="deposit")
    screenshot_file_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Purchase(Base):
    """Покупка аккаунта (зеркало схемы из bot.py — ОБЩАЯ БД).

    ⚠️ Имена колонок СТРОГО как в bot.py:
      user_id (telegram_id покупателя), account_id, listing_id,
      amount, payment_method, created_at. Никаких отсебятин.
    """
    __tablename__ = "purchases"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    account_id = Column(Integer, nullable=False)
    listing_id = Column(Integer, nullable=True)
    amount = Column(Float, nullable=False)
    payment_method = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)


# ===== DB =====
try:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    with engine.connect() as _c:
        pass
except Exception:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))


@app.teardown_appcontext
def remove_session(exc=None):
    SessionLocal.remove()


# ===== СПРАВОЧНИКИ =====
COUNTRY_FLAGS = {
    "США": "🇺🇸", "Россия": "🇷🇺", "Индия": "🇮🇳", "Германия": "🇩🇪",
    "Бразилия": "🇧🇷", "Индонезия": "🇮🇩", "Казахстан": "🇰🇿", "Украина": "🇺🇦",
    "Беларусь": "🇧🇾", "Вьетнам": "🇻🇳", "Филиппины": "🇵🇭", "Мьянма": "🇲🇲",
    "Мексика": "🇲🇽", "Турция": "🇹🇷", "Польша": "🇵🇱", "Великобритания": "🇬🇧",
    "Аргентина": "🇦🇷",
}
ORIGIN_LABELS = {
    "Авторег": ("🤖", "Авторег"),
    "Саморег": ("👤", "Саморег"),
    "Фишинг": ("🎣", "Фишинг"),
    "Стиллер": ("🕵️", "Стиллер"),
}

# Кеш для bot username (определяется через getMe один раз)
_BOT_USERNAME_CACHE = {"username": None, "ts": 0}
_BOT_INFO_TTL = 60 * 60  # час


def mask_phone(phone: str) -> str:
    """Маскирует номер, оставляя последние 4 цифры."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) <= 4:
        return digits
    return "+" + "*" * (len(digits) - 4) + digits[-4:]


def get_bot_username() -> str | None:
    """Возвращает username бота через Telegram getMe API. Кешируется на час."""
    now = datetime.now().timestamp()
    if _BOT_USERNAME_CACHE["username"] and (now - _BOT_USERNAME_CACHE["ts"]) < _BOT_INFO_TTL:
        return _BOT_USERNAME_CACHE["username"]
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
        with urllib.request.urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok") and data.get("result", {}).get("username"):
            _BOT_USERNAME_CACHE["username"] = data["result"]["username"]
            _BOT_USERNAME_CACHE["ts"] = now
            return _BOT_USERNAME_CACHE["username"]
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        pass
    return _BOT_USERNAME_CACHE["username"]  # может быть None


# ===== TELEGRAM INITDATA VALIDATION =====
def validate_init_data(init_data: str):
    """
    Проверяет подпись initData по алгоритму Telegram:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
        data = dict(pairs)
        received_hash = data.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        result = dict(data)
        if "user" in result:
            try:
                result["user_obj"] = json.loads(result["user"])
            except (ValueError, TypeError):
                result["user_obj"] = None
        return result
    except Exception:
        return None


def require_auth(f):
    """Декоратор: проверяет initData, кладёт telegram_id в kwargs.

    initData берём по очереди из:
      1) заголовка X-Init-Data
      2) тела JSON-запроса
      3) query-параметра ?initData=...  ← для скачивания файлов через <a download>,
         потому что у обычной ссылки нельзя задать кастомный заголовок
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        init_data = (
            request.headers.get("X-Init-Data", "")
            or (request.get_json(silent=True) or {}).get("initData", "")
            or request.args.get("initData", "")
        )
        validated = validate_init_data(init_data)
        if not validated or not validated.get("user_obj"):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        kwargs["telegram_id"] = validated["user_obj"]["id"]
        kwargs["tg_user"] = validated["user_obj"]
        return f(*args, **kwargs)
    return wrapper


# ===== TELETHON ФУНКЦИИ (для «Мои покупки») =====
#
# Логика идентична боту: открываем сессию через ТЕ ЖЕ API_ID/API_HASH,
# читаем свежие сообщения из диалогов и ищем 5-значный код подтверждения.
#
# Flask — синхронный, Telethon — асинхронный. Поэтому крутим event loop
# через asyncio.run() прямо в endpoint'е. Поиск кода обычно ≤15 сек,
# воркер Gunicorn на это время блокируется — для операции получения
# кода это приемлемо (так же делал бы и обычный sync Telethon).


CODE_KEYWORDS = [
    "telegram", "код", "code", "login", "verify", "подтверждени",
    "авторизаци", "вход", "42777", "служебны", "service",
    "верификаци", "verification",
]


async def _get_code_from_session_async(session_string: str, phone: str = None) -> Optional[str]:
    """Асинхронный поиск кода подтверждения — копия логики из bot.py."""
    client = None
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            return None

        # Принудительно обновляем диалоги (свежие сообщения с сервера)
        try:
            await client.get_dialogs(limit=1)
        except Exception:
            pass

        all_codes = []
        async for dialog in client.iter_dialogs(limit=100):
            dialog_name = (dialog.name or "").lower()
            is_service = any(kw in dialog_name for kw in CODE_KEYWORDS)
            msg_limit = 50 if is_service else 10
            try:
                messages = await client.get_messages(dialog, limit=msg_limit)
                for msg in messages:
                    if not getattr(msg, "text", None):
                        continue
                    codes_5 = re.findall(r'(?<!\d)\d{5}(?!\d)', msg.text)
                    codes_login = re.findall(
                        r'(?:login|code|код)\s*(?:code|код|:)?\s*(\d{5})',
                        msg.text.lower(),
                    )
                    codes_is = re.findall(r'(\d{5})\s*is\s*your', msg.text.lower())
                    for code in codes_5 + codes_login + codes_is:
                        code_str = str(code)
                        if len(code_str) == 5 and code_str.isdigit():
                            all_codes.append({
                                "code": code_str,
                                "dialog": dialog.name or "Unknown",
                                "date": msg.date,
                                "is_service": is_service,
                            })
            except Exception:
                continue

        if not all_codes:
            return None

        # Сортируем: служебные первыми, затем — самые свежие.
        all_codes.sort(key=lambda x: (not x["is_service"], x["date"]), reverse=False)
        return all_codes[0]["code"]
    except Exception:
        return None
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


def get_code_from_session(session_string: str, phone: str = None) -> Optional[str]:
    """Sync-обёртка для Flask: запускает async-поиск кода в event loop."""
    try:
        return asyncio.run(_get_code_from_session_async(session_string, phone))
    except Exception:
        return None


# ===== HTML-ШАБЛОН (CSS и JS встроены) =====
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#1d4ed8">
    <title>Vest Account — Маркетплейс аккаунтов</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root {
            /* Палитра — Vest Account: глубокий индиго + бирюза */
            --blue-50:  #eef4ff;
            --blue-100: #d9e6ff;
            --blue-200: #b8d0ff;
            --blue-300: #8db1ff;
            --blue-400: #5d8aff;
            --blue-500: #3b6cf2;
            --blue-600: #2a52d4;
            --blue-700: #1f3eaa;
            --blue-800: #182f80;
            --blue-900: #11215c;
            --indigo-600: #5b3df0;
            --indigo-700: #4a2bd6;
            --violet-500: #8b5cf6;
            --teal-400: #2dd4bf;
            --teal-500: #14b8a6;

            --white:    #ffffff;
            --gray-50:  #f7f9fc;
            --gray-100: #eef2f7;
            --gray-200: #dde4ed;
            --gray-300: #c5cfdc;
            --gray-400: #94a3b8;
            --gray-500: #64748b;
            --gray-700: #334155;
            --gray-900: #0f172a;

            --green-500: #22c55e;
            --green-600: #16a34a;
            --amber-500: #f59e0b;
            --red-500:   #ef4444;
            --purple-500: #a855f7;

            --bg:       #f3f5fb;
            --surface:  var(--white);
            --text:     var(--gray-900);
            --text-muted: var(--gray-500);
            --accent:   var(--indigo-600);
            --accent-2: var(--teal-500);
            --shadow-sm: 0 2px 8px rgba(15, 23, 42, 0.05);
            --shadow:   0 8px 28px rgba(42, 82, 212, 0.10);
            --shadow-lg: 0 18px 48px rgba(42, 82, 212, 0.16);
            --radius:   22px;
            --radius-sm: 14px;
            --radius-lg: 28px;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
        html, body { height: 100%; overflow-x: hidden; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Inter', 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            font-size: 15px;
            line-height: 1.45;
            -webkit-font-smoothing: antialiased;
            padding-bottom: 88px;
            background-image:
                radial-gradient(circle at 0% 0%, rgba(91, 61, 240, 0.10), transparent 45%),
                radial-gradient(circle at 100% 0%, rgba(20, 184, 166, 0.08), transparent 45%),
                radial-gradient(circle at 50% 100%, rgba(42, 82, 212, 0.06), transparent 55%);
            background-attachment: fixed;
        }

        /* ====== ШАПКА ====== */
        .app-header {
            position: sticky; top: 0; z-index: 50;
            background: linear-gradient(135deg, #1f3eaa 0%, #2a52d4 45%, #5b3df0 100%);
            color: var(--white);
            padding: 14px 16px 18px;
            display: flex; align-items: center; gap: 12px;
            box-shadow: 0 10px 30px rgba(31, 62, 170, 0.28);
        }
        .brand-logo {
            width: 40px; height: 40px; border-radius: 12px;
            background: linear-gradient(135deg, #14b8a6, #5b3df0);
            display: flex; align-items: center; justify-content: center;
            font-weight: 800; font-size: 16px; color: var(--white);
            box-shadow: 0 6px 16px rgba(20, 184, 166, 0.4);
            flex-shrink: 0;
            letter-spacing: -0.5px;
        }
        .brand-text { font-weight: 700; font-size: 13px; opacity: 0.85; }
        /* Прячем brand-классы (заглушка на случай если где-то остались) */
        .brand-logo, .brand-text { display: none !important; }
        .app-header::before {
            content: ''; position: absolute; inset: 0;
            background: radial-gradient(circle at 80% 0%, rgba(255,255,255,0.18), transparent 60%);
            pointer-events: none;
        }
        .app-header > * { position: relative; z-index: 1; }
        .avatar {
            width: 44px; height: 44px; border-radius: 50%;
            background: rgba(255, 255, 255, 0.18); overflow: hidden;
            display: flex; align-items: center; justify-content: center;
            flex-shrink: 0; border: 2px solid rgba(255, 255, 255, 0.32);
            backdrop-filter: blur(10px);
        }
        .avatar img { width: 100%; height: 100%; object-fit: cover; }
        .avatar-fallback { font-weight: 600; font-size: 18px; color: var(--white); }
        .user-info { flex: 1; min-width: 0; }
        .user-name {
            font-size: 16px; font-weight: 600;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .user-meta {
            font-size: 12px; opacity: 0.85; margin-top: 2px;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .balance-pill {
            display: inline-flex; align-items: center; gap: 5px;
            background: rgba(255, 255, 255, 0.20);
            border: 1px solid rgba(255, 255, 255, 0.22);
            padding: 8px 14px; border-radius: 20px;
            color: var(--white); font-size: 13px; font-weight: 700;
            cursor: pointer; backdrop-filter: blur(12px);
            transition: all 0.18s;
            font-family: inherit;
        }
        .balance-pill .balance-cur {
            display: inline-flex; align-items: center; justify-content: center;
            width: 20px; height: 20px; border-radius: 50%;
            background: rgba(255, 255, 255, 0.28);
            font-size: 12px; font-weight: 800;
        }
        .balance-pill:active { transform: scale(0.96); background: rgba(255, 255, 255, 0.28); }
        .balance-pill.refreshing { animation: pulse 1s ease-in-out infinite; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.55; }
        }

        /* ====== СТРАНИЦЫ (view switcher) ====== */
        .page { display: none; animation: fadeUp 0.28s cubic-bezier(0.32, 0.72, 0, 1); }
        .page.active { display: block; }
        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* ====== HERO ====== */
        .hero { padding: 30px 20px 8px; }
        .hero-brand {
            display: inline-flex; align-items: center; gap: 8px;
            background: rgba(91, 61, 240, 0.08);
            border: 1px solid rgba(91, 61, 240, 0.18);
            color: var(--indigo-700);
            padding: 6px 12px; border-radius: 20px;
            font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
            text-transform: uppercase; margin-bottom: 12px;
        }
        .hero-brand .dot {
            width: 6px; height: 6px; border-radius: 50%;
            background: var(--teal-500);
            box-shadow: 0 0 0 4px rgba(20, 184, 166, 0.25);
        }
        .hero-title {
            font-size: 32px; font-weight: 800;
            color: var(--blue-900); letter-spacing: -0.8px; line-height: 1.05;
        }
        .hero-title span {
            background: linear-gradient(135deg, var(--blue-600), var(--indigo-600), var(--violet-500));
            -webkit-background-clip: text; background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .hero-sub {
            font-size: 14px; color: var(--text-muted); margin-top: 10px;
            max-width: 360px;
        }

        /* ====== СЕКЦИИ ====== */
        .section { padding: 16px 16px 6px; }
        .section-head {
            display: flex; align-items: baseline; justify-content: space-between;
            margin-bottom: 12px; padding: 0 4px;
        }
        .section-title { font-size: 17px; font-weight: 700; color: var(--gray-900); }
        .section-count {
            font-size: 12px; color: var(--blue-700);
            background: var(--blue-100); padding: 3px 10px;
            border-radius: 20px; font-weight: 700;
        }

        /* ====== Пиллы ====== */
        .cat-scroll {
            display: flex; gap: 8px; overflow-x: auto;
            padding: 4px 0 10px;
            scrollbar-width: none; -ms-overflow-style: none;
        }
        .cat-scroll::-webkit-scrollbar { display: none; }
        .cat-pill {
            flex-shrink: 0;
            background: var(--surface);
            border: 1.5px solid var(--gray-200);
            color: var(--gray-700);
            padding: 9px 14px; border-radius: 14px;
            font-size: 13px; font-weight: 600;
            cursor: pointer; transition: all 0.18s;
            display: inline-flex; align-items: center; gap: 6px;
            white-space: nowrap; font-family: inherit;
            box-shadow: var(--shadow-sm);
        }
        .cat-pill:active { transform: scale(0.96); }
        .cat-pill.active {
            background: linear-gradient(135deg, var(--blue-600), var(--blue-700));
            border-color: transparent;
            color: var(--white);
            box-shadow: 0 6px 18px rgba(37, 99, 235, 0.38);
        }
        .cat-emoji { font-size: 15px; }

        /* ====== Фильтр-бар (вместо двух scroll'ов) ====== */
        .filter-bar {
            display: flex; align-items: center; gap: 10px;
            padding: 4px 0 10px;
        }
        .filter-btn {
            display: inline-flex; align-items: center; gap: 8px;
            background: linear-gradient(135deg, var(--blue-600), var(--indigo-600));
            color: var(--white);
            border: none;
            padding: 10px 16px; border-radius: 14px;
            font-size: 14px; font-weight: 700;
            cursor: pointer; font-family: inherit;
            box-shadow: 0 8px 20px rgba(91, 61, 240, 0.28);
            transition: transform 0.16s, box-shadow 0.16s;
            position: relative;
        }
        .filter-btn:active { transform: scale(0.96); }
        .filter-btn-icon { font-size: 16px; line-height: 1; }
        .filter-btn-badge {
            display: inline-flex; align-items: center; justify-content: center;
            min-width: 20px; height: 20px; padding: 0 6px;
            background: var(--teal-400);
            color: var(--blue-900);
            font-size: 11px; font-weight: 800;
            border-radius: 10px;
            margin-left: 2px;
        }
        .filter-summary {
            flex: 1; min-width: 0;
            font-size: 12px; color: var(--text-muted);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            font-weight: 600;
        }

        /* ====== Сетка фильтров в модалке (всё видно без скролла) ====== */
        .filter-modal-section {
            margin-bottom: 14px;
        }
        .filter-modal-section:last-child { margin-bottom: 0; }
        .filter-modal-label {
            font-size: 12px; font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase; letter-spacing: 0.5px;
            margin-bottom: 8px;
            display: flex; align-items: center; gap: 6px;
        }
        .filter-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 6px;
        }
        .filter-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }
        .filter-grid.cols-2 { grid-template-columns: repeat(2, 1fr); }
        .filter-chip {
            display: flex; align-items: center; justify-content: center; gap: 4px;
            background: var(--gray-50);
            border: 1.5px solid var(--gray-200);
            color: var(--gray-700);
            padding: 9px 6px; border-radius: 12px;
            font-size: 12px; font-weight: 600;
            cursor: pointer; transition: all 0.16s;
            font-family: inherit;
            white-space: nowrap;
        }
        .filter-chip:active { transform: scale(0.96); }
        .filter-chip.active {
            background: linear-gradient(135deg, var(--blue-600), var(--blue-700));
            border-color: transparent;
            color: var(--white);
            box-shadow: 0 4px 14px rgba(37, 99, 235, 0.32);
        }
        .filter-chip .chip-emoji { font-size: 14px; line-height: 1; }

        .filter-actions {
            display: flex; gap: 10px; margin-top: 18px;
        }
        .filter-actions .btn-primary,
        .filter-actions .btn-secondary {
            flex: 1; margin: 0;
        }

        /* ====== Каталог ====== */
        .catalog-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
            padding: 4px 0 12px;
        }
        .card {
            background: var(--surface);
            border-radius: var(--radius);
            padding: 16px 14px 14px;
            box-shadow: var(--shadow);
            cursor: pointer;
            transition: transform 0.18s, box-shadow 0.18s;
            display: flex; flex-direction: column; gap: 8px;
            position: relative; overflow: hidden;
            border: 1px solid rgba(221, 228, 237, 0.8);
            animation: cardIn 0.32s ease-out backwards;
        }
        .card::after {
            content: ''; position: absolute; top: 0; left: 0; right: 0;
            height: 80px;
            background: linear-gradient(180deg, rgba(91, 61, 240, 0.08), transparent);
            pointer-events: none;
        }
        .card > * { position: relative; z-index: 1; }
        .card:active { transform: scale(0.98); box-shadow: var(--shadow-sm); }
        .card-flag {
            font-size: 36px; line-height: 1;
            width: 52px; height: 52px;
            display: flex; align-items: center; justify-content: center;
            background: linear-gradient(135deg, var(--blue-50), var(--blue-100));
            border-radius: 16px;
        }
        .card-country { font-size: 15px; font-weight: 700; color: var(--gray-900); }
        .card-origin {
            display: inline-flex; align-items: center; gap: 4px;
            font-size: 11px; background: var(--blue-50); color: var(--blue-700);
            padding: 4px 9px; border-radius: 8px;
            font-weight: 600; align-self: flex-start;
        }
        .card-preview {
            font-family: 'SF Mono', Monaco, Consolas, monospace;
            font-size: 12px; color: var(--text-muted); letter-spacing: 0.5px;
        }
        .card-price {
            font-size: 19px; font-weight: 800; color: var(--blue-700);
            margin-top: auto;
            display: flex; align-items: baseline; gap: 3px;
        }
        .card-price .rub { font-size: 13px; color: var(--text-muted); font-weight: 600; }

        /* ====== Лоадер / empty ====== */
        .loader { display: flex; justify-content: center; padding: 32px 0; }
        .spinner {
            width: 32px; height: 32px;
            border: 3px solid var(--blue-100);
            border-top-color: var(--blue-600);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loader.hidden { display: none; }
        .empty-state { text-align: center; padding: 40px 20px; color: var(--text-muted); }
        .empty-state .empty-sub { font-size: 13px; margin-top: 6px; color: var(--text-muted); opacity: 0.7; }

        /* ====== Мои покупки ====== */
        .purchases-list { display: flex; flex-direction: column; gap: 12px; padding: 0 16px 32px; }
        .purchase-card {
            background: var(--white);
            border-radius: 16px;
            padding: 16px;
            box-shadow: 0 4px 18px rgba(17, 33, 92, 0.06);
            border: 1px solid var(--gray-100);
        }
        .purchase-card-head {
            display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 12px; gap: 10px;
        }
        .purchase-phone {
            font-size: 17px; font-weight: 700; color: var(--gray-900);
            font-variant-numeric: tabular-nums;
        }
        .purchase-amount {
            font-size: 14px; font-weight: 600; color: var(--blue-700);
            background: var(--blue-50); padding: 4px 10px; border-radius: 999px;
        }
        .purchase-meta {
            display: flex; gap: 8px; flex-wrap: wrap;
            font-size: 12px; color: var(--gray-500); margin-bottom: 14px;
        }
        .purchase-meta .badge {
            background: var(--gray-100); color: var(--gray-700);
            padding: 3px 8px; border-radius: 6px;
        }
        .purchase-actions {
            display: flex; gap: 8px; flex-wrap: wrap;
        }
        .purchase-actions .pur-btn {
            flex: 1 1 0; min-width: 90px;
            padding: 10px 8px; border-radius: 10px;
            font-size: 13px; font-weight: 600;
            border: none; cursor: pointer; transition: transform .15s, opacity .15s;
            display: inline-flex; align-items: center; justify-content: center; gap: 6px;
        }
        .purchase-actions .pur-btn:active { transform: scale(0.96); }
        .pur-btn.primary { background: var(--blue-600); color: #fff; }
        .pur-btn.primary:hover { background: var(--blue-700); }
        .pur-btn.secondary { background: var(--gray-100); color: var(--gray-900); }
        .pur-btn.secondary:hover { background: var(--gray-200); }
        .pur-btn.success { background: var(--teal-500); color: #fff; }
        .pur-btn.success:hover { background: #0e9c8c; }
        .pur-btn:disabled { opacity: 0.55; cursor: not-allowed; }
        .purchase-card.loading { opacity: 0.7; pointer-events: none; }

        /* ====== Модалка кода ====== */
        .code-modal-body { text-align: center; padding: 8px 0 16px; }
        .code-phone {
            font-size: 14px; color: var(--gray-500); margin-bottom: 10px;
        }
        .code-big {
            font-size: 44px; font-weight: 800; letter-spacing: 8px;
            color: var(--blue-700);
            background: linear-gradient(135deg, var(--blue-50) 0%, var(--blue-100) 100%);
            border-radius: 16px; padding: 22px 12px;
            font-variant-numeric: tabular-nums;
            margin-bottom: 14px;
            box-shadow: inset 0 0 0 1px rgba(43, 82, 212, 0.15);
        }
        .code-hint {
            font-size: 12px; color: var(--gray-500); line-height: 1.5;
        }
        .code-error {
            background: #fff1f0; color: #b3261e;
            border-radius: 12px; padding: 14px;
            font-size: 13px; line-height: 1.5;
            text-align: left;
        }
        .empty-state.hidden { display: none; }
        .empty-emoji { font-size: 48px; margin-bottom: 12px; opacity: 0.5; }

        /* ====== Нижняя навигация ====== */
        .bottom-nav {
            position: fixed; bottom: 0; left: 0; right: 0;
            background: rgba(255, 255, 255, 0.94);
            border-top: 1px solid rgba(221, 228, 237, 0.8);
            display: flex; justify-content: space-around;
            padding: 8px 0 calc(14px + env(safe-area-inset-bottom));
            z-index: 40;
            backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
            box-shadow: 0 -4px 24px rgba(15, 23, 42, 0.06);
        }
        .nav-btn {
            flex: 1; background: none; border: none;
            display: flex; flex-direction: column; align-items: center;
            gap: 3px; padding: 6px 4px;
            color: var(--text-muted); font-size: 11px; font-weight: 600;
            cursor: pointer; transition: color 0.15s; font-family: inherit;
        }
        .nav-btn .nav-emoji { font-size: 22px; transition: transform 0.2s; }
        .nav-btn.active {
            color: var(--indigo-600);
        }
        .nav-btn.active .nav-emoji { transform: scale(1.18) translateY(-2px); }
        .nav-brand {
            position: absolute;
            bottom: calc(2px + env(safe-area-inset-bottom));
            left: 50%; transform: translateX(-50%);
            font-size: 9px; font-weight: 700; letter-spacing: 0.6px;
            text-transform: uppercase; color: var(--gray-400);
            opacity: 0.6;
            pointer-events: none;
        }

        /* ====== Модалки (для item / support) ====== */
        .modal {
            position: fixed; inset: 0; z-index: 100;
            display: flex; align-items: flex-end; justify-content: center;
        }
        .modal.hidden { display: none; }
        .modal-backdrop {
            position: absolute; inset: 0;
            background: rgba(15, 23, 42, 0.55);
            backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
            animation: fadeIn 0.2s ease-out;
        }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        .modal-sheet {
            position: relative; background: var(--white);
            width: 100%; max-width: 480px;
            border-radius: 24px 24px 0 0;
            padding: 12px 20px calc(24px + env(safe-area-inset-bottom));
            animation: slideUp 0.28s cubic-bezier(0.32, 0.72, 0, 1);
        }
        @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
        .modal-handle {
            width: 40px; height: 4px; background: var(--gray-300);
            border-radius: 2px; margin: 0 auto 16px;
        }
        .modal-title { font-size: 20px; font-weight: 700; color: var(--gray-900); margin-bottom: 12px; }

        /* ====== Профиль — ПОЛНОЦЕННАЯ СТРАНИЦА ====== */
        .profile-hero {
            background: linear-gradient(135deg, #1f3eaa 0%, #2a52d4 45%, #5b3df0 100%);
            color: var(--white);
            padding: 20px 20px 100px;
            position: relative;
            overflow: hidden;
        }
        .profile-hero::before {
            content: ''; position: absolute; top: -40px; right: -40px;
            width: 200px; height: 200px;
            background: radial-gradient(circle, rgba(255,255,255,0.16), transparent 65%);
            border-radius: 50%;
        }
        .profile-hero::after {
            content: ''; position: absolute; bottom: -30px; left: -30px;
            width: 160px; height: 160px;
            background: radial-gradient(circle, rgba(20, 184, 166, 0.30), transparent 65%);
            border-radius: 50%;
        }
        .profile-hero .profile-watermark {
            position: absolute; right: 18px; top: 18px;
            font-size: 11px; font-weight: 700; letter-spacing: 0.6px;
            text-transform: uppercase; opacity: 0.7;
            z-index: 1;
        }
        .profile-hero .profile-watermark .dot {
            display: inline-block; width: 6px; height: 6px; border-radius: 50%;
            background: var(--teal-400); margin-right: 6px;
            box-shadow: 0 0 0 3px rgba(45, 212, 191, 0.3);
            vertical-align: middle;
        }
        .profile-back {
            position: absolute; top: 14px; left: 14px;
            width: 38px; height: 38px; border-radius: 50%;
            background: rgba(255, 255, 255, 0.18);
            border: 1px solid rgba(255,255,255,0.18);
            color: var(--white); font-size: 18px;
            display: flex; align-items: center; justify-content: center;
            cursor: pointer; backdrop-filter: blur(10px);
            transition: background 0.15s; font-family: inherit;
        }
        .profile-back:active { background: rgba(255, 255, 255, 0.32); }
        .profile-hero > * { position: relative; z-index: 1; }
        .profile-hero-center {
            display: flex; flex-direction: column; align-items: center;
            padding-top: 24px;
        }
        .profile-hero .avatar {
            width: 84px; height: 84px;
            border: 3px solid rgba(255,255,255,0.42);
            box-shadow: 0 8px 24px rgba(0,0,0,0.18);
        }
        .profile-name {
            font-size: 22px; font-weight: 700; margin-top: 12px;
            text-align: center;
        }
        .profile-username {
            font-size: 13px; opacity: 0.85; margin-top: 4px;
            text-align: center;
        }

        /* Баланс-карточка, «парящая» над низом hero */
        .balance-card {
            margin: -68px 16px 0;
            background: var(--white);
            border-radius: var(--radius);
            padding: 20px;
            box-shadow: var(--shadow-lg);
            position: relative; z-index: 2;
            border: 1px solid rgba(226, 232, 240, 0.8);
        }
        .balance-card-label {
            display: flex; align-items: center; justify-content: space-between;
            font-size: 12px; color: var(--text-muted);
            text-transform: uppercase; letter-spacing: 0.6px; font-weight: 600;
            margin-bottom: 8px;
        }
        .balance-card-value {
            font-size: 36px; font-weight: 800; color: var(--blue-900);
            letter-spacing: -0.8px; line-height: 1;
            display: flex; align-items: baseline; gap: 6px;
        }
        .balance-card-value .cur { font-size: 18px; color: var(--text-muted); font-weight: 600; }
        .balance-card-hold {
            font-size: 12px; color: var(--text-muted); margin-top: 6px;
        }
        .balance-card-hold b { color: var(--amber-500); font-weight: 700; }

        /* Кнопка «Пополнить» */
        .btn-primary, .btn-secondary, .btn-ghost {
            width: 100%; padding: 14px; border: none;
            border-radius: var(--radius-sm);
            font-size: 15px; font-weight: 700;
            cursor: pointer; transition: all 0.18s;
            font-family: inherit;
            display: inline-flex; align-items: center; justify-content: center; gap: 8px;
        }
        .btn-primary {
            background: linear-gradient(135deg, var(--blue-600), var(--blue-700));
            color: var(--white);
            box-shadow: 0 8px 22px rgba(37, 99, 235, 0.38);
            margin-top: 14px;
        }
        .btn-primary:active { transform: scale(0.98); box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3); }
        .btn-secondary {
            background: var(--gray-100); color: var(--gray-700);
            margin-top: 8px;
        }
        .btn-secondary:active { background: var(--gray-200); }
        .btn-ghost {
            background: transparent; color: var(--blue-600);
            border: 1.5px solid var(--blue-200);
            margin-top: 8px;
        }
        .btn-ghost:active { background: var(--blue-50); }

        /* Статистика */
        .stats-grid {
            display: grid; grid-template-columns: 1fr 1fr;
            gap: 10px; margin: 16px;
        }
        .stat-card {
            background: var(--surface);
            border-radius: var(--radius-sm);
            padding: 14px;
            border: 1px solid var(--gray-200);
            box-shadow: var(--shadow-sm);
        }
        .stat-label {
            font-size: 11px; color: var(--text-muted);
            text-transform: uppercase; letter-spacing: 0.6px; font-weight: 700;
            margin-bottom: 6px;
        }
        .stat-value {
            font-size: 19px; font-weight: 800; color: var(--gray-900);
            display: flex; align-items: baseline; gap: 3px;
        }
        .stat-value .small { font-size: 12px; color: var(--text-muted); font-weight: 600; }

        /* Список действий */
        .profile-actions {
            margin: 0 16px 24px;
            background: var(--surface);
            border-radius: var(--radius);
            border: 1px solid var(--gray-200);
            box-shadow: var(--shadow-sm);
            overflow: hidden;
        }

        /* ====== Промокод-карточка в профиле ====== */
        .promo-card {
            margin: 16px;
            background: linear-gradient(135deg, rgba(91, 61, 240, 0.06), rgba(20, 184, 166, 0.05));
            border: 1.5px solid rgba(91, 61, 240, 0.18);
            border-radius: var(--radius);
            padding: 18px;
            box-shadow: var(--shadow-sm);
            position: relative;
            overflow: hidden;
        }
        .promo-card::before {
            content: ''; position: absolute; top: -40px; right: -40px;
            width: 120px; height: 120px;
            background: radial-gradient(circle, rgba(139, 92, 246, 0.16), transparent 65%);
            border-radius: 50%;
            pointer-events: none;
        }
        .promo-card > * { position: relative; z-index: 1; }
        .promo-card-head {
            display: flex; align-items: center; gap: 12px; margin-bottom: 12px;
        }
        .promo-card-icon {
            width: 42px; height: 42px; border-radius: 12px;
            background: linear-gradient(135deg, var(--violet-500), var(--indigo-600));
            color: var(--white);
            display: flex; align-items: center; justify-content: center;
            font-size: 22px;
            box-shadow: 0 6px 16px rgba(91, 61, 240, 0.35);
            flex-shrink: 0;
        }
        .promo-card-title { flex: 1; min-width: 0; }
        .promo-card-name {
            font-size: 15px; font-weight: 700; color: var(--gray-900);
        }
        .promo-card-sub {
            font-size: 12px; color: var(--text-muted); margin-top: 2px;
        }
        .promo-card-btn {
            margin-top: 0;
            width: 100%;
        }
        .action-item {
            display: flex; align-items: center; gap: 12px;
            padding: 14px 16px;
            cursor: pointer; transition: background 0.15s;
            border: none; background: transparent; width: 100%;
            text-align: left; font-family: inherit; color: var(--gray-900);
        }
        .action-item:not(:last-child) {
            border-bottom: 1px solid var(--gray-100);
        }
        .action-item:active { background: var(--gray-50); }
        .action-emoji {
            width: 36px; height: 36px;
            border-radius: 10px;
            display: flex; align-items: center; justify-content: center;
            font-size: 18px;
            background: var(--blue-50);
            flex-shrink: 0;
        }
        .action-text { flex: 1; }
        .action-title { font-size: 14px; font-weight: 600; color: var(--gray-900); }
        .action-desc { font-size: 12px; color: var(--text-muted); margin-top: 1px; }
        .action-arrow { color: var(--gray-400); font-size: 18px; }

        /* ====== Страница пополнения ====== */
        .topup-hero {
            background: linear-gradient(135deg, var(--blue-700) 0%, var(--blue-600) 55%, var(--indigo-600) 100%);
            color: var(--white);
            padding: 20px 20px 28px;
            position: relative; overflow: hidden;
        }
        .topup-hero::before {
            content: ''; position: absolute; top: -40px; right: -40px;
            width: 200px; height: 200px;
            background: radial-gradient(circle, rgba(255,255,255,0.16), transparent 65%);
            border-radius: 50%;
        }
        .topup-hero > * { position: relative; z-index: 1; }
        .topup-title {
            font-size: 24px; font-weight: 800;
            text-align: center; margin-top: 24px;
        }
        .topup-sub {
            font-size: 13px; opacity: 0.85; margin-top: 6px;
            text-align: center;
        }

        .topup-methods {
            margin: -16px 16px 16px;
            display: flex; flex-direction: column; gap: 10px;
            position: relative; z-index: 2;
        }
        .topup-method {
            background: var(--surface);
            border: 1.5px solid var(--gray-200);
            border-radius: var(--radius);
            padding: 16px;
            display: flex; align-items: center; gap: 14px;
            cursor: pointer; transition: all 0.18s;
            box-shadow: var(--shadow-sm);
            font-family: inherit; color: var(--gray-900);
            text-align: left; width: 100%;
        }
        .topup-method:active { transform: scale(0.98); }
        .topup-method.active {
            border-color: var(--blue-500);
            background: linear-gradient(135deg, var(--blue-50), var(--surface));
            box-shadow: 0 8px 22px rgba(37, 99, 235, 0.18);
        }
        .topup-method-icon {
            width: 44px; height: 44px; border-radius: 12px;
            display: flex; align-items: center; justify-content: center;
            font-size: 22px;
            background: linear-gradient(135deg, var(--blue-100), var(--blue-50));
            flex-shrink: 0;
        }
        .topup-method-text { flex: 1; min-width: 0; }
        .topup-method-title { font-size: 15px; font-weight: 700; }
        .topup-method-desc { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

        .promo-section {
            background: var(--surface);
            border: 1.5px solid var(--gray-200);
            border-radius: var(--radius);
            padding: 18px;
            margin: 0 16px 16px;
            box-shadow: var(--shadow-sm);
        }
        .promo-section h4 {
            font-size: 14px; font-weight: 700; color: var(--gray-900);
            margin-bottom: 6px; display: flex; align-items: center; gap: 8px;
        }
        .promo-section p {
            font-size: 12px; color: var(--text-muted); margin-bottom: 12px;
        }
        .promo-input-row {
            display: flex; gap: 8px;
        }
        .promo-input {
            flex: 1; padding: 12px 14px;
            border: 1.5px solid var(--gray-200);
            border-radius: var(--radius-sm);
            font-size: 14px; font-family: inherit;
            background: var(--gray-50);
            color: var(--gray-900);
            text-transform: uppercase;
            letter-spacing: 1px;
            transition: border-color 0.15s, background 0.15s;
        }
        .promo-input:focus {
            outline: none;
            border-color: var(--blue-500);
            background: var(--white);
            box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.12);
        }
        .promo-btn {
            padding: 12px 18px;
            background: linear-gradient(135deg, var(--blue-600), var(--blue-700));
            color: var(--white);
            border: none; border-radius: var(--radius-sm);
            font-size: 14px; font-weight: 700;
            cursor: pointer; font-family: inherit;
            transition: transform 0.15s;
        }
        .promo-btn:active { transform: scale(0.96); }
        .promo-btn:disabled { opacity: 0.6; cursor: not-allowed; }
        .promo-msg {
            margin-top: 12px; padding: 10px 12px;
            border-radius: 10px;
            font-size: 13px; font-weight: 600;
            display: none;
        }
        .promo-msg.success {
            display: block;
            background: rgba(34, 197, 94, 0.1);
            color: var(--green-600);
            border: 1px solid rgba(34, 197, 94, 0.25);
        }
        .promo-msg.error {
            display: block;
            background: rgba(239, 68, 68, 0.08);
            color: var(--red-500);
            border: 1px solid rgba(239, 68, 68, 0.22);
        }

        /* ====== Item details modal ====== */
        .item-head { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
        .item-flag { font-size: 48px; line-height: 1; }
        .item-sub { font-size: 13px; color: var(--text-muted); margin-top: 2px; }
        .item-price {
            font-size: 32px; font-weight: 800; color: var(--blue-700);
            text-align: center; margin: 8px 0 16px;
        }
        .item-seller-row {
            display: flex; align-items: center; gap: 12px;
            padding: 12px; margin-bottom: 12px;
            background: var(--gray-50); border-radius: 14px;
            border: 1px solid var(--gray-100);
        }
        .item-seller-avatar {
            width: 38px; height: 38px; border-radius: 50%;
            background: linear-gradient(135deg, var(--blue-500), var(--indigo-600));
            color: var(--white); display: flex; align-items: center; justify-content: center;
            font-size: 18px; font-weight: 700; flex-shrink: 0;
        }
        .item-seller-info { flex: 1; min-width: 0; }
        .item-seller-name {
            font-size: 14px; font-weight: 700; color: var(--gray-900);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .item-seller-rating {
            font-size: 12px; color: #f59e0b; font-weight: 600;
            margin-top: 2px;
        }
        .item-description {
            font-size: 14px; color: var(--gray-700); line-height: 1.5;
            padding: 12px; margin-bottom: 12px;
            background: var(--blue-50); border-radius: 12px;
            border: 1px solid var(--blue-100);
        }
        .item-features { list-style: none; margin-bottom: 16px; }
        .item-features li {
            padding: 8px 0; border-bottom: 1px solid var(--gray-100);
            font-size: 14px; color: var(--gray-700);
        }
        .item-features li:last-child { border-bottom: none; }
        .item-hint {
            color: var(--text-muted); font-size: 13px;
            margin-bottom: 16px; line-height: 1.6;
        }
        .support-text { color: var(--gray-700); margin-bottom: 16px; line-height: 1.6; }

        /* Тост */
        .toast {
            position: fixed; left: 50%; bottom: 110px;
            transform: translateX(-50%) translateY(20px);
            background: rgba(15, 23, 42, 0.95);
            color: var(--white);
            padding: 12px 18px;
            border-radius: 14px;
            font-size: 14px; font-weight: 600;
            box-shadow: 0 12px 28px rgba(0,0,0,0.25);
            opacity: 0;
            transition: all 0.25s cubic-bezier(0.32, 0.72, 0, 1);
            z-index: 200;
            pointer-events: none;
            max-width: 90vw; text-align: center;
        }
        .toast.show {
            opacity: 1; transform: translateX(-50%) translateY(0);
        }
        .toast.success { background: linear-gradient(135deg, #16a34a, #22c55e); }
        .toast.error { background: linear-gradient(135deg, #dc2626, #ef4444); }

        /* Адаптив */
        @media (min-width: 600px) {
            .catalog-grid { grid-template-columns: repeat(3, 1fr); max-width: 720px; margin: 0 auto; }
        }
        @media (min-width: 900px) {
            .catalog-grid { grid-template-columns: repeat(4, 1fr); }
            body { max-width: 720px; margin: 0 auto; box-shadow: 0 0 60px rgba(0,0,0,0.06); background-color: var(--bg); }
            .bottom-nav { max-width: 720px; left: 50%; transform: translateX(-50%); }
            .app-header { max-width: 720px; left: 50%; transform: translateX(-50%); width: 100%; }
        }
        @keyframes cardIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .card:nth-child(1) { animation-delay: 0.02s; }
        .card:nth-child(2) { animation-delay: 0.04s; }
        .card:nth-child(3) { animation-delay: 0.06s; }
        .card:nth-child(4) { animation-delay: 0.08s; }
        .card:nth-child(5) { animation-delay: 0.10s; }
        .card:nth-child(6) { animation-delay: 0.12s; }
    </style>
</head>
<body>
    <!-- ====== Каталог ====== -->
    <div class="page active" id="pageCatalog">
        <header class="app-header">
            <div class="avatar" id="userAvatar">
                <span class="avatar-fallback" id="avatarFallback">…</span>
            </div>
            <div class="user-info">
                <div class="user-name" id="userName">—</div>
                <div class="user-meta" id="userMeta">нажмите, чтобы открыть профиль</div>
            </div>
            <button class="balance-pill" id="balancePill" aria-label="Баланс">
                <span class="balance-cur">₽</span>
                <span id="balanceValue">—</span>
            </button>
        </header>

        <section class="hero">
            <span class="hero-brand"><span class="dot"></span>Vest Account</span>
            <h1 class="hero-title">Маркетплейс<br><span>аккаунтов</span></h1>
            <p class="hero-sub">Проверенные сессии · моментальная выдача · поддержка 24/7</p>
        </section>

        <section class="section">
            <div class="filter-bar">
                <button class="filter-btn" id="openFilters" type="button">
                    <span class="filter-btn-icon">🎛️</span>
                    <span>Фильтры</span>
                    <span class="filter-btn-badge" id="filterBadge" hidden>0</span>
                </button>
                <div class="filter-summary" id="filterSummary">Все страны · Любое · По умолчанию</div>
            </div>
        </section>

        <section class="section">
            <div class="section-head">
                <h2 class="section-title">Каталог</h2>
                <span class="section-count" id="catListCount">0</span>
            </div>
            <div class="catalog-grid" id="catalog"></div>
            <div class="loader" id="loader"><div class="spinner"></div></div>
            <div class="empty-state hidden" id="emptyState">
                <div class="empty-emoji">📭</div>
                <p>В этой категории пока ничего нет</p>
            </div>
        </section>
    </div>

    <!-- ====== Профиль (полноценная страница) ====== -->
    <div class="page" id="pageProfile">
        <div class="profile-hero">
            <button class="profile-back" id="profileBack" aria-label="Назад">←</button>
            <span class="profile-watermark"><span class="dot"></span>Vest Account</span>
            <div class="profile-hero-center">
                <div class="avatar" id="profileAvatar"></div>
                <div class="profile-name" id="profileName">—</div>
                <div class="profile-username" id="profileUsername">—</div>
            </div>
        </div>

        <div class="balance-card">
            <div class="balance-card-label">
                <span>Ваш баланс</span>
            </div>
            <div class="balance-card-value">
                <span id="profileBalance">0</span>
                <span class="cur">₽</span>
            </div>
            <div class="balance-card-hold">
                В холде: <b id="profileHold">0</b> ₽
            </div>
            <button class="btn-primary" id="topupBtn">
                <span>＋</span><span>Пополнить баланс</span>
            </button>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Рейтинг</div>
                <div class="stat-value" id="profileRating">5.0 <span class="small">★</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Отзывы</div>
                <div class="stat-value" id="profileReviews">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Потрачено</div>
                <div class="stat-value"><span id="profileSpent">0</span><span class="small">₽</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Заработано</div>
                <div class="stat-value"><span id="profileEarned">0</span><span class="small">₽</span></div>
            </div>
        </div>

        <!-- ====== Промокод — кнопка в профиле, открывает мини-окно (общая БД с ботом) ====== -->
        <div class="promo-card">
            <div class="promo-card-head">
                <div class="promo-card-icon">🎁</div>
                <div class="promo-card-title">
                    <div class="promo-card-name">Промокод</div>
                    <div class="promo-card-sub">Баланс пополнится мгновенно</div>
                </div>
            </div>
            <button class="btn-primary promo-card-btn" id="openPromoModal">
                <span>🎁</span><span>Активировать промокод</span>
            </button>
        </div>

        <div class="profile-actions">
            <button class="action-item" id="openTopup2">
                <div class="action-emoji">💳</div>
                <div class="action-text">
                    <div class="action-title">Пополнить баланс</div>
                    <div class="action-desc">СБП · Crypto · Промокод</div>
                </div>
                <div class="action-arrow">›</div>
            </button>
            <button class="action-item" id="openPurchasesFromProfile">
                <div class="action-emoji">📦</div>
                <div class="action-text">
                    <div class="action-title">Мои покупки</div>
                    <div class="action-desc">Код · .session · JSON</div>
                </div>
                <div class="action-arrow">›</div>
            </button>
            <button class="action-item" id="openSupport">
                <div class="action-emoji">💬</div>
                <div class="action-text">
                    <div class="action-title">Поддержка</div>
                    <div class="action-desc">Связаться с менеджером</div>
                </div>
                <div class="action-arrow">›</div>
            </button>
            <button class="action-item" id="openBot">
                <div class="action-emoji">🤖</div>
                <div class="action-text">
                    <div class="action-title">Открыть бота</div>
                    <div class="action-desc">Полный функционал</div>
                </div>
                <div class="action-arrow">›</div>
            </button>
        </div>
    </div>

    <!-- ====== Мои покупки (полноценная страница) ====== -->
    <div class="page" id="pagePurchases">
        <div class="topup-hero">
            <button class="profile-back" id="purchasesBack" aria-label="Назад">←</button>
            <div class="topup-title">Мои покупки</div>
            <div class="topup-sub">Получите код, .session или JSON по любой покупке</div>
        </div>

        <div class="purchases-list" id="purchasesList">
            <!-- Карточки покупок рендерятся JS-ом -->
        </div>

        <div class="loader hidden" id="purchasesLoader">
            <div class="spinner"></div>
        </div>

        <div class="empty-state hidden" id="purchasesEmpty">
            <div class="empty-emoji">📦</div>
            <p>У вас пока нет покупок</p>
            <p class="empty-sub">Купите аккаунт — он появится здесь</p>
        </div>
    </div>

    <!-- ====== Модалка кода подтверждения ====== -->
    <div class="modal hidden" id="codeModal">
        <div class="modal-backdrop" data-close="codeModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <h3 class="modal-title" id="codeModalTitle">🔐 Код подтверждения</h3>
            <div class="code-modal-body" id="codeModalBody">
                <div class="code-phone" id="codePhone">—</div>
                <div class="code-big" id="codeValue">—</div>
                <div class="code-hint">
                    Код действителен ограниченное время.
                    При необходимости можно запросить повторно.
                </div>
            </div>
            <button class="btn-primary" id="codeModalRefresh">🔄 Получить ещё раз</button>
            <button class="btn-secondary" data-close="codeModal">Закрыть</button>
        </div>
    </div>

    <!-- ====== Пополнение баланса ====== -->
    <div class="page" id="pageTopup">
        <div class="topup-hero">
            <button class="profile-back" id="topupBack" aria-label="Назад">←</button>
            <div class="topup-title">Пополнение баланса</div>
            <div class="topup-sub">Выберите способ или активируйте промокод</div>
        </div>

        <div class="topup-methods">
            <button class="topup-method" data-method="sbp">
                <div class="topup-method-icon">🏦</div>
                <div class="topup-method-text">
                    <div class="topup-method-title">СБП — по номеру</div>
                    <div class="topup-method-desc">Перевод по номеру телефона</div>
                </div>
                <div class="action-arrow">›</div>
            </button>
            <button class="topup-method" data-method="crypto">
                <div class="topup-method-icon">🪙</div>
                <div class="topup-method-text">
                    <div class="topup-method-title">CryptoBot</div>
                    <div class="topup-method-desc">BTC · TON · USDT и др.</div>
                </div>
                <div class="action-arrow">›</div>
            </button>
            <button class="topup-method" data-method="card">
                <div class="topup-method-icon">💳</div>
                <div class="topup-method-text">
                    <div class="topup-method-title">Банковская карта</div>
                    <div class="topup-method-desc">Через поддержку</div>
                </div>
                <div class="action-arrow">›</div>
            </button>
        </div>
    </div>

    <nav class="bottom-nav" id="bottomNav">
        <button class="nav-btn active" data-page="pageCatalog">
            <span class="nav-emoji">🛍️</span><span>Каталог</span>
        </button>
        <button class="nav-btn" data-page="pagePurchases">
            <span class="nav-emoji">📦</span><span>Мои покупки</span>
        </button>
        <button class="nav-btn" data-page="pageProfile">
            <span class="nav-emoji">👤</span><span>Профиль</span>
        </button>
        <div class="nav-brand">Vest Account</div>
    </nav>

    <!-- Модалка фильтров (все страны + происхождение + цена) -->
    <div class="modal hidden" id="filtersModal">
        <div class="modal-backdrop" data-close="filtersModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <h3 class="modal-title">🎛️ Фильтры</h3>

            <div class="filter-modal-section">
                <div class="filter-modal-label">Страна</div>
                <div class="filter-grid" id="filtersCountryGrid">
                    <!-- страны рендерятся JS-ом: первая кнопка «Все» + 17 стран -->
                </div>
            </div>

            <div class="filter-modal-section">
                <div class="filter-modal-label">Происхождение</div>
                <div class="filter-grid cols-4" id="filtersOriginGrid">
                    <!-- «Любое» + 4 происхождения -->
                </div>
            </div>

            <div class="filter-modal-section">
                <div class="filter-modal-label">Сортировка по цене</div>
                <div class="filter-grid cols-2" id="filtersPriceGrid">
                    <!-- дешевле / дороже / новые / по умолчанию -->
                </div>
            </div>

            <div class="filter-actions">
                <button class="btn-secondary" id="filtersReset">Сбросить</button>
                <button class="btn-primary" id="filtersApply">Применить</button>
            </div>
        </div>
    </div>

    <!-- Модалка item -->
    <div class="modal hidden" id="itemModal">
        <div class="modal-backdrop" data-close="itemModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <div class="item-head">
                <span class="item-flag" id="itemFlag">🌍</span>
                <div>
                    <h3 class="modal-title" id="itemCountry">—</h3>
                    <div class="item-sub" id="itemOrigin">—</div>
                </div>
            </div>
            <div class="item-price" id="itemPrice">— ₽</div>

            <div class="item-seller-row">
                <div class="item-seller-avatar" id="itemSellerAvatar">👤</div>
                <div class="item-seller-info">
                    <div class="item-seller-name" id="itemSeller">Продавец</div>
                    <div class="item-seller-rating" id="itemRating">— ★</div>
                </div>
            </div>

            <div class="item-description" id="itemDescription">Описание появится после загрузки.</div>

            <ul class="item-features">
                <li>✅ Сессия прошла верификацию</li>
                <li>⚡ Выдача сразу после оплаты</li>
                <li>🔒 Без банов на момент продажи</li>
            </ul>

            <button class="btn-primary" id="buyBtn">Купить</button>
            <button class="btn-secondary" data-close="itemModal">Закрыть</button>
        </div>
    </div>

    <!-- Модалка support -->
    <div class="modal hidden" id="supportModal">
        <div class="modal-backdrop" data-close="supportModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <h3 class="modal-title">Поддержка</h3>
            <p class="support-text">
                Если что-то пошло не так — напишите в поддержку.
                Ответ обычно в течение 15 минут.
            </p>
            <button class="btn-primary" id="supportBtn">Открыть чат поддержки</button>
            <button class="btn-secondary" data-close="supportModal">Закрыть</button>
        </div>
    </div>

    <!-- Модалка промокода (открывается из профиля) -->
    <div class="modal hidden" id="promoModal">
        <div class="modal-backdrop" data-close="promoModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <h3 class="modal-title">🎁 Активация промокода</h3>
            <p class="support-text">
                Введите промокод — баланс пополнится мгновенно.
                Промокоды общие с ботом и действуют на всех.
            </p>
            <div class="promo-input-row">
                <input
                    type="text"
                    class="promo-input"
                    id="profilePromoInput"
                    placeholder="VEST-XXXX"
                    autocomplete="off"
                    autocapitalize="characters"
                    spellcheck="false"
                    maxlength="32"
                />
            </div>
            <button class="btn-primary" id="profilePromoBtn">Активировать</button>
            <div class="promo-msg" id="profilePromoMsg"></div>
            <button class="btn-secondary" data-close="promoModal">Закрыть</button>
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <script>
        (function () {
            'use strict';

            const tg = window.Telegram && window.Telegram.WebApp;
            if (tg) { tg.ready(); tg.expand(); }

            const state = {
                initData: tg ? tg.initData : '',
                tgUser: tg && tg.initDataUnsafe && tg.initDataUnsafe.user
                    ? tg.initDataUnsafe.user : null,
                country: 'all',
                origin: 'all',
                priceSort: 'default',  // default | asc | desc | new
                catalog: [],
                categories: [],
                currentPage: 'pageCatalog',
                botUsername: null,
                lastBalanceSync: null,
            };

            const $ = (id) => document.getElementById(id);
            const dom = {
                balancePill: $('balancePill'),
                balanceValue: $('balanceValue'),
                filterSummary: $('filterSummary'),
                filterBadge: $('filterBadge'),
                catListCount: $('catListCount'),
                catalog: $('catalog'),
                loader: $('loader'),
                emptyState: $('emptyState'),
                pageCatalog: $('pageCatalog'),
                pageProfile: $('pageProfile'),
                pageTopup: $('pageTopup'),
                pagePurchases: $('pagePurchases'),
                purchasesList: $('purchasesList'),
                purchasesLoader: $('purchasesLoader'),
                purchasesEmpty: $('purchasesEmpty'),
                purchasesBack: $('purchasesBack'),
                codeModal: $('codeModal'),
                codeModalTitle: $('codeModalTitle'),
                codePhone: $('codePhone'),
                codeValue: $('codeValue'),
                codeModalRefresh: $('codeModalRefresh'),
                profileBack: $('profileBack'),
                profileAvatar: $('profileAvatar'),
                profileName: $('profileName'),
                profileUsername: $('profileUsername'),
                profileBalance: $('profileBalance'),
                profileHold: $('profileHold'),
                profileRating: $('profileRating'),
                profileReviews: $('profileReviews'),
                profileSpent: $('profileSpent'),
                profileEarned: $('profileEarned'),
                // (balanceSyncedAt убран — без индикатора синхронизации)
                topupBtn: $('topupBtn'),
                topupBack: $('topupBack'),
                openTopup2: $('openTopup2'),
                openSupport: $('openSupport'),
                openBot: $('openBot'),
                openPurchasesFromProfile: $('openPurchasesFromProfile'),
                profilePromoInput: $('profilePromoInput'),
                profilePromoBtn: $('profilePromoBtn'),
                profilePromoMsg: $('profilePromoMsg'),
                openPromoModal: $('openPromoModal'),
                promoModal: $('promoModal'),
                // (topup-page promo поля убраны — ввода промокода там больше нет)
                itemModal: $('itemModal'),
                itemFlag: $('itemFlag'),
                itemCountry: $('itemCountry'),
                itemOrigin: $('itemOrigin'),
                itemPrice: $('itemPrice'),
                itemDescription: $('itemDescription'),
                itemSeller: $('itemSeller'),
                itemRating: $('itemRating'),
                buyBtn: $('buyBtn'),
                openFilters: $('openFilters'),
                filtersModal: $('filtersModal'),
                filtersCountryGrid: $('filtersCountryGrid'),
                filtersOriginGrid: $('filtersOriginGrid'),
                filtersPriceGrid: $('filtersPriceGrid'),
                filtersApply: $('filtersApply'),
                filtersReset: $('filtersReset'),
                supportModal: $('supportModal'),
                supportBtn: $('supportBtn'),
                toast: $('toast'),
                bottomNav: $('bottomNav'),
            };

            /* ===== Утилиты ===== */
            function showToast(text, type) {
                dom.toast.textContent = text;
                dom.toast.className = 'toast show ' + (type || '');
                clearTimeout(showToast._t);
                showToast._t = setTimeout(() => {
                    dom.toast.classList.remove('show');
                }, 2400);
            }
            function escapeHtml(s) {
                return String(s || '').replace(/[&<>"']/g, (c) => ({
                    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
                })[c]);
            }
            function formatPrice(n) {
                return (Number(n) || 0).toLocaleString('ru-RU', { maximumFractionDigits: 0 });
            }
            function formatRub(n) {
                return formatPrice(n);
            }

            /* ===== Мои покупки ===== */
            const purchasesState = {
                items: [],
                loading: false,
                codePurchaseId: null,
            };

            function formatPurchaseDate(iso) {
                if (!iso) return '';
                try {
                    const d = new Date(iso);
                    const pad = (n) => String(n).padStart(2, '0');
                    return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${String(d.getFullYear()).slice(-2)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
                } catch (_) {
                    return '';
                }
            }

            async function loadPurchases() {
                if (purchasesState.loading) return;
                purchasesState.loading = true;
                dom.purchasesLoader.classList.remove('hidden');
                dom.purchasesList.innerHTML = '';
                dom.purchasesEmpty.classList.add('hidden');
                try {
                    const resp = await fetch('/api/purchases', {
                        headers: state.initData ? { 'X-Init-Data': state.initData } : {},
                    });
                    const data = await resp.json();
                    if (!data.ok) {
                        showToast('Не удалось загрузить покупки', 'error');
                        return;
                    }
                    purchasesState.items = data.items || [];
                    renderPurchases();
                } catch (e) {
                    console.error('loadPurchases error', e);
                    showToast('Ошибка сети', 'error');
                } finally {
                    purchasesState.loading = false;
                    dom.purchasesLoader.classList.add('hidden');
                }
            }

            function renderPurchases() {
                const list = dom.purchasesList;
                list.innerHTML = '';
                if (!purchasesState.items.length) {
                    dom.purchasesEmpty.classList.remove('hidden');
                    return;
                }
                dom.purchasesEmpty.classList.add('hidden');

                const flags = {
                    'США': '🇺🇸', 'Россия': '🇷🇺', 'Индия': '🇮🇳', 'Германия': '🇩🇪',
                    'Бразилия': '🇧🇷', 'Индонезия': '🇮🇩', 'Казахстан': '🇰🇿',
                    'Украина': '🇺🇦', 'Беларусь': '🇧🇾', 'Вьетнам': '🇻🇳',
                    'Филиппины': '🇵🇭', 'Мьянма': '🇲🇲', 'Мексика': '🇲🇽',
                    'Турция': '🇹🇷', 'Польша': '🇵🇱', 'Великобритания': '🇬🇧',
                    'Аргентина': '🇦🇷',
                };

                for (const p of purchasesState.items) {
                    const card = document.createElement('div');
                    card.className = 'purchase-card';
                    card.dataset.purchaseId = p.id;
                    const country = p.country || '—';
                    const flag = flags[country] || '🌍';
                    const date = formatPurchaseDate(p.created_at);
                    const method = p.payment_method || '—';
                    const hasSession = !!p.has_session;

                    card.innerHTML = `
                        <div class="purchase-card-head">
                            <div class="purchase-phone">${escapeHtml(p.phone || '—')}</div>
                            <div class="purchase-amount">${formatRub(p.amount)} ₽</div>
                        </div>
                        <div class="purchase-meta">
                            <span class="badge">${flag} ${escapeHtml(country)}</span>
                            <span class="badge">${escapeHtml(method)}</span>
                            <span class="badge">${escapeHtml(date)}</span>
                        </div>
                        <div class="purchase-actions">
                            <button class="pur-btn primary" data-act="code" ${hasSession ? '' : 'disabled'}>
                                🔐 Код
                            </button>
                            <button class="pur-btn secondary" data-act="session" ${hasSession ? '' : 'disabled'}>
                                📄 .session
                            </button>
                            <button class="pur-btn success" data-act="json" ${hasSession ? '' : 'disabled'}>
                                { } JSON
                            </button>
                        </div>
                    `;
                    // Обработчики кликов
                    card.querySelectorAll('.pur-btn').forEach((btn) => {
                        btn.addEventListener('click', () => {
                            const act = btn.dataset.act;
                            if (act === 'code') fetchPurchaseCode(p.id, card);
                            else if (act === 'session') downloadPurchaseFile(p.id, 'session', card);
                            else if (act === 'json') downloadPurchaseFile(p.id, 'json', card);
                        });
                    });
                    list.appendChild(card);
                }
            }

            async function fetchPurchaseCode(purchaseId, card) {
                if (!card) card = document.querySelector(`.purchase-card[data-purchase-id="${purchaseId}"]`);
                if (card) card.classList.add('loading');
                try {
                    const resp = await fetch(`/api/purchases/${purchaseId}/code`, {
                        method: 'POST',
                        headers: state.initData ? { 'X-Init-Data': state.initData } : {},
                    });
                    const data = await resp.json();
                    if (data.ok && data.code) {
                        purchasesState.codePurchaseId = purchaseId;
                        dom.codePhone.textContent = data.phone ? 'Номер: +' + data.phone.replace(/^\+/, '') : '—';
                        dom.codeValue.textContent = data.code;
                        dom.codeModalTitle.textContent = '🔐 Код подтверждения';
                        // Восстановим обычный вид (если до этого была ошибка)
                        const body = document.getElementById('codeModalBody');
                        if (body) {
                            body.innerHTML = `
                                <div class="code-phone" id="codePhone">${escapeHtml(data.phone ? 'Номер: +' + data.phone.replace(/^\\+/, '') : '—')}</div>
                                <div class="code-big" id="codeValue">${escapeHtml(data.code)}</div>
                                <div class="code-hint">Код действителен ограниченное время. При необходимости можно запросить повторно.</div>
                            `;
                        }
                        dom.codeModal.classList.remove('hidden');
                    } else {
                        const phone = data.phone ? '+' + data.phone.replace(/^\+/, '') : '';
                        const hint = data.hint || 'Попробуйте позже.';
                        showCodeError(phone, hint);
                    }
                } catch (e) {
                    console.error('fetchPurchaseCode error', e);
                    showToast('Ошибка получения кода', 'error');
                } finally {
                    if (card) card.classList.remove('loading');
                }
            }

            function showCodeError(phone, hint) {
                purchasesState.codePurchaseId = null;
                dom.codePhone.textContent = phone || '—';
                dom.codeValue.textContent = '—';
                dom.codeModalTitle.textContent = '⚠️ Код не найден';
                const body = document.getElementById('codeModalBody');
                if (body) {
                    body.innerHTML = `
                        <div class="code-phone">${escapeHtml(phone || '')}</div>
                        <div class="code-error">${escapeHtml(hint)}</div>
                    `;
                }
                dom.codeModal.classList.remove('hidden');
            }

            async function downloadPurchaseFile(purchaseId, kind, card) {
                if (card) card.classList.add('loading');
                try {
                    // Передаём initData в query — send_file не получает заголовки
                    // от <a download>, поэтому кладём подпись в URL.
                    const url = `/api/purchases/${purchaseId}/${kind}` +
                        (state.initData
                            ? `?initData=${encodeURIComponent(state.initData)}`
                            : '');
                    const a = document.createElement('a');
                    a.href = url;
                    a.target = '_blank';
                    a.rel = 'noopener';
                    document.body.appendChild(a);
                    a.click();
                    setTimeout(() => document.body.removeChild(a), 0);
                    showToast(kind === 'session' ? '📄 .session отправлен' : '{ } JSON отправлен', 'success');
                } catch (e) {
                    console.error('downloadPurchaseFile error', e);
                    showToast('Ошибка скачивания', 'error');
                } finally {
                    if (card) card.classList.remove('loading');
                }
            }

            /* ===== Page router ===== */
            function switchPage(pageId, opts) {
                opts = opts || {};
                const pages = ['pageCatalog', 'pageProfile', 'pagePurchases', 'pageTopup'];
                pages.forEach((p) => {
                    const el = document.getElementById(p);
                    if (!el) return;
                    el.classList.toggle('active', p === pageId);
                });
                state.currentPage = pageId;
                // nav highlight
                document.querySelectorAll('.nav-btn').forEach((b) => {
                    b.classList.toggle('active', b.dataset.page === pageId);
                });
                // back button в tg
                if (tg) {
                    if (pageId === 'pageCatalog') {
                        tg.BackButton.hide();
                    } else {
                        tg.BackButton.show();
                    }
                }
                window.scrollTo({ top: 0, behavior: 'smooth' });
                // при открытии профиля — обновим баланс
                if (pageId === 'pageProfile' && opts.refreshBalance !== false) {
                    refreshBalance({ silent: false });
                }
                // при открытии «Мои покупки» — подтянем список
                if (pageId === 'pagePurchases') {
                    loadPurchases();
                }
            }

            /* ===== User render (шапка: аватарка + ник + баланс справа) ===== */
            function renderUser() {
                const av = document.getElementById('userAvatar');
                const fb = document.getElementById('avatarFallback');
                const nameEl = document.getElementById('userName');
                const metaEl = document.getElementById('userMeta');
                if (!av) return;
                av.innerHTML = '';
                const u = state.tgUser;

                if (!u) {
                    const fallback = document.createElement('span');
                    fallback.className = 'avatar-fallback';
                    fallback.textContent = '👤';
                    av.appendChild(fallback);
                    if (nameEl) nameEl.textContent = 'Гость';
                    if (metaEl) metaEl.textContent = 'нажмите, чтобы открыть профиль';
                    return;
                }

                const initial = (u.first_name || u.username || '?').charAt(0).toUpperCase();
                const fallbackEl = document.createElement('span');
                fallbackEl.className = 'avatar-fallback';
                fallbackEl.textContent = initial;
                av.appendChild(fallbackEl);

                if (u.photo_url) {
                    const img = document.createElement('img');
                    img.src = u.photo_url;
                    img.alt = initial;
                    img.onload = () => {
                        fallbackEl.style.display = 'none';
                        av.appendChild(img);
                    };
                    img.onerror = () => {
                        // если Telegram не отдал фото — остаётся initial
                    };
                }

                // Ник — приоритет username, иначе first_name
                if (nameEl) {
                    if (u.username) {
                        nameEl.textContent = '@' + u.username;
                    } else if (u.first_name) {
                        nameEl.textContent = u.first_name;
                    } else {
                        nameEl.textContent = 'Пользователь';
                    }
                }
                if (metaEl) {
                    metaEl.textContent = 'нажмите, чтобы открыть профиль';
                }
            }

            function renderProfileAvatar() {
                const u = state.tgUser;
                dom.profileAvatar.innerHTML = '';
                const av = document.createElement('div');
                av.className = 'avatar';
                const initial = (u && (u.first_name || u.username || '?').charAt(0) || '?').toUpperCase();
                av.innerHTML = `<span class="avatar-fallback">${escapeHtml(initial)}</span>`;
                if (u && u.photo_url) {
                    const img = document.createElement('img');
                    img.src = u.photo_url;
                    img.onload = () => {
                        av.querySelector('.avatar-fallback').style.display = 'none';
                        av.appendChild(img);
                    };
                }
                dom.profileAvatar.appendChild(av);

                if (u) {
                    const name = [u.first_name, u.last_name].filter(Boolean).join(' ') || 'Без имени';
                    dom.profileName.textContent = name;
                    dom.profileUsername.textContent = u.username ? '@' + u.username : 'id ' + u.id;
                }
            }

            /* ===== API ===== */
            async function api(path, options = {}) {
                const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
                if (state.initData) headers['X-Init-Data'] = state.initData;
                try {
                    const resp = await fetch(path, { ...options, headers });
                    const data = await resp.json().catch(() => ({ ok: false, error: 'bad json' }));
                    return { ok: resp.ok && data.ok, status: resp.status, data };
                } catch (err) {
                    return { ok: false, error: err.message };
                }
            }

            /* ===== Auth + balance ===== */
            async function auth() {
                if (!state.initData) return { ok: false };
                const r = await api('/api/auth', {
                    method: 'POST',
                    body: JSON.stringify({ initData: state.initData }),
                });
                if (r.ok && r.data.user) {
                    setBalanceUI(r.data.user.balance || 0);
                }
                return r;
            }

            async function refreshBalance(opts) {
                opts = opts || {};
                if (opts.silent === undefined) opts.silent = true;
                if (!opts.silent) {
                    dom.balancePill.classList.add('refreshing');
                }
                const r = await api('/api/balance');
                if (r.ok && r.data) {
                    setBalanceUI(r.data.balance || 0, {
                        hold: r.data.hold_balance,
                        syncedAt: r.data.synced_at,
                    });
                    if (r.data.user) fillProfileStats(r.data.user);
                }
                if (!opts.silent) {
                    dom.balancePill.classList.remove('refreshing');
                }
                return r;
            }

            function setBalanceUI(value, extra) {
                const v = Number(value) || 0;
                dom.balanceValue.textContent = formatRub(v);
                dom.profileBalance.textContent = formatRub(v);
                if (extra && typeof extra.hold !== 'undefined' && extra.hold !== null) {
                    dom.profileHold.textContent = formatRub(extra.hold);
                }
            }

            function fillProfileStats(u) {
                if (!u) return;
                if (typeof u.balance !== 'undefined') {
                    dom.profileBalance.textContent = formatRub(u.balance);
                    dom.balanceValue.textContent = formatRub(u.balance);
                }
                if (typeof u.hold_balance !== 'undefined') {
                    dom.profileHold.textContent = formatRub(u.hold_balance);
                }
                if (typeof u.rating !== 'undefined') {
                    dom.profileRating.innerHTML = (Number(u.rating) || 5).toFixed(1) + ' <span class="small">★</span>';
                }
                if (typeof u.reviews_count !== 'undefined') {
                    dom.profileReviews.textContent = u.reviews_count;
                }
                if (typeof u.total_spent !== 'undefined') {
                    dom.profileSpent.textContent = formatRub(u.total_spent);
                }
                if (typeof u.total_earned !== 'undefined') {
                    dom.profileEarned.textContent = formatRub(u.total_earned);
                }
            }

            /* ===== Каталог + Фильтры ===== */
            const COUNTRY_FLAGS_MAP = {
                'США': '🇺🇸', 'Россия': '🇷🇺', 'Индия': '🇮🇳', 'Германия': '🇩🇪',
                'Бразилия': '🇧🇷', 'Индонезия': '🇮🇩', 'Казахстан': '🇰🇿',
                'Украина': '🇺🇦', 'Беларусь': '🇧🇾', 'Вьетнам': '🇻🇳',
                'Филиппины': '🇵🇭', 'Мьянма': '🇲🇲', 'Мексика': '🇲🇽',
                'Турция': '🇹🇷', 'Польша': '🇵🇱', 'Великобритания': '🇬🇧',
                'Аргентина': '🇦🇷',
            };
            const COUNTRY_LIST = [
                'США','Россия','Индия','Германия','Бразилия','Индонезия',
                'Казахстан','Украина','Беларусь','Вьетнам','Филиппины','Мьянма',
                'Мексика','Турция','Польша','Великобритания','Аргентина',
            ];
            const ORIGIN_OPTIONS = [
                { key: 'all',      emoji: '✨', label: 'Любое' },
                { key: 'Авторег',  emoji: '🤖', label: 'Авторег' },
                { key: 'Саморег',  emoji: '👤', label: 'Саморег' },
                { key: 'Фишинг',   emoji: '🎣', label: 'Фишинг' },
                { key: 'Стиллер',  emoji: '🕵️', label: 'Стиллер' },
            ];
            const PRICE_OPTIONS = [
                { key: 'default', emoji: '✨', label: 'По умолчанию' },
                { key: 'asc',     emoji: '⬆️', label: 'Сначала дешевые' },
                { key: 'desc',    emoji: '⬇️', label: 'Сначала дорогие' },
                { key: 'new',     emoji: '🆕', label: 'Новые' },
            ];

            async function loadCategories() {
                const r = await api('/api/categories');
                if (!r.ok) return;
                state.categories = r.data.categories || [];
                renderFilterModal();
            }
            async function loadCatalog() {
                showLoader(true);
                const params = new URLSearchParams();
                if (state.country && state.country !== 'all') params.set('country', state.country);
                if (state.origin && state.origin !== 'all') params.set('origin', state.origin);
                if (state.priceSort && state.priceSort !== 'default') params.set('sort', state.priceSort);
                params.set('limit', '100');

                const r = await api('/api/catalog?' + params.toString());
                showLoader(false);
                if (!r.ok) {
                    dom.catalog.innerHTML = '';
                    showEmpty(true);
                    return;
                }
                state.catalog = r.data.items || [];
                renderCatalog();
                updateFilterSummary();
            }

            /* Рендер сетки фильтров в модалке — всё видно без скролла */
            function renderFilterModal() {
                // Страны: «Все» + полный список 17 стран в 3 колонки
                if (dom.filtersCountryGrid) {
                    dom.filtersCountryGrid.innerHTML = '';
                    const allBtn = document.createElement('button');
                    allBtn.type = 'button';
                    allBtn.className = 'filter-chip' + (state.country === 'all' ? ' active' : '');
                    allBtn.dataset.country = 'all';
                    allBtn.innerHTML = '<span class="chip-emoji">🌐</span><span>Все</span>';
                    dom.filtersCountryGrid.appendChild(allBtn);
                    // Если в БД есть категории — покажем их; иначе полный дефолтный список
                    const list = state.categories.length
                        ? state.categories.map((c) => c.country)
                        : COUNTRY_LIST;
                    list.forEach((country) => {
                        const flag = COUNTRY_FLAGS_MAP[country] || '🌍';
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'filter-chip' + (state.country === country ? ' active' : '');
                        btn.dataset.country = country;
                        btn.innerHTML = `<span class="chip-emoji">${flag}</span><span>${escapeHtml(country)}</span>`;
                        dom.filtersCountryGrid.appendChild(btn);
                    });
                }
                // Происхождение
                if (dom.filtersOriginGrid) {
                    dom.filtersOriginGrid.innerHTML = '';
                    ORIGIN_OPTIONS.forEach((o) => {
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'filter-chip' + (state.origin === o.key ? ' active' : '');
                        btn.dataset.origin = o.key;
                        btn.innerHTML = `<span class="chip-emoji">${o.emoji}</span><span>${escapeHtml(o.label)}</span>`;
                        dom.filtersOriginGrid.appendChild(btn);
                    });
                }
                // Цена / сортировка
                if (dom.filtersPriceGrid) {
                    dom.filtersPriceGrid.innerHTML = '';
                    PRICE_OPTIONS.forEach((p) => {
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'filter-chip' + (state.priceSort === p.key ? ' active' : '');
                        btn.dataset.sort = p.key;
                        btn.innerHTML = `<span class="chip-emoji">${p.emoji}</span><span>${escapeHtml(p.label)}</span>`;
                        dom.filtersPriceGrid.appendChild(btn);
                    });
                }
            }

            function updateFilterSummary() {
                if (!dom.filterSummary) return;
                const country = state.country === 'all' ? 'Все страны' : state.country;
                const origin = state.origin === 'all'
                    ? 'Любое'
                    : (ORIGIN_OPTIONS.find((o) => o.key === state.origin) || {}).label || state.origin;
                const priceLbl = (PRICE_OPTIONS.find((p) => p.key === state.priceSort) || {}).label || '';
                const isDefault = state.country === 'all' && state.origin === 'all' && state.priceSort === 'default';
                if (isDefault) {
                    dom.filterSummary.textContent = 'Все страны · Любое · По умолчанию';
                } else {
                    dom.filterSummary.textContent = `${country} · ${origin} · ${priceLbl}`;
                }
                // Бейдж со счётчиком активных фильтров
                if (dom.filterBadge) {
                    const active = (state.country !== 'all' ? 1 : 0)
                        + (state.origin !== 'all' ? 1 : 0)
                        + (state.priceSort !== 'default' ? 1 : 0);
                    if (active > 0) {
                        dom.filterBadge.textContent = String(active);
                        dom.filterBadge.hidden = false;
                    } else {
                        dom.filterBadge.hidden = true;
                    }
                }
            }

            function renderCatalog() {
                dom.catalog.innerHTML = '';
                dom.catListCount.textContent = state.catalog.length;
                if (state.catalog.length === 0) { showEmpty(true); return; }
                showEmpty(false);
                const frag = document.createDocumentFragment();
                state.catalog.forEach((it) => {
                    const card = document.createElement('div');
                    card.className = 'card';
                    card.innerHTML = `
                        <div class="card-flag">${it.flag}</div>
                        <div class="card-country">${escapeHtml(it.country)}</div>
                        <span class="card-origin">${it.origin_icon} ${escapeHtml(it.origin_label)}</span>
                        <div class="card-preview">${escapeHtml(it.preview || '—')}</div>
                        <div class="card-price">${formatPrice(it.price)}<span class="rub">₽</span></div>
                    `;
                    card.addEventListener('click', () => openItem(it));
                    frag.appendChild(card);
                });
                dom.catalog.appendChild(frag);
            }

            function openFiltersModal() {
                renderFilterModal();
                openModal('filtersModal');
            }

            function applyFiltersFromModal() {
                // Считываем выбор из DOM (могли покликать до нажатия «Применить»)
                if (dom.filtersCountryGrid) {
                    const sel = dom.filtersCountryGrid.querySelector('.filter-chip.active');
                    state.country = sel ? sel.dataset.country : 'all';
                }
                if (dom.filtersOriginGrid) {
                    const sel = dom.filtersOriginGrid.querySelector('.filter-chip.active');
                    state.origin = sel ? sel.dataset.origin : 'all';
                }
                if (dom.filtersPriceGrid) {
                    const sel = dom.filtersPriceGrid.querySelector('.filter-chip.active');
                    state.priceSort = sel ? sel.dataset.sort : 'default';
                }
                closeModal('filtersModal');
                loadCatalog();
            }

            function resetFilters() {
                state.country = 'all';
                state.origin = 'all';
                state.priceSort = 'default';
                renderFilterModal();
            }

            /* Клики внутри модалки фильтров — переключаем active */
            function bindFilterGrids() {
                if (dom.filtersCountryGrid) {
                    dom.filtersCountryGrid.addEventListener('click', (e) => {
                        const btn = e.target.closest('.filter-chip');
                        if (!btn) return;
                        dom.filtersCountryGrid.querySelectorAll('.filter-chip').forEach((b) => b.classList.remove('active'));
                        btn.classList.add('active');
                    });
                }
                if (dom.filtersOriginGrid) {
                    dom.filtersOriginGrid.addEventListener('click', (e) => {
                        const btn = e.target.closest('.filter-chip');
                        if (!btn) return;
                        dom.filtersOriginGrid.querySelectorAll('.filter-chip').forEach((b) => b.classList.remove('active'));
                        btn.classList.add('active');
                    });
                }
                if (dom.filtersPriceGrid) {
                    dom.filtersPriceGrid.addEventListener('click', (e) => {
                        const btn = e.target.closest('.filter-chip');
                        if (!btn) return;
                        dom.filtersPriceGrid.querySelectorAll('.filter-chip').forEach((b) => b.classList.remove('active'));
                        btn.classList.add('active');
                    });
                }
            }
            /* Текущий выбранный товар для покупки */
            const buyState = { item: null, busy: false };

            function openItem(it) {
                buyState.item = it;
                dom.itemFlag.textContent = it.flag;
                dom.itemCountry.textContent = it.country;
                dom.itemOrigin.textContent = `${it.origin_icon} ${it.origin_label} · ${it.preview || ''}`;

                // Цена + никнейм продавца + рейтинг
                const priceNum = Number(it.price) || 0;
                dom.itemPrice.textContent = formatPrice(priceNum) + ' ₽';

                const sellerName = it.seller_username
                    ? '@' + it.seller_username
                    : (it.seller_id ? 'id ' + it.seller_id : 'Платформа');
                const sellerInitial = (sellerName.replace('@', '').replace('id ', '') || '?').charAt(0).toUpperCase();
                const avatar = document.getElementById('itemSellerAvatar');
                if (avatar) avatar.textContent = sellerInitial;
                if (dom.itemSeller) dom.itemSeller.textContent = sellerName;

                const rating = Number(it.seller_rating);
                const reviews = Number(it.seller_reviews) || 0;
                if (dom.itemRating) {
                    if (rating && rating > 0) {
                        dom.itemRating.textContent = `${rating.toFixed(1)} ★ (${reviews} отзывов)`;
                    } else {
                        dom.itemRating.textContent = 'Новый продавец';
                    }
                }

                // Описание
                if (dom.itemDescription) {
                    const desc = (it.description && String(it.description).trim())
                        ? it.description
                        : 'Аккаунт прошёл проверку. Сессия валидна, выдача мгновенно после оплаты.';
                    dom.itemDescription.textContent = desc;
                }

                // Кнопка «Купить» — показываем актуальную цену
                if (dom.buyBtn) {
                    dom.buyBtn.textContent = `Купить за ${formatPrice(priceNum)} ₽`;
                    dom.buyBtn.disabled = false;
                }

                openModal('itemModal');
            }

            async function buyCurrentItem() {
                if (buyState.busy || !buyState.item) return;
                buyState.busy = true;
                const oldText = dom.buyBtn ? dom.buyBtn.textContent : '';
                if (dom.buyBtn) {
                    dom.buyBtn.disabled = true;
                    dom.buyBtn.textContent = 'Покупаем…';
                }
                try {
                    const r = await api('/api/buy', {
                        method: 'POST',
                        body: JSON.stringify({ account_id: buyState.item.id }),
                    });
                    if (r.ok) {
                        showToast('✅ Покупка успешна!', 'success');
                        // Обновим баланс и каталог
                        if (typeof r.data.balance !== 'undefined') {
                            setBalanceUI(r.data.balance, {
                                hold: r.data.hold_balance,
                                syncedAt: r.data.synced_at,
                            });
                        } else {
                            refreshBalance({ silent: true });
                        }
                        closeModal('itemModal');
                        loadCatalog();
                    } else {
                        const err = (r.data && r.data.error) || 'unknown';
                        showToast(translateBuyError(err), 'error');
                        if (dom.buyBtn) {
                            dom.buyBtn.disabled = false;
                            dom.buyBtn.textContent = oldText || 'Купить';
                        }
                    }
                } catch (e) {
                    showToast('Ошибка сети', 'error');
                    if (dom.buyBtn) {
                        dom.buyBtn.disabled = false;
                        dom.buyBtn.textContent = oldText || 'Купить';
                    }
                } finally {
                    buyState.busy = false;
                }
            }

            function translateBuyError(err) {
                const map = {
                    'unauthorized': '❌ Не авторизовано',
                    'not_found': '❌ Аккаунт уже купили',
                    'already_sold': '❌ Аккаунт уже продан',
                    'low_balance': '❌ Недостаточно средств — пополните баланс',
                    'self_buy': '❌ Нельзя купить свой аккаунт',
                    'server_error': '❌ Ошибка сервера, попробуйте позже',
                };
                return map[err] || ('❌ ' + err);
            }
            function showLoader(v) { dom.loader.classList.toggle('hidden', !v); }
            function showEmpty(v) { dom.emptyState.classList.toggle('hidden', !v); }

            /* ===== Модалки ===== */
            function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
            function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

            /* ===== Профиль ===== */
            async function openProfile() {
                renderProfileAvatar();
                switchPage('pageProfile');
                // данные подтянем в switchPage
            }

            /* ===== Пополнение ===== */
            async function openTopup() {
                switchPage('pageTopup');
                // пытаемся узнать username бота для кнопок оплаты
                if (!state.botUsername) {
                    api('/api/bot-info').then((r) => {
                        if (r.ok && r.data.username) {
                            state.botUsername = r.data.username;
                        }
                    });
                }
            }

            function activateTopupMethod(method) {
                // Параметр start для deeplink
                const startArg = 'deposit_' + method;
                let url;
                if (state.botUsername) {
                    url = 'https://t.me/' + state.botUsername + '?start=' + startArg;
                } else {
                    // fallback — открываем бота без аргумента
                    url = 'https://t.me/';
                }

                if (tg && tg.openTelegramLink) {
                    tg.openTelegramLink(url);
                } else {
                    window.open(url, '_blank');
                }
                showToast('Открываем бота…', 'success');
            }

            function translatePromoError(err) {
                const map = {
                    'not_found': '❌ Промокод не найден или неактивен',
                    'exhausted': '❌ Промокод уже использован',
                    'already_used': '❌ Вы уже активировали этот промокод',
                    'unauthorized': '❌ Не авторизовано — откройте из Telegram',
                    'no_code': 'Введите промокод',
                };
                return map[err] || ('❌ ' + err);
            }

            /* ===== Активация промокода прямо из профиля (общая БД с ботом) ===== */
            /* (Промокод со страницы пополнения убран — остаётся только в профиле) */
            async function activatePromoFromProfile() {
                const code = (dom.profilePromoInput.value || '').trim().toUpperCase();
                if (!code) {
                    showProfilePromoMsg('Введите промокод', 'error');
                    return;
                }
                dom.profilePromoBtn.disabled = true;
                const oldText = dom.profilePromoBtn.textContent;
                dom.profilePromoBtn.textContent = '…';
                try {
                    const r = await api('/api/promo/redeem', {
                        method: 'POST',
                        body: JSON.stringify({ code: code }),
                    });
                    if (r.ok) {
                        showProfilePromoMsg(
                            '✅ Готово! Зачислено: +' + formatRub(r.data.amount) + ' ₽',
                            'success'
                        );
                        showToast('+' + formatRub(r.data.amount) + ' ₽ на баланс!', 'success');
                        dom.profilePromoInput.value = '';
                        if (typeof r.data.balance !== 'undefined') {
                            setBalanceUI(r.data.balance, {
                                hold: r.data.hold_balance,
                                syncedAt: r.data.synced_at,
                            });
                        } else {
                            refreshBalance({ silent: true });
                        }
                    } else {
                        const err = (r.data && r.data.error) || 'Не удалось активировать';
                        showProfilePromoMsg(translatePromoError(err), 'error');
                    }
                } catch (e) {
                    showProfilePromoMsg('Ошибка сети. Попробуйте ещё раз.', 'error');
                } finally {
                    dom.profilePromoBtn.disabled = false;
                    dom.profilePromoBtn.textContent = oldText;
                }
            }

            function showProfilePromoMsg(text, type) {
                dom.profilePromoMsg.textContent = text;
                dom.profilePromoMsg.className = 'promo-msg ' + (type || '');
            }

            /* Открыть мини-окно для ввода промокода */
            function openPromoModalFromProfile() {
                // сбрасываем предыдущее сообщение
                showProfilePromoMsg('', '');
                if (dom.profilePromoInput) {
                    // чуть позже фокус, чтобы анимация успела начаться
                    setTimeout(() => dom.profilePromoInput.focus(), 220);
                }
                openModal('promoModal');
            }

            /* ===== Bot username / support ===== */
            function openSupport() {
                openModal('supportModal');
            }
            function openBotChat() {
                const url = state.botUsername
                    ? 'https://t.me/' + state.botUsername
                    : 'https://t.me/';
                if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
                else window.open(url, '_blank');
            }

            /* ===== Bind events ===== */
            function bindEvents() {
                // Кнопка «Фильтры» — открывает модалку с гридами стран/происхождения/цены
                if (dom.openFilters) {
                    dom.openFilters.addEventListener('click', openFiltersModal);
                }
                if (dom.filtersApply) {
                    dom.filtersApply.addEventListener('click', applyFiltersFromModal);
                }
                if (dom.filtersReset) {
                    dom.filtersReset.addEventListener('click', resetFilters);
                }
                bindFilterGrids();

                // Header balance pill -> profile
                dom.balancePill.addEventListener('click', openProfile);

                // Bottom nav
                document.querySelectorAll('.nav-btn').forEach((btn) => {
                    btn.addEventListener('click', () => {
                        const page = btn.dataset.page;
                        if (page === 'pageProfile') openProfile();
                        else switchPage(page);
                    });
                });

                // Profile back / topup
                dom.profileBack.addEventListener('click', () => switchPage('pageCatalog'));
                dom.topupBack.addEventListener('click', () => switchPage('pageProfile'));
                dom.purchasesBack.addEventListener('click', () => switchPage('pageCatalog'));
                if (dom.codeModalRefresh) {
                    dom.codeModalRefresh.addEventListener('click', () => {
                        const pid = purchasesState.codePurchaseId;
                        if (pid) fetchPurchaseCode(pid);
                    });
                }
                dom.topupBtn.addEventListener('click', openTopup);
                dom.openTopup2.addEventListener('click', openTopup);
                dom.openSupport.addEventListener('click', openSupport);
                dom.openBot.addEventListener('click', openBotChat);
                if (dom.openPurchasesFromProfile) {
                    dom.openPurchasesFromProfile.addEventListener('click', () => switchPage('pagePurchases'));
                }

                // Topup methods
                document.querySelectorAll('.topup-method').forEach((btn) => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('.topup-method').forEach((b) => b.classList.remove('active'));
                        btn.classList.add('active');
                        activateTopupMethod(btn.dataset.method);
                    });
                });

                // Promo (в профиле остаётся; со страницы пополнения убран)
                if (dom.openPromoModal) {
                    dom.openPromoModal.addEventListener('click', openPromoModalFromProfile);
                }
                dom.profilePromoBtn.addEventListener('click', activatePromoFromProfile);
                dom.profilePromoInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') activatePromoFromProfile();
                });
                dom.profilePromoInput.addEventListener('input', (e) => {
                    e.target.value = e.target.value.toUpperCase();
                });

                // Support
                dom.supportBtn.addEventListener('click', () => {
                    const url = 'https://t.me/VestGameSupport';
                    if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
                    else window.open(url, '_blank');
                });

                // Кнопка «Купить» в модалке товара
                if (dom.buyBtn) {
                    dom.buyBtn.addEventListener('click', buyCurrentItem);
                }

                // Modal close
                document.addEventListener('click', (e) => {
                    const closer = e.target.closest('[data-close]');
                    if (closer) closeModal(closer.dataset.close);
                });

                // TG back button
                if (tg && tg.BackButton) {
                    tg.BackButton.onClick(() => {
                        if (state.currentPage === 'pageTopup') switchPage('pageProfile');
                        else if (state.currentPage === 'pageProfile') switchPage('pageCatalog');
                    });
                }

                // Refresh balance при возвращении из фона (юзер вышел в бот пополнить — вернулся)
                document.addEventListener('visibilitychange', () => {
                    if (document.visibilityState === 'visible') {
                        refreshBalance({ silent: true });
                    }
                });
                window.addEventListener('focus', () => {
                    refreshBalance({ silent: true });
                });

                // Периодический рефреш баланса (каждые 30 сек пока апппа открыта)
                setInterval(() => {
                    if (document.visibilityState === 'visible') {
                        refreshBalance({ silent: true });
                    }
                }, 30000);
            }

            async function bootstrap() {
                renderUser();
                bindEvents();

                await auth();

                // Грузим всё параллельно
                await Promise.all([loadCategories(), loadCatalog()]);
                updateFilterSummary();

                // Узнаём username бота
                api('/api/bot-info').then((r) => {
                    if (r.ok && r.data.username) state.botUsername = r.data.username;
                });
            }

            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', bootstrap);
            } else {
                bootstrap();
            }
        })();
    </script>
</body>
</html>
"""


# ===== ROUTES =====
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/api/auth", methods=["POST"])
def api_auth():
    """Валидация initData + апсерт юзера в БД."""
    payload = request.get_json(silent=True) or {}
    init_data = payload.get("initData", "")
    validated = validate_init_data(init_data)
    if not validated or not validated.get("user_obj"):
        return jsonify({"ok": False, "error": "invalid initData"}), 401

    tg_user = validated["user_obj"]
    tg_id = tg_user["id"]

    session = SessionLocal()
    try:
        db_user = session.execute(
            select(User).where(User.telegram_id == tg_id)
        ).scalar_one_or_none()
        if not db_user:
            # Автосоздание юзера при первом входе.
            # В схеме bot.py нет колонок first_name / last_name / photo_url,
            # поэтому пишем ТОЛЬКО то, что реально есть в таблице.
            db_user = User(
                telegram_id=tg_id,
                username=tg_user.get("username"),
            )
            session.add(db_user)
        else:
            # Обновляем только те поля, которые есть в реальной схеме.
            new_username = tg_user.get("username")
            if new_username and new_username != db_user.username:
                db_user.username = new_username
        session.commit()
        # Имя/аватар берём из Telegram WebApp (initDataUnsafe.user),
        # а не из БД — так профиль всегда актуален.
        return jsonify({
            "ok": True,
            "user": {
                "id": tg_id,
                "username": db_user.username,
                "first_name": tg_user.get("first_name"),
                "last_name": tg_user.get("last_name"),
                "photo_url": tg_user.get("photo_url"),
                "balance": db_user.balance,
                "is_admin": db_user.is_admin,
            },
        })
    finally:
        session.close()


@app.route("/api/balance")
@require_auth
def api_balance(telegram_id, tg_user):
    """
    Быстрый endpoint для получения ТЕКУЩЕГО баланса из общей БД.
    Баланс хранится в таблице users.balance — той же, что использует bot.py,
    поэтому при пополнении через бота значение подхватится автоматически.
    """
    session = SessionLocal()
    try:
        db_user = session.execute(
            select(User).where(User.telegram_id == telegram_id)
        ).scalar_one_or_none()
        if not db_user:
            # Автосоздание, если юзер пришёл только через мини-апп.
            # Поля — строго по реальной схеме (см. bot.py).
            db_user = User(
                telegram_id=telegram_id,
                username=tg_user.get("username"),
            )
            session.add(db_user)
            session.commit()
            session.refresh(db_user)
        return jsonify({
            "ok": True,
            "balance": float(db_user.balance or 0.0),
            "hold_balance": float(db_user.hold_balance or 0.0),
            "total_spent": float(db_user.total_spent or 0.0),
            "total_earned": float(db_user.total_earned or 0.0),
            "rating": float(db_user.rating or 5.0),
            "reviews_count": int(db_user.reviews_count or 0),
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "user": {
                "balance": float(db_user.balance or 0.0),
                "hold_balance": float(db_user.hold_balance or 0.0),
                "total_spent": float(db_user.total_spent or 0.0),
                "total_earned": float(db_user.total_earned or 0.0),
                "rating": float(db_user.rating or 5.0),
                "reviews_count": int(db_user.reviews_count or 0),
            },
        })
    finally:
        session.close()


@app.route("/api/me")
@require_auth
def api_me(telegram_id, tg_user):
    """Полный профиль текущего юзера."""
    session = SessionLocal()
    try:
        db_user = session.execute(
            select(User).where(User.telegram_id == telegram_id)
        ).scalar_one_or_none()
        if not db_user:
            return jsonify({"ok": False, "error": "user not found"}), 404
        return jsonify({
            "ok": True,
            "user": {
                "id": db_user.telegram_id,
                "username": db_user.username,
                # Имя/аватар — из Telegram WebApp (в БД этих колонок нет).
                "first_name": tg_user.get("first_name"),
                "last_name": tg_user.get("last_name"),
                "photo_url": tg_user.get("photo_url"),
                "balance": db_user.balance,
                "hold_balance": db_user.hold_balance,
                "total_spent": db_user.total_spent,
                "total_earned": db_user.total_earned,
                "rating": db_user.rating,
                "reviews_count": db_user.reviews_count,
                "is_admin": db_user.is_admin,
            },
        })
    finally:
        session.close()


# ===== API: МОИ ПОКУПКИ =====
#
# Идентично логике из bot.py: список покупок юзера + выдача
# кода подтверждения / .session / JSON по id покупки.
# Все запросы проходят require_auth (initData → telegram_id),
# и каждый фильтрует покупки по user_id, чтобы чужой код не ушёл.


@app.route("/api/purchases")
@require_auth
def api_purchases(telegram_id, tg_user):
    """Список покупок текущего юзера (от новых к старым)."""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(Purchase)
            .where(Purchase.user_id == telegram_id)
            .order_by(Purchase.created_at.desc())
        ).scalars().all()

        items = []
        for p in rows:
            account = session.execute(
                select(Account).where(Account.id == p.account_id)
            ).scalar_one_or_none()
            items.append({
                "id": p.id,
                "amount": float(p.amount or 0.0),
                "payment_method": p.payment_method,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "phone": account.phone if account else None,
                "country": account.country if account else None,
                "has_session": bool(account and account.session_string),
            })
        return jsonify({"ok": True, "items": items, "count": len(items)})
    finally:
        session.close()


@app.route("/api/purchases/<int:purchase_id>/code", methods=["GET", "POST"])
@require_auth
def api_purchase_code(telegram_id, tg_user, purchase_id):
    """Получить код подтверждения из диалогов аккаунта покупки."""
    session = SessionLocal()
    try:
        purchase = session.execute(
            select(Purchase).where(Purchase.id == purchase_id)
        ).scalar_one_or_none()

        if not purchase:
            return jsonify({"ok": False, "error": "purchase_not_found"}), 404
        if purchase.user_id != telegram_id:
            return jsonify({"ok": False, "error": "not_your_purchase"}), 403

        account = session.execute(
            select(Account).where(Account.id == purchase.account_id)
        ).scalar_one_or_none()
        if not account or not account.session_string:
            return jsonify({"ok": False, "error": "no_session"}), 404

        code = get_code_from_session(account.session_string, account.phone)
        if not code:
            return jsonify({
                "ok": False,
                "error": "code_not_found",
                "phone": account.phone,
                "hint": "Подождите 1–2 минуты и попробуйте снова. "
                        "Либо скачайте .session файл и войдите вручную.",
            }), 404

        return jsonify({
            "ok": True,
            "phone": account.phone,
            "code": code,
            "country": account.country,
        })
    finally:
        session.close()


@app.route("/api/purchases/<int:purchase_id>/session", methods=["GET", "POST"])
@require_auth
def api_purchase_session(telegram_id, tg_user, purchase_id):
    """Отдать .session файл (строка сессии в .txt с именем <phone>.session)."""
    session = SessionLocal()
    try:
        purchase = session.execute(
            select(Purchase).where(Purchase.id == purchase_id)
        ).scalar_one_or_none()

        if not purchase or purchase.user_id != telegram_id:
            return jsonify({"ok": False, "error": "not_found"}), 404

        account = session.execute(
            select(Account).where(Account.id == purchase.account_id)
        ).scalar_one_or_none()
        if not account or not account.session_string:
            return jsonify({"ok": False, "error": "no_session"}), 404

        session_bytes = account.session_string.encode("utf-8")
        buf = io.BytesIO(session_bytes)
        filename = f"{account.phone}.session"
        return send_file(
            buf,
            as_attachment=True,
            download_name=filename,
            mimetype="text/plain",
        )
    finally:
        session.close()


@app.route("/api/purchases/<int:purchase_id>/json", methods=["GET", "POST"])
@require_auth
def api_purchase_json(telegram_id, tg_user, purchase_id):
    """Отдать JSON с данными сессии (phone, session_string, api_id, api_hash...)."""
    session = SessionLocal()
    try:
        purchase = session.execute(
            select(Purchase).where(Purchase.id == purchase_id)
        ).scalar_one_or_none()

        if not purchase or purchase.user_id != telegram_id:
            return jsonify({"ok": False, "error": "not_found"}), 404

        account = session.execute(
            select(Account).where(Account.id == purchase.account_id)
        ).scalar_one_or_none()
        if not account:
            return jsonify({"ok": False, "error": "no_account"}), 404

        # Если готового session_json нет (старые записи) — собираем на лету
        # из session_string + API_ID/API_HASH, чтобы юзер всегда получил файл.
        if not account.session_json:
            session_json_str = json.dumps({
                "phone": account.phone,
                "session_string": account.session_string,
                "api_id": API_ID,
                "api_hash": API_HASH,
                "country": account.country,
            }, ensure_ascii=False, indent=2)
        else:
            session_json_str = account.session_json

        json_bytes = session_json_str.encode("utf-8")
        buf = io.BytesIO(json_bytes)
        filename = f"{account.phone}_session.json"
        return send_file(
            buf,
            as_attachment=True,
            download_name=filename,
            mimetype="application/json",
        )
    finally:
        session.close()


@app.route("/api/promo/redeem", methods=["POST"])
@require_auth
def api_promo_redeem(telegram_id, tg_user):
    """
    Активация промокода (логика идентична боту, пишем в ту же таблицу
    promo_usages — счётчик used_count инкрементируется атомарно).
    """
    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "no_code"}), 400

    session = SessionLocal()
    try:
        # Блокируем строку промокода, чтобы конкурентные активации не
        # превысили max_uses (FOR UPDATE через with_for_update).
        promo = session.execute(
            select(PromoCode)
            .where(PromoCode.code == code)
            .with_for_update()
        ).scalar_one_or_none()

        if not promo or not promo.is_active:
            return jsonify({"ok": False, "error": "not_found"}), 404
        if (promo.used_count or 0) >= (promo.max_uses or 0):
            return jsonify({"ok": False, "error": "exhausted"}), 409

        # Уже использовал этот юзер?
        already = session.execute(
            select(PromoUsage).where(
                PromoUsage.user_id == telegram_id,
                PromoUsage.promo_id == promo.id,
            )
        ).scalar_one_or_none()
        if already:
            return jsonify({"ok": False, "error": "already_used"}), 409

        # Активируем
        promo.used_count = (promo.used_count or 0) + 1
        session.add(PromoUsage(user_id=telegram_id, promo_id=promo.id))

        # Начисляем баланс
        db_user = session.execute(
            select(User).where(User.telegram_id == telegram_id)
            .with_for_update()
        ).scalar_one_or_none()
        if not db_user:
            db_user = User(
                telegram_id=telegram_id,
                username=tg_user.get("username"),
            )
            session.add(db_user)
            session.flush()

        old_balance = float(db_user.balance or 0.0)
        db_user.balance = old_balance + float(promo.amount or 0.0)

        # Логируем платёж (promo = type)
        session.add(Payment(
            user_id=telegram_id,
            amount=float(promo.amount or 0.0),
            payment_id=f"promo_{promo.id}_{telegram_id}_{int(datetime.utcnow().timestamp())}",
            status="completed",
            method="promo",
            type="deposit",
        ))

        session.commit()

        return jsonify({
            "ok": True,
            "code": promo.code,
            "amount": float(promo.amount or 0.0),
            "balance": float(db_user.balance or 0.0),
            "hold_balance": float(db_user.hold_balance or 0.0),
            "old_balance": old_balance,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })
    except IntegrityError:
        session.rollback()
        return jsonify({"ok": False, "error": "already_used"}), 409
    except Exception as e:
        # На случай неожиданной ошибки (например, расхождение схемы) — отдаём
        # JSON, чтобы фронт не показывал странное "bad json".
        try:
            session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "server_error", "detail": str(e)[:200]}), 500
    finally:
        session.close()


@app.route("/api/bot-info")
def api_bot_info():
    """Возвращает username бота (через Telegram getMe) — для deeplink."""
    username = get_bot_username()
    return jsonify({
        "ok": True,
        "username": username,
        "support": "VestGameSupport",
    })


@app.route("/api/catalog")
def api_catalog():
    """Каталог доступных аккаунтов."""
    country = request.args.get("country")
    origin = request.args.get("origin")
    sort = request.args.get("sort", "default")  # default | asc | desc | new
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    session = SessionLocal()
    try:
        q = select(Account).where(
            Account.is_sold == False,
            Account.is_verified == True,
        )
        if country and country != "all":
            q = q.where(Account.country == country)
        if origin and origin != "all":
            q = q.where(Account.origin == origin)

        # Сортировка по цене
        if sort == "asc":
            q = q.order_by(Account.price.asc(), Account.created_at.desc())
        elif sort == "desc":
            q = q.order_by(Account.price.desc(), Account.created_at.desc())
        elif sort == "new":
            q = q.order_by(Account.created_at.desc())
        else:
            # по умолчанию — сначала новые
            q = q.order_by(Account.created_at.desc())

        q = q.limit(limit).offset(offset)
        rows = session.execute(q).scalars().all()

        items = []
        # Соберём заранее продавцов одним запросом (чтобы не делать N+1)
        seller_ids = {a.seller_id for a in rows if a.seller_id}
        sellers_by_id = {}
        if seller_ids:
            seller_rows = session.execute(
                select(User).where(User.telegram_id.in_(seller_ids))
            ).scalars().all()
            sellers_by_id = {u.telegram_id: u for u in seller_rows}

        # Подтянем связанные listing'и (для description/title)
        account_ids = [a.id for a in rows]
        listings_by_account = {}
        if account_ids:
            listing_rows = session.execute(
                select(Listing).where(
                    Listing.account_id.in_(account_ids),
                    Listing.status == "active",
                )
            ).scalars().all()
            for L in listing_rows:
                # если по одному аккаунту несколько объявлений — берём самое свежее
                if L.account_id not in listings_by_account:
                    listings_by_account[L.account_id] = L

        for a in rows:
            origin_key = a.origin if a.origin in ORIGIN_LABELS else "Авторег"
            icon, label = ORIGIN_LABELS[origin_key]
            seller = sellers_by_id.get(a.seller_id) if a.seller_id else None
            listing = listings_by_account.get(a.id)
            items.append({
                "id": a.id,
                "country": a.country,
                "flag": COUNTRY_FLAGS.get(a.country, "🌍"),
                "price": float(a.price or 0),
                "origin": origin_key,
                "origin_icon": icon,
                "origin_label": label,
                "preview": mask_phone(a.phone),
                "created_at": a.created_at.isoformat() if a.created_at else None,
                # продавец
                "seller_id": a.seller_id,
                "seller_username": seller.username if seller else None,
                "seller_rating": float(seller.rating or 5.0) if seller else 5.0,
                "seller_reviews": int(seller.reviews_count or 0) if seller else 0,
                # описание (из связанного listing'а, если есть)
                "description": (listing.description if listing and listing.description else None),
                "title": (listing.title if listing and listing.title else None),
            })
        return jsonify({"ok": True, "items": items, "count": len(items)})
    finally:
        session.close()


@app.route("/api/buy", methods=["POST"])
@require_auth
def api_buy(telegram_id, tg_user):
    """
    Реальная покупка аккаунта.
    Схема покупки — как в bot.py: списываем с баланса, помечаем аккаунт
    проданным, создаём Purchase. Продавцу зачисляем в hold (как в боте).
    """
    payload = request.get_json(silent=True) or {}
    try:
        account_id = int(payload.get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    if account_id <= 0:
        return jsonify({"ok": False, "error": "bad_account_id"}), 400

    session = SessionLocal()
    try:
        # Блокируем аккаунт и покупателя (FOR UPDATE), чтобы при гонках
        # один и тот же аккаунт не купили дважды.
        account = session.execute(
            select(Account).where(Account.id == account_id)
            .with_for_update()
        ).scalar_one_or_none()
        if not account:
            return jsonify({"ok": False, "error": "not_found"}), 404
        if account.is_sold:
            return jsonify({"ok": False, "error": "already_sold"}), 409
        if account.seller_id and account.seller_id == telegram_id:
            return jsonify({"ok": False, "error": "self_buy"}), 403

        price = float(account.price or 0.0)
        if price <= 0:
            return jsonify({"ok": False, "error": "bad_price"}), 400

        buyer = session.execute(
            select(User).where(User.telegram_id == telegram_id)
            .with_for_update()
        ).scalar_one_or_none()
        if not buyer:
            # авто-создание (как в /api/balance)
            buyer = User(
                telegram_id=telegram_id,
                username=tg_user.get("username"),
            )
            session.add(buyer)
            session.flush()

        if float(buyer.balance or 0.0) < price:
            return jsonify({"ok": False, "error": "low_balance"}), 402

        # Списываем с баланса покупателя
        buyer.balance = float(buyer.balance or 0.0) - price
        buyer.total_spent = float(buyer.total_spent or 0.0) + price

        # Помечаем аккаунт проданным
        account.is_sold = True

        # Создаём Purchase (схема как в bot.py)
        purchase = Purchase(
            user_id=telegram_id,
            account_id=account.id,
            listing_id=None,  # покупка напрямую из каталога (не из P2P-объявления)
            amount=price,
            payment_method="balance",
        )
        session.add(purchase)

        # Если есть продавец — кладём выручку в его hold (по аналогии с ботом).
        # Здесь НЕ применяем 7%-комиссию — мини-апп не админка, продавец
        # получает полную сумму в hold и сам распоряжается через бота.
        if account.seller_id:
            seller = session.execute(
                select(User).where(User.telegram_id == account.seller_id)
                .with_for_update()
            ).scalar_one_or_none()
            if seller:
                seller.hold_balance = float(seller.hold_balance or 0.0) + price
                seller.total_earned = float(seller.total_earned or 0.0) + price

        session.commit()

        return jsonify({
            "ok": True,
            "purchase_id": purchase.id,
            "account_id": account.id,
            "amount": price,
            "balance": float(buyer.balance or 0.0),
            "hold_balance": float(buyer.hold_balance or 0.0),
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })
    except IntegrityError:
        session.rollback()
        return jsonify({"ok": False, "error": "already_sold"}), 409
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "server_error", "detail": str(e)[:200]}), 500
    finally:
        session.close()


@app.route("/api/categories")
def api_categories():
    """Список стран с количеством доступных аккаунтов и минимальной ценой."""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(Account)
            .where(Account.is_sold == False, Account.is_verified == True)
        ).scalars().all()

        cats = {}
        for a in rows:
            c = a.country or "Другое"
            if c not in cats:
                cats[c] = {
                    "country": c,
                    "flag": COUNTRY_FLAGS.get(c, "🌍"),
                    "count": 0,
                    "min_price": float(a.price or 0),
                }
            cats[c]["count"] += 1
            cats[c]["min_price"] = min(cats[c]["min_price"], float(a.price or 0))

        return jsonify({
            "ok": True,
            "categories": sorted(cats.values(), key=lambda x: -x["count"]),
        })
    finally:
        session.close()


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "ts": datetime.utcnow().isoformat(),
        "bot_username": get_bot_username(),
    })


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not found"}), 404
    abort(404)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
