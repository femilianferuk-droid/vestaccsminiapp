"""
VestAccPunt Mini App — single-file Flask-приложение.

Один файл, который содержит:
  - Flask-бэк с валидацией Telegram initData (HMAC-SHA256)
  - REST API для каталога / категорий / профиля
  - HTML-шаблон мини-аппа со встроенными CSS и JS
  - Модели SQLAlchemy, идентичные схеме bot.py

Запуск:
    pip install -r requirements.txt
    cp .env.example .env   # заполни BOT_TOKEN и DATABASE_URL
    python app.py
"""
import os
import hmac
import hashlib
import json
from datetime import datetime
from urllib.parse import parse_qsl
from functools import wraps

from flask import Flask, request, jsonify, render_template_string, abort
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text,
    create_engine, select,
)
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
from dotenv import load_dotenv

load_dotenv()

# ===== КОНФИГ =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "8608742695:AAGlbLTlGniqZvwl9nE6IJBzj4UboWXN03A")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://bothost_db_d9dbd53f40eb:pa0bg7BK4-HmRor5Fpn3X58gh8kB_0a2OJMIle5kFSQ@node1.pghost.ru:15818/bothost_db_d9dbd53f40eb")
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

# ===== МОДЕЛИ (зеркалят схему из bot.py) =====
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255))
    last_name = Column(String(255))
    photo_url = Column(Text)
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


# ===== DB =====
# Пробуем с пулом по умолчанию для прода (Postgres).
# Если БД его не поддерживает (например sqlite при локальной отладке) —
# фоллбек на дефолт, чтобы не падать на импорте.
try:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    # Проверим что пул вообще применился (для некоторых диалектов max_overflow недопустим)
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


