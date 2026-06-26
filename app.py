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
import urllib.request
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from functools import wraps

from flask import Flask, request, jsonify, render_template_string, abort
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text,
    create_engine, select,
)
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv

load_dotenv()

# ===== КОНФИГ =====
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
    """Использование промокодов (общая таблица)."""
    __tablename__ = "promo_usages"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    promo_id = Column(Integer, nullable=False)
    used_at = Column(DateTime, default=datetime.utcnow)


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
    """Декоратор: проверяет initData, кладёт telegram_id в kwargs."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        init_data = (
            request.headers.get("X-Init-Data", "")
            or (request.get_json(silent=True) or {}).get("initData", "")
        )
        validated = validate_init_data(init_data)
        if not validated or not validated.get("user_obj"):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        kwargs["telegram_id"] = validated["user_obj"]["id"]
        kwargs["tg_user"] = validated["user_obj"]
        return f(*args, **kwargs)
    return wrapper


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
            <div class="brand-logo">VA</div>
            <div class="user-info">
                <div class="brand-text">Vest Account</div>
                <div class="user-name" id="userName" style="font-size: 14px; margin-top: 2px;">Загрузка…</div>
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
            <div class="section-head">
                <h2 class="section-title">Страны</h2>
                <span class="section-count" id="catCount">0</span>
            </div>
            <div class="cat-scroll" id="catScroll">
                <button class="cat-pill active" data-country="all">
                    <span class="cat-emoji">🌐</span><span>Все</span>
                </button>
            </div>
        </section>

        <section class="section">
            <div class="section-head">
                <h2 class="section-title">Происхождение</h2>
            </div>
            <div class="cat-scroll" id="originScroll">
                <button class="cat-pill active" data-origin="all">
                    <span class="cat-emoji">✨</span><span>Любое</span>
                </button>
                <button class="cat-pill" data-origin="Авторег">
                    <span class="cat-emoji">🤖</span><span>Авторег</span>
                </button>
                <button class="cat-pill" data-origin="Саморег">
                    <span class="cat-emoji">👤</span><span>Саморег</span>
                </button>
                <button class="cat-pill" data-origin="Фишинг">
                    <span class="cat-emoji">🎣</span><span>Фишинг</span>
                </button>
                <button class="cat-pill" data-origin="Стиллер">
                    <span class="cat-emoji">🕵️</span><span>Стиллер</span>
                </button>
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

        <!-- ====== Промокод — прямо в профиле (общая БД с ботом) ====== -->
        <div class="promo-card">
            <div class="promo-card-head">
                <div class="promo-card-icon">🎁</div>
                <div class="promo-card-title">
                    <div class="promo-card-name">Промокод</div>
                    <div class="promo-card-sub">Введите код — баланс пополнится мгновенно</div>
                </div>
            </div>
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
                <button class="promo-btn" id="profilePromoBtn">Активировать</button>
            </div>
            <div class="promo-msg" id="profilePromoMsg"></div>
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

        <div class="promo-section">
            <h4>🎁 Промокод</h4>
            <p>Если у вас есть код — введите ниже, баланс зачислится мгновенно.</p>
            <div class="promo-input-row">
                <input
                    type="text"
                    class="promo-input"
                    id="promoInput"
                    placeholder="VESTACC-XXXX"
                    autocomplete="off"
                    autocapitalize="characters"
                    spellcheck="false"
                    maxlength="32"
                />
                <button class="promo-btn" id="promoBtn">Активировать</button>
            </div>
            <div class="promo-msg" id="promoMsg"></div>
        </div>
    </div>

    <nav class="bottom-nav" id="bottomNav">
        <button class="nav-btn active" data-page="pageCatalog">
            <span class="nav-emoji">🛍️</span><span>Каталог</span>
        </button>
        <button class="nav-btn" data-page="pageProfile">
            <span class="nav-emoji">👤</span><span>Профиль</span>
        </button>
        <div class="nav-brand">Vest Account</div>
    </nav>

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
            <ul class="item-features">
                <li>✅ Сессия прошла верификацию</li>
                <li>⚡ Выдача сразу после оплаты</li>
                <li>🔒 Без банов на момент продажи</li>
            </ul>
            <p class="item-hint">
                Чтобы купить — откройте бота и нажмите «Купить аккаунт».
                Синхронизация с этим каталогом автоматическая.
            </p>
            <button class="btn-primary" data-close="itemModal">Понятно</button>
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
                catalog: [],
                categories: [],
                currentPage: 'pageCatalog',
                botUsername: null,
                lastBalanceSync: null,
            };

            const $ = (id) => document.getElementById(id);
            const dom = {
                userName: $('userName'),
                balancePill: $('balancePill'),
                balanceValue: $('balanceValue'),
                catScroll: $('catScroll'),
                catCount: $('catCount'),
                originScroll: $('originScroll'),
                catListCount: $('catListCount'),
                catalog: $('catalog'),
                loader: $('loader'),
                emptyState: $('emptyState'),
                pageCatalog: $('pageCatalog'),
                pageProfile: $('pageProfile'),
                pageTopup: $('pageTopup'),
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
                promoInput: $('promoInput'),
                promoBtn: $('promoBtn'),
                promoMsg: $('promoMsg'),
                profilePromoInput: $('profilePromoInput'),
                profilePromoBtn: $('profilePromoBtn'),
                profilePromoMsg: $('profilePromoMsg'),
                itemModal: $('itemModal'),
                itemFlag: $('itemFlag'),
                itemCountry: $('itemCountry'),
                itemOrigin: $('itemOrigin'),
                itemPrice: $('itemPrice'),
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

            /* ===== Page router ===== */
            function switchPage(pageId, opts) {
                opts = opts || {};
                const pages = ['pageCatalog', 'pageProfile', 'pageTopup'];
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
            }

            /* ===== User render ===== */
            function renderUser() {
                const u = state.tgUser;
                if (!u) {
                    dom.userName.textContent = 'Гость';
                    return;
                }
                const name = [u.first_name, u.last_name].filter(Boolean).join(' ') || 'Без имени';
                const sub = u.username ? '@' + u.username : ('id ' + u.id);
                dom.userName.textContent = name + ' · ' + sub;
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

            /* ===== Каталог ===== */
            async function loadCategories() {
                const r = await api('/api/categories');
                if (!r.ok) return;
                state.categories = r.data.categories || [];
                renderCategories();
            }
            async function loadCatalog() {
                showLoader(true);
                const params = new URLSearchParams();
                if (state.country && state.country !== 'all') params.set('country', state.country);
                if (state.origin && state.origin !== 'all') params.set('origin', state.origin);
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
            }
            function renderCategories() {
                const allBtn = dom.catScroll.querySelector('[data-country="all"]');
                dom.catScroll.innerHTML = '';
                dom.catScroll.appendChild(allBtn);
                state.categories.forEach((c) => {
                    const btn = document.createElement('button');
                    btn.className = 'cat-pill';
                    btn.dataset.country = c.country;
                    if (state.country === c.country) btn.classList.add('active');
                    btn.innerHTML = `<span class="cat-emoji">${c.flag}</span><span>${escapeHtml(c.country)}</span><span style="opacity:.6;font-weight:500;">${c.count}</span>`;
                    btn.addEventListener('click', () => selectCountry(c.country));
                    dom.catScroll.appendChild(btn);
                });
                dom.catCount.textContent = state.categories.length;
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
            function selectCountry(c) {
                state.country = c;
                dom.catScroll.querySelectorAll('[data-country]').forEach((b) => {
                    b.classList.toggle('active', b.dataset.country === c);
                });
                loadCatalog();
            }
            function selectOrigin(o) {
                state.origin = o;
                dom.originScroll.querySelectorAll('[data-origin]').forEach((b) => {
                    b.classList.toggle('active', b.dataset.origin === o);
                });
                loadCatalog();
            }
            function openItem(it) {
                dom.itemFlag.textContent = it.flag;
                dom.itemCountry.textContent = it.country;
                dom.itemOrigin.textContent = `${it.origin_icon} ${it.origin_label} · ${it.preview}`;
                dom.itemPrice.textContent = formatPrice(it.price) + ' ₽';
                openModal('itemModal');
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
                // Промокод уже обрабатывается отдельно
                if (method === 'promo') return;

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

            async function activatePromo() {
                const code = (dom.promoInput.value || '').trim().toUpperCase();
                if (!code) {
                    showPromoMsg('Введите промокод', 'error');
                    return;
                }
                dom.promoBtn.disabled = true;
                const oldText = dom.promoBtn.textContent;
                dom.promoBtn.textContent = '…';
                try {
                    const r = await api('/api/promo/redeem', {
                        method: 'POST',
                        body: JSON.stringify({ code: code }),
                    });
                    if (r.ok) {
                        showPromoMsg(
                            '✅ Промокод активирован! Зачислено: +' +
                                formatRub(r.data.amount) + ' ₽',
                            'success'
                        );
                        showToast('+' + formatRub(r.data.amount) + ' ₽ на баланс!', 'success');
                        dom.promoInput.value = '';
                        // Обновим баланс в UI
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
                        showPromoMsg(translatePromoError(err), 'error');
                    }
                } catch (e) {
                    showPromoMsg('Ошибка сети. Попробуйте ещё раз.', 'error');
                } finally {
                    dom.promoBtn.disabled = false;
                    dom.promoBtn.textContent = oldText;
                }
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

            function showPromoMsg(text, type) {
                dom.promoMsg.textContent = text;
                dom.promoMsg.className = 'promo-msg ' + (type || '');
            }

            /* ===== Активация промокода прямо из профиля (общая БД с ботом) ===== */
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
                // Catalog filters
                dom.catScroll.addEventListener('click', (e) => {
                    const btn = e.target.closest('[data-country]');
                    if (btn) selectCountry(btn.dataset.country);
                });
                dom.originScroll.addEventListener('click', (e) => {
                    const btn = e.target.closest('[data-origin]');
                    if (btn) selectOrigin(btn.dataset.origin);
                });

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
                dom.topupBtn.addEventListener('click', openTopup);
                dom.openTopup2.addEventListener('click', openTopup);
                dom.openSupport.addEventListener('click', openSupport);
                dom.openBot.addEventListener('click', openBotChat);

                // Topup methods
                document.querySelectorAll('.topup-method').forEach((btn) => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('.topup-method').forEach((b) => b.classList.remove('active'));
                        btn.classList.add('active');
                        activateTopupMethod(btn.dataset.method);
                    });
                });

                // Promo
                dom.promoBtn.addEventListener('click', activatePromo);
                dom.promoInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') activatePromo();
                });
                dom.promoInput.addEventListener('input', (e) => {
                    e.target.value = e.target.value.toUpperCase();
                });

                // Promo (прямо в профиле)
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
        q = q.order_by(Account.created_at.desc()).limit(limit).offset(offset)
        rows = session.execute(q).scalars().all()

        items = []
        for a in rows:
            origin_key = a.origin if a.origin in ORIGIN_LABELS else "Авторег"
            icon, label = ORIGIN_LABELS[origin_key]
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
            })
        return jsonify({"ok": True, "items": items, "count": len(items)})
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