def mask_phone(phone: str) -> str:
    """Маскирует номер, оставляя последние 4 цифры."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) <= 4:
        return digits
    return "+" + "*" * (len(digits) - 4) + digits[-4:]


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
    <meta name="theme-color" content="#2563eb">
    <title>VestAccPunt — Каталог</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root {
            --blue-50:  #eff6ff;
            --blue-100: #dbeafe;
            --blue-200: #bfdbfe;
            --blue-300: #93c5fd;
            --blue-400: #60a5fa;
            --blue-500: #3b82f6;
            --blue-600: #2563eb;
            --blue-700: #1d4ed8;
            --blue-800: #1e40af;
            --blue-900: #1e3a8a;

            --white:    #ffffff;
            --gray-50:  #f8fafc;
            --gray-100: #f1f5f9;
            --gray-200: #e2e8f0;
            --gray-300: #cbd5e1;
            --gray-500: #64748b;
            --gray-700: #334155;
            --gray-900: #0f172a;

            --bg:       var(--blue-50);
            --surface:  var(--white);
            --text:     var(--gray-900);
            --text-muted: var(--gray-500);
            --accent:   var(--blue-600);
            --shadow:   0 4px 24px rgba(37, 99, 235, 0.08);
            --radius:   18px;
            --radius-sm: 12px;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
        html, body { height: 100%; overflow-x: hidden; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            font-size: 15px;
            line-height: 1.45;
            -webkit-font-smoothing: antialiased;
            padding-bottom: 80px;
        }

        /* Шапка */
        .app-header {
            position: sticky; top: 0; z-index: 50;
            background: linear-gradient(135deg, var(--blue-600), var(--blue-700));
            color: var(--white);
            padding: 14px 16px 18px;
            display: flex; align-items: center; gap: 12px;
            box-shadow: 0 4px 16px rgba(37, 99, 235, 0.18);
        }
        .avatar {
            width: 44px; height: 44px; border-radius: 50%;
            background: var(--blue-500); overflow: hidden;
            display: flex; align-items: center; justify-content: center;
            flex-shrink: 0; border: 2px solid rgba(255, 255, 255, 0.3);
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
            display: inline-flex; align-items: center; gap: 4px;
            background: rgba(255, 255, 255, 0.18);
            border: none; padding: 7px 12px; border-radius: 20px;
            color: var(--white); font-size: 13px; font-weight: 600;
            cursor: pointer; backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
            transition: background 0.2s;
        }
        .balance-pill:active { background: rgba(255, 255, 255, 0.28); }

        /* Hero */
        .hero {
            padding: 24px 20px 8px;
            background: linear-gradient(180deg, var(--blue-50) 0%, var(--bg) 100%);
        }
        .hero-title {
            font-size: 28px; font-weight: 700;
            color: var(--blue-900); letter-spacing: -0.5px; line-height: 1.1;
        }
        .hero-title span { color: var(--blue-500); }
        .hero-sub { font-size: 13px; color: var(--text-muted); margin-top: 8px; }

        /* Секции */
        .section { padding: 14px 16px 8px; }
        .section-head {
            display: flex; align-items: baseline; justify-content: space-between;
            margin-bottom: 10px; padding: 0 4px;
        }
        .section-title { font-size: 17px; font-weight: 700; color: var(--gray-900); }
        .section-count {
            font-size: 13px; color: var(--text-muted);
            background: var(--white); padding: 2px 10px;
            border-radius: 20px; font-weight: 600;
        }

        /* Пиллы категорий/фильтров */
        .cat-scroll {
            display: flex; gap: 8px; overflow-x: auto;
            padding: 4px 0 8px;
            scrollbar-width: none; -ms-overflow-style: none;
        }
        .cat-scroll::-webkit-scrollbar { display: none; }
        .cat-pill {
            flex-shrink: 0;
            background: var(--white);
            border: 1.5px solid var(--gray-200);
            color: var(--gray-700);
            padding: 8px 14px; border-radius: 14px;
            font-size: 13px; font-weight: 600;
            cursor: pointer; transition: all 0.18s;
            display: inline-flex; align-items: center; gap: 6px;
            white-space: nowrap; font-family: inherit;
        }
        .cat-pill:active { transform: scale(0.96); }
        .cat-pill.active {
            background: var(--blue-600); border-color: var(--blue-600);
            color: var(--white); box-shadow: 0 4px 14px rgba(37, 99, 235, 0.35);
        }
        .cat-emoji { font-size: 15px; }
        .cat-pill.active .cat-emoji { filter: brightness(1.2); }

        /* Каталог */
        .catalog-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
            padding: 4px 0 12px;
        }
        .card {
            background: var(--surface);
            border-radius: var(--radius);
            padding: 14px; box-shadow: var(--shadow);
            cursor: pointer; transition: transform 0.18s, box-shadow 0.18s;
            display: flex; flex-direction: column; gap: 8px;
            position: relative; overflow: hidden;
            animation: cardIn 0.3s ease-out backwards;
        }
        .card::before {
            content: ''; position: absolute; top: 0; left: 0; right: 0;
            height: 4px;
            background: linear-gradient(90deg, var(--blue-400), var(--blue-600));
            opacity: 0; transition: opacity 0.2s;
        }
        .card:active::before { opacity: 1; }
        .card:active { transform: scale(0.98); }
        .card-flag { font-size: 32px; line-height: 1; }
        .card-country { font-size: 14px; font-weight: 700; color: var(--gray-900); margin-top: 2px; }
        .card-origin {
            display: inline-flex; align-items: center; gap: 4px;
            font-size: 11px; background: var(--blue-50); color: var(--blue-700);
            padding: 3px 8px; border-radius: 8px;
            font-weight: 600; align-self: flex-start;
        }
        .card-preview {
            font-family: 'SF Mono', Monaco, Consolas, monospace;
            font-size: 12px; color: var(--text-muted); letter-spacing: 0.5px;
        }
        .card-price {
            font-size: 18px; font-weight: 700; color: var(--blue-700);
            margin-top: auto;
            display: flex; align-items: baseline; gap: 2px;
        }
        .card-price .rub { font-size: 13px; color: var(--text-muted); font-weight: 500; }

        /* Loader / empty */
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

        /* Нижняя навигация */
        .bottom-nav {
            position: fixed; bottom: 0; left: 0; right: 0;
            background: var(--white);
            border-top: 1px solid var(--gray-200);
            display: flex; justify-content: space-around;
            padding: 8px 0 calc(8px + env(safe-area-inset-bottom));
            z-index: 40;
            backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
        }
        .nav-btn {
            flex: 1; background: none; border: none;
            display: flex; flex-direction: column; align-items: center;
            gap: 2px; padding: 6px 4px;
            color: var(--text-muted); font-size: 11px; font-weight: 600;
            cursor: pointer; transition: color 0.15s; font-family: inherit;
        }
        .nav-btn .nav-emoji { font-size: 22px; transition: transform 0.15s; }
        .nav-btn.active { color: var(--blue-600); }
        .nav-btn.active .nav-emoji { transform: scale(1.15); }

        /* Модалки */
        .modal {
            position: fixed; inset: 0; z-index: 100;
            display: flex; align-items: flex-end; justify-content: center;
        }
        .modal.hidden { display: none; }
        .modal-backdrop {
            position: absolute; inset: 0;
            background: rgba(15, 23, 42, 0.5);
            backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
            animation: fadeIn 0.2s ease-out;
        }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        .modal-sheet {
            position: relative; background: var(--white);
            width: 100%; max-width: 480px;
            border-radius: 24px 24px 0 0;
            padding: 12px 20px calc(24px + env(safe-area-inset-bottom));
            animation: slideUp 0.25s cubic-bezier(0.32, 0.72, 0, 1);
        }
        @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
        .modal-handle {
            width: 40px; height: 4px; background: var(--gray-300);
            border-radius: 2px; margin: 0 auto 16px;
        }
        .modal-title { font-size: 20px; font-weight: 700; color: var(--gray-900); margin-bottom: 12px; }

        /* Профиль в модалке */
        .profile-row { display: flex; justify-content: center; margin: 8px 0 12px; }
        .profile-row .avatar { width: 72px; height: 72px; border-width: 3px; border-color: var(--blue-200); }
        .profile-name { text-align: center; font-size: 18px; font-weight: 700; color: var(--gray-900); }
        .profile-username { text-align: center; font-size: 14px; color: var(--text-muted); margin: 4px 0 16px; }
        .profile-stats {
            display: grid; grid-template-columns: 1fr 1fr;
            gap: 10px; margin-bottom: 16px;
        }
        .stat-card {
            background: var(--blue-50);
            border-radius: var(--radius-sm);
            padding: 14px; text-align: center;
        }
        .stat-label {
            font-size: 11px; color: var(--text-muted);
            text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;
        }
        .stat-value { font-size: 20px; font-weight: 700; color: var(--blue-700); }
        .profile-hint {
            text-align: center; color: var(--text-muted);
            font-size: 13px; margin-bottom: 16px; line-height: 1.6;
        }
        .profile-hint code {
            background: var(--blue-50); color: var(--blue-700);
            padding: 1px 6px; border-radius: 4px; font-size: 12px;
        }

        /* Item details */
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

        /* Кнопки */
        .btn-primary, .btn-secondary {
            width: 100%; padding: 14px; border: none;
            border-radius: var(--radius-sm);
            font-size: 15px; font-weight: 600;
            cursor: pointer; transition: opacity 0.15s, transform 0.15s;
            font-family: inherit;
        }
        .btn-primary {
            background: var(--blue-600); color: var(--white);
            box-shadow: 0 4px 14px rgba(37, 99, 235, 0.35);
        }
        .btn-primary:active { transform: scale(0.98); opacity: 0.9; }
        .btn-secondary {
            background: var(--gray-100); color: var(--gray-700);
            margin-top: 8px;
        }
        .btn-secondary:active { background: var(--gray-200); }

        /* Адаптив */
        @media (min-width: 600px) {
            .catalog-grid { grid-template-columns: repeat(3, 1fr); max-width: 720px; margin: 0 auto; }
        }
        @media (min-width: 900px) {
            .catalog-grid { grid-template-columns: repeat(4, 1fr); }
            body { max-width: 720px; margin: 0 auto; box-shadow: 0 0 40px rgba(0,0,0,0.05); }
            .bottom-nav { max-width: 720px; left: 50%; transform: translateX(-50%); }
        }
        @keyframes cardIn {
            from { opacity: 0; transform: translateY(8px); }
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
    <header class="app-header" id="appHeader">
        <div class="avatar" id="userAvatar">
            <span class="avatar-fallback" id="avatarFallback">…</span>
        </div>
        <div class="user-info">
            <div class="user-name" id="userName">Загрузка…</div>
            <div class="user-meta" id="userMeta">@—</div>
        </div>
        <button class="balance-pill" id="balancePill" aria-label="Баланс">
            <span class="balance-icon">💎</span>
            <span class="balance-value" id="balanceValue">—</span>
        </button>
    </header>

    <section class="hero">
        <h1 class="hero-title">Маркетплейс<br><span>аккаунтов</span></h1>
        <p class="hero-sub">Проверенные сессии · моментальная выдача · 24/7</p>
    </section>

    <section class="section">
        <div class="section-head">
            <h2 class="section-title">Страны</h2>
            <span class="section-count" id="catCount">0</span>
        </div>
        <div class="cat-scroll" id="catScroll">
            <button class="cat-pill active" data-country="all">
                <span class="cat-emoji">🌐</span>
                <span>Все</span>
            </button>
        </div>
    </section>

    <section class="section">
        <div class="section-head">
            <h2 class="section-title">Происхождение</h2>
        </div>
        <div class="cat-scroll" id="originScroll">
            <button class="cat-pill active" data-origin="all">
                <span class="cat-emoji">✨</span>
                <span>Любое</span>
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

    <nav class="bottom-nav">
        <button class="nav-btn active" data-tab="catalog">
            <span class="nav-emoji">🛒</span><span>Каталог</span>
        </button>
        <button class="nav-btn" data-tab="profile">
            <span class="nav-emoji">👤</span><span>Профиль</span>
        </button>
        <button class="nav-btn" data-tab="support">
            <span class="nav-emoji">💬</span><span>Поддержка</span>
        </button>
    </nav>

    <div class="modal hidden" id="profileModal">
        <div class="modal-backdrop" data-close="profileModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <h3 class="modal-title">Профиль</h3>
            <div class="profile-row" id="profileAvatar"></div>
            <div class="profile-name" id="profileName">—</div>
            <div class="profile-username" id="profileUsername">—</div>
            <div class="profile-stats">
                <div class="stat-card">
                    <div class="stat-label">Баланс</div>
                    <div class="stat-value" id="profileBalance">—</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Рейтинг</div>
                    <div class="stat-value" id="profileRating">—</div>
                </div>
            </div>
            <p class="profile-hint">
                Покупка и продажа — через основной бот.<br>
                Откройте его командой <code>/start</code>.
            </p>
            <button class="btn-primary" data-close="profileModal">Готово</button>
        </div>
    </div>

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
            };

            const $ = (id) => document.getElementById(id);
            const dom = {
                avatar: $('userAvatar'),
                avatarFallback: $('avatarFallback'),
                userName: $('userName'),
                userMeta: $('userMeta'),
                balancePill: $('balancePill'),
                balanceValue: $('balanceValue'),
                catScroll: $('catScroll'),
                catCount: $('catCount'),
                originScroll: $('originScroll'),
                catListCount: $('catListCount'),
                catalog: $('catalog'),
                loader: $('loader'),
                emptyState: $('emptyState'),
                profileModal: $('profileModal'),
                profileAvatar: $('profileAvatar'),
                profileName: $('profileName'),
                profileUsername: $('profileUsername'),
                profileBalance: $('profileBalance'),
                profileRating: $('profileRating'),
                itemModal: $('itemModal'),
                itemFlag: $('itemFlag'),
                itemCountry: $('itemCountry'),
                itemOrigin: $('itemOrigin'),
                itemPrice: $('itemPrice'),
                supportModal: $('supportModal'),
                supportBtn: $('supportBtn'),
            };

            function renderUser() {
                const u = state.tgUser;
                if (!u) {
                    dom.userName.textContent = 'Гость';
                    dom.userMeta.textContent = 'Откройте из Telegram';
                    dom.avatarFallback.textContent = '👤';
                    return;
                }
                const name = [u.first_name, u.last_name].filter(Boolean).join(' ') || 'Без имени';
                dom.userName.textContent = name;
                dom.userMeta.textContent = u.username ? '@' + u.username : 'id ' + u.id;
                dom.avatarFallback.textContent = (u.first_name || u.username || '?').charAt(0).toUpperCase();
                if (u.photo_url) {
                    const img = document.createElement('img');
                    img.src = u.photo_url;
                    img.alt = name;
                    img.onload = () => {
                        dom.avatarFallback.style.display = 'none';
                        dom.avatar.appendChild(img);
                    };
                }
            }

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

            async function auth() {
                if (!state.initData) return { ok: false };
                const r = await api('/api/auth', {
                    method: 'POST',
                    body: JSON.stringify({ initData: state.initData }),
                });
                if (r.ok && r.data.user) {
                    dom.balanceValue.textContent = (r.data.user.balance || 0).toFixed(0);
                }
                return r;
            }

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
            function openProfile() {
                const u = state.tgUser;
                if (!u) return;
                const name = [u.first_name, u.last_name].filter(Boolean).join(' ') || 'Без имени';
                dom.profileName.textContent = name;
                dom.profileUsername.textContent = u.username ? '@' + u.username : 'id ' + u.id;
                dom.profileAvatar.innerHTML = '';
                const av = document.createElement('div');
                av.className = 'avatar';
                av.innerHTML = `<span class="avatar-fallback">${(u.first_name || '?').charAt(0).toUpperCase()}</span>`;
                if (u.photo_url) {
                    const img = document.createElement('img');
                    img.src = u.photo_url;
                    img.onload = () => {
                        av.querySelector('.avatar-fallback').style.display = 'none';
                        av.appendChild(img);
                    };
                    dom.profileAvatar.appendChild(av);
                } else {
                    dom.profileAvatar.appendChild(av);
                }
                api('/api/me').then((r) => {
                    if (r.ok && r.data.user) {
                        const u2 = r.data.user;
                        dom.profileBalance.textContent = (u2.balance || 0).toFixed(0) + ' ₽';
                        dom.profileRating.textContent = (u2.rating || 5).toFixed(1) + ' ★';
                    }
                });
                openModal('profileModal');
            }
            function openSupport() { openModal('supportModal'); }
            function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
            function closeModal(id) { document.getElementById(id).classList.add('hidden'); }
            function showLoader(v) { dom.loader.classList.toggle('hidden', !v); }
            function showEmpty(v) { dom.emptyState.classList.toggle('hidden', !v); }

            function formatPrice(n) {
                return (Number(n) || 0).toLocaleString('ru-RU', { maximumFractionDigits: 0 });
            }
            function escapeHtml(s) {
                return String(s || '').replace(/[&<>"']/g, (c) => ({
                    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
                })[c]);
            }

            function bindEvents() {
                dom.catScroll.addEventListener('click', (e) => {
                    const btn = e.target.closest('[data-country]');
                    if (btn) selectCountry(btn.dataset.country);
                });
                dom.originScroll.addEventListener('click', (e) => {
                    const btn = e.target.closest('[data-origin]');
                    if (btn) selectOrigin(btn.dataset.origin);
                });
                dom.balancePill.addEventListener('click', openProfile);
                document.addEventListener('click', (e) => {
                    const closer = e.target.closest('[data-close]');
                    if (closer) closeModal(closer.dataset.close);
                });
                document.querySelectorAll('.nav-btn').forEach((btn) => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('.nav-btn').forEach((b) => b.classList.remove('active'));
                        btn.classList.add('active');
                        const tab = btn.dataset.tab;
                        if (tab === 'profile') openProfile();
                        else if (tab === 'support') openSupport();
                        else window.scrollTo({ top: 0, behavior: 'smooth' });
                    });
                });
                dom.supportBtn.addEventListener('click', () => {
                    if (tg && tg.openTelegramLink) tg.openTelegramLink('https://t.me/VestGameSupport');
                    else window.open('https://t.me/VestGameSupport', '_blank');
                });
            }

            async function bootstrap() {
                renderUser();
                bindEvents();
                await Promise.all([auth(), loadCategories(), loadCatalog()]);
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
            db_user = User(
                telegram_id=tg_id,
                username=tg_user.get("username"),
                first_name=tg_user.get("first_name"),
                last_name=tg_user.get("last_name"),
                photo_url=tg_user.get("photo_url"),
            )
            session.add(db_user)
        else:
            db_user.username = tg_user.get("username") or db_user.username
            db_user.first_name = tg_user.get("first_name") or db_user.first_name
            db_user.last_name = tg_user.get("last_name") or db_user.last_name
            if tg_user.get("photo_url"):
                db_user.photo_url = tg_user["photo_url"]
        session.commit()
        return jsonify({
            "ok": True,
            "user": {
                "id": tg_id,
                "username": db_user.username,
                "first_name": db_user.first_name,
                "last_name": db_user.last_name,
                "photo_url": db_user.photo_url,
                "balance": db_user.balance,
                "is_admin": db_user.is_admin,
            },
        })
    finally:
        session.close()


@app.route("/api/catalog")
def api_catalog():
    """Каталог доступных аккаунтов из таблицы accounts."""
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


@app.route("/api/me")
@require_auth
def api_me(telegram_id, tg_user):
    """Профиль текущего юзера (требует валидный initData)."""
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
                "first_name": db_user.first_name,
                "last_name": db_user.last_name,
                "photo_url": db_user.photo_url or tg_user.get("photo_url"),
                "balance": db_user.balance,
                "hold_balance": db_user.hold_balance,
                "rating": db_user.rating,
                "reviews_count": db_user.reviews_count,
                "is_admin": db_user.is_admin,
            },
        })
    finally:
        session.close()


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()})


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not found"}), 404
    abort(404)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
