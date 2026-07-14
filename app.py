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
import threading
import time
import tempfile
import urllib.request
import urllib.error
import pathlib
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
from functools import wraps
from typing import Optional

from flask import (
    Flask, request, jsonify, render_template_string, abort, send_file
)
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text,
    create_engine, func, select, text,
)
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv

# Telethon — для получения кода подтверждения и выдачи сессии.
# Те же api_id / api_hash, что и в боте (ОБЩАЯ сессия, общий ключ).
from telethon import TelegramClient, errors as telethon_errors
from telethon.sessions import StringSession, SQLiteSession

load_dotenv()

# ===== EVENT LOOP ДЛЯ TELETHON =====
# Flask работает в sync-режиме, а Telethon — async. Каждый asyncio.run()
# создаёт НОВЫЙ event loop, а Telethon-клиент жёстко привязан к loop,
# в котором был создан → при попытке использовать тот же клиент в
# другом loop получаем "Event loop is closed" / RuntimeError, и
# фронт мини-аппа видит ошибку при вводе кода (хотя код верный).
#
# Решение: один долгоживущий event loop в фоновом потоке. Все async-
# операции (в т.ч. sign_in, get_me, disconnect) запускаем через
# asyncio.run_coroutine_threadsafe() в ЭТОМ loop. Клиент, созданный
# в нём, спокойно переживает между запросами — лежит в _SELL_PENDING.
_ASYNC_LOOP: Optional[asyncio.AbstractEventLoop] = None
_ASYNC_THREAD: Optional[threading.Thread] = None
_ASYNC_LOCK = threading.Lock()


def _start_async_loop() -> asyncio.AbstractEventLoop:
    """Лениво поднимает фоновый поток с event loop и возвращает его."""
    global _ASYNC_LOOP, _ASYNC_THREAD
    if _ASYNC_LOOP is not None and _ASYNC_LOOP.is_running():
        return _ASYNC_LOOP
    with _ASYNC_LOCK:
        if _ASYNC_LOOP is not None and _ASYNC_LOOP.is_running():
            return _ASYNC_LOOP
        loop = asyncio.new_event_loop()
        def _runner():
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
        t = threading.Thread(target=_runner, name="telethon-async-loop", daemon=True)
        t.start()
        # ждём, пока loop реально стартует
        while not loop.is_running():
            pass
        _ASYNC_LOOP = loop
        _ASYNC_THREAD = t
        return loop


def _run_async(coro, timeout: Optional[float] = None):
    """Запускает корутину в общем event loop из sync-кода Flask.

    Блокирует текущий воркер Gunicorn на время выполнения корутины —
    это нормально для операций sign_in / get_me / disconnect, которые
    отрабатывают за секунды (как и в bot.py, где они тоже синхронно
    ждут внутри aiogram-обработчика).
    """
    loop = _start_async_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


# ===== КОНФИГ =====
# Telethon API — ТЕ ЖЕ значения, что и в bot.py (vestaccpunt),
# чтобы сессия из бота валидировалась в мини-аппе без расхождений.
API_ID = int(os.getenv("TELEGRAM_API_ID", "32480523"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "147839735c9fa4e83451209e9b55cfc5")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8608742695:AAFC8Ea7U7Y23wMM4FCTzu5YPM0AXbYIoZ8")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://bothost_db_d9dbd53f40eb:pa0bg7BK4-HmRor5Fpn3X58gh8kB_0a2OJMIle5kFSQ@node1.pghost.ru:15818/bothost_db_d9dbd53f40eb")
if "+asyncpg" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("+asyncpg", "")
SECRET_KEY = os.getenv("FLASK_SECRET", os.urandom(32).hex())

if not BOT_TOKEN:
    app_config_error = "BOT_TOKEN is not configured"
elif not DATABASE_URL:
    app_config_error = "DATABASE_URL is not configured"
else:
    app_config_error = None

# ===== VEST ACCOUNT BOT (ОТДЕЛЬНОЕ «ЛИЦО» В ЧАТЕ) =====
#
# В in-app чате между покупателем и продавцом сообщения о покупке и
# релизе холда пишет «Vest Account» — отдельное лицо, а не покупатель.
# Технически это просто sender_id = 0 в таблице chat_messages
# (любой реальный telegram_id > 0, поэтому 0 — наш «зарезервированный»
# идентификатор бота). На фронте такие сообщения рисуются с аватаркой
# из PNG-файла в репозитории и именем «Vest Account».
BOT_SENDER_ID = 0

# Аватарка бота — файл рядом с app.py (лежит в репозитории miniapp).
# Отдаём его через /api/bot_avatar с кешированием на стороне браузера.
_BOT_AVATAR_PATH = pathlib.Path(__file__).resolve().parent / \
    "Gemini_Generated_Image_w0v6n4w0v6n4w0v6.png"

# Маркер кнопок в тексте сообщений бота. Фронт парсит эти токены и
# рендерит их как настоящие кнопки под ��узырьком. Формат:
#   [[BTN:<action>|<label>]]            — простая кнопка (например, open_dispute)
#   [[BTN:<action>:<param>|<label>]]     — кнопка с параметром (например,
#                                         open_review:42 — оставить отзыв
#                                         по покупке #42). param — это
#                                         цифровой id, без пробелов.
# где <action> — идентификатор, <label> — текст на кнопке.
BOT_BTN_TOKEN_RE = re.compile(r'\[\[BTN:([a-z_]+)(?::(\d+))?\|([^\]]+)\]\]')

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["JSON_AS_ASCII"] = False

# ===== МОДЕЛИ (зеркалят схем�� из bot.py — ОБЩАЯ БД) =====
Base = declarative_base()


class User(Base):
    # ⚠️ Схема СТРОГО совпадает с bot.py — никаких лишних колонок,
    # иначе UPDATE при апсерте упадёт с "column does not exist".
    # first_name — необязательная колонка (создаётся миграцией ниже).
    # В bot.py её нет, но SELECT * работает без знания колонок, а наш
    # UPSERT идёт по явному списку полей — bot.py сломать не должен.
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255), nullable=True)
    # last_name — необязательная колонка (создаётся миграцией ниже).
    # Используется в карточке товара, чтобы показать ПОЛНОЕ имя продавца
    # (first_name + last_name), а не только first_name.
    last_name = Column(String(255), nullable=True)
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
    # Месяц и год регистрации Telegram-аккаунта — их указывает продавец
    # при добавлении объявления (на форме «Продать»). NULL = не указано.
    # Эти поля нужны, чтобы фильтр каталога «Дата создания аккаунта
    # (от/до)» работал по реальной дате регистрации аккаунта, а не по
    # дате добавления объявления в БД (created_at — это другое).
    reg_month = Column(Integer, nullable=True)
    reg_year = Column(Integer, nullable=True)
    # ====== TELEGRAM PREMIUM ======
    # True, если у аккаунта активна подписка Telegram Premium.
    # Определяется автоматически при выставлении (sell session /
    # sell phone verify / sell phone 2fa) через Telethon
    # `await client.get_me()` → `me.premium`. На карточке каталога
    # бэкенд отдаёт `has_premium`, а фронт рисует зелёный бейдж
    # «Телеграмм премиум». Не путать с Premium-подпиской у покупателя
    # — это флаг САМОГО аккаунта, который продаётся.
    has_premium = Column(Boolean, default=False, nullable=False)
    # ====== SPAM STATUS ======
    # Статус проверки @SpamBot: NULL = не проверен, "clean" = без блока,
    # "spam" = спам-блок активен. "checking" = проверка в процессе.
    # Проверка запускается автоматически сразу после выставления аккаунта.
    spam_status = Column(String(20), nullable=True)


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


# ----- Остальные модели — ОБЩАЯ БД с bot.py (vestaccpunt).
# Эти таблицы создаёт и пишет бот, но модели здесь нужны, чтобы:
#  1) Base.metadata совпадал со схемой бота (нет расхождений при будущих миграциях);
#  2) любые join / cross-table запросы из мини-аппа могли использовать ORM;
#  3) мини-апп случайно не уронил данные, если кто-то добавит сюда feature.
# Имена таблиц и колонок СТРОГО как в bot.py — никаких отсебятин.


class Hold(Base):
    """Холд средств продавца после продажи (P2P маркетплейс).

    Бот со��даёт эту запись сразу после покупки, а через HOLD_PERIOD_HOURS
    переводит деньги на баланс продавца. Мини-апп только читает.
    """
    __tablename__ = "holds"
    id = Column(Integer, primary_key=True)
    seller_id = Column(BigInteger, nullable=False)
    listing_id = Column(Integer, nullable=False)
    purchase_id = Column(Integer, nullable=False)
    gross_amount = Column(Float, nullable=False)     # сумма продажи
    commission = Column(Float, nullable=False)       # комиссия (7%)
    net_amount = Column(Float, nullable=False)       # сколько получит продавец (93%)
    status = Column(String(20), default="hold")      # hold / released / cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    release_at = Column(DateTime, nullable=False)
    released_at = Column(DateTime, nullable=True)


class Review(Base):
    """Отзыв покупателя о продавце после покупки (P2P маркетплейс).

    Бот пишет, мини-апп читает (для профиля продавца и карточки объявления).
    is_auto=True — отзыв выставлен автоматически фоновым планировщиком
    мини-аппа через REVIEW_AUTO_POST_DAYS дней после покупки (т.е. покупатель
    не оставил отзыв сам). По умолчанию False (ручной отзыв).
    """
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True)
    seller_id = Column(BigInteger, nullable=False)
    buyer_id = Column(BigInteger, nullable=False)
    listing_id = Column(Integer, nullable=False)
    purchase_id = Column(Integer, nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    # Флаг «автоматический отзыв». Не путать с bot.py — если бот когда-то
    # начнёт писать эту колонку, схема совпадёт. NOT NULL DEFAULT FALSE,
    # чтобы старые строки получили валидное значение.
    is_auto = Column(Boolean, default=False, nullable=False)


class RequiredChannel(Base):
    """Обязательные каналы для подписки (общая таблица с bot.py).

    Бот управляет списком и проверяет подписку, мини-апп может показывать
    плашку «Подпишитесь на каналы» — поэтому модель нужна и тут.
    """
    __tablename__ = "required_channels"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String(255), nullable=False)
    channel_url = Column(String(255), nullable=False)
    channel_name = Column(String(255), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class MediaSettings(Base):
    """Настройки медиа для разделов бота (общая таблица с bot.py).

    Бот администрирует, мини-апп только читает (если понадобится рендерить
    картинки разделов в маркетплейсе).
    """
    __tablename__ = "media_settings"
    id = Column(Integer, primary_key=True)
    section = Column(String(50), unique=True, nullable=False)
    file_id = Column(String(255), nullable=False)
    file_type = Column(String(20), default="photo")
    caption = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ChatThread(Base):
    """Диалог между двумя пользователями (общая таблица с bot.py).

    Поля user1_id / user2_id — это telegram_id. Чтобы один и тот же диалог
    между парой пользователей не плодил дубликаты, при создании всегда
    сортируем (min, max) и ищем существующую запись перед INSERT-ом.

    last_message_at нужен для сортировки списка чатов «свежие сверху».
    """
    __tablename__ = "chat_threads"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user1_id = Column(BigInteger, nullable=False, index=True)
    user2_id = Column(BigInteger, nullable=False, index=True)
    last_message_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ChatMessage(Base):
    """Сообщение в диалоге (общая таблица с bot.py).

    sender_id — telegram_id отправителя (для проверки «своё/чужое» на фронте).
    read_at — NULL = не прочитано получателем; заполняется при открытии диалога.
    """
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(Integer, nullable=False, index=True)
    sender_id = Column(BigInteger, nullable=False, index=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    read_at = Column(DateTime, nullable=True)


# ===== DB =====
# Важно: не открываем соединение при импорте модуля. На serverless cold start
# синхронный connect к недоступной БД блокировал отдачу HTML и выглядел как
# бесконечная загрузка как в Telegram WebView, так и в обычном браузере.
_db_url = DATABASE_URL or "sqlite:////tmp/vest-account-fallback.db"
_engine_options = {"pool_pre_ping": True}
if _db_url.startswith("postgresql"):
    _engine_options.update({
        "pool_size": 5,
        "max_overflow": 10,
        "pool_timeout": 5,
        "connect_args": {"connect_timeout": 5},
    })
engine = create_engine(_db_url, **_engine_options)
SessionLocal = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))


# ===== CREATE TABLE IF NOT EXISTS (для таблиц, которые БОТ ещё не знает) =====
#
# Base.metadata в этом файле уже содержит ВСЕ общие таблицы (users, listings,
# reviews и т.п.) — но app.py не вызывает create_all() (это делает бот в
# setup_database). Если новые таблицы (чат) есть только здесь, бот их не
# создаст. Поэтому при старте делаем точечный CREATE TABLE через metadata —
# это безопасно: если таблица уже есть (checkfirst=True), ничего не меняется.
# Плюс SQLAlchemy сама генерирует корректный DDL под текущую БД (PostgreSQL /
# SQLite / ...) — в отличие от хардкода через SERIAL.
def _ensure_chat_tables():
    """Создаём таблицы чатов, если их ещё нет. Ничего не ломает."""
    try:
        Base.metadata.create_all(
            bind=engine,
            tables=[ChatThread.__table__, ChatMessage.__table__],
            checkfirst=True,
        )
    except Exception as _e:
        # Не валим старт приложения из-за DDL — пусть даже без чатов работает
        # (например, если БД ещё не готова или прав нет, бот всё равно поднимет).
        try:
            app.logger.warning("chat tables DDL failed: %s", _e)
        except Exception:
            pass


def _ensure_user_first_name_column():
    """Добавляем колонки users.first_name / users.last_name, если их ещё нет.

    Нужно для отображения им��н собеседников в чате (а не «id XXXX»)
    и ПОЛНОГО имени продавца в карточке товара (first_name + last_name).
    Если bot.py уже создал users без этих колонок — без миграции
    SQLAlchemy-модель в app.py будет считать, что поле есть, и любой
    SELECT/UPDATE с first_name/last_name упадёт с «column does not exist».
    """
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(255)"
            )
            # last_name — необязательное, без default. Если запись уже
            # существует, останется NULL — и фронт в карточке просто не
            # покажет фамилию.
            conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name VARCHAR(255)"
            )
    except Exception as _e:
        try:
            app.logger.warning("users.first_name/last_name migration failed: %s", _e)
        except Exception:
            pass


def _ensure_account_reg_columns():
    """Добавляем колонки accounts.reg_month / accounts.reg_year, если их ещё нет.

    Продавец указывает месяц и год регистрации Telegram-аккаунта
    при создании объявления (форма «Продать» → «Дата регистрации»).
    Если таблица уже создана bot.py без этих колонок — без миграции
    любой INSERT/UPDATE с reg_month / reg_year упадёт с
    «column does not exist», и фронт мини-аппа увидит 500.
    """
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS reg_month INTEGER"
            )
            conn.exec_driver_sql(
                "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS reg_year INTEGER"
            )
    except Exception as _e:
        try:
            app.logger.warning("accounts.reg_* migration failed: %s", _e)
        except Exception:
            pass


def _ensure_account_premium_column():
    """Добавляем колонку accounts.has_premium, если её ещё нет.

    Флаг Telegram Premium на самом аккаунте (не у покупателя):
    выставляется автоматически при создании объявления, если у
    загружаемой сессии `client.get_me().premium == True`.
    Если таблица уже создана bot.py без этой колонки — без миграции
    любой INSERT/UPDATE с has_premium упадёт с «column does not exist»
    и фронт мини-аппа увидит 500.
    """
    try:
        with engine.begin() as conn:
            # NOT NULL DEFAULT FALSE — чтобы и старые строки получили
            # валидное значение, и новые записи всегда имели осмысленный
            # default, если поле случайно не передадут.
            conn.exec_driver_sql(
                "ALTER TABLE accounts "
                "ADD COLUMN IF NOT EXISTS has_premium BOOLEAN NOT NULL DEFAULT FALSE"
            )
    except Exception as _e:
        try:
            app.logger.warning("accounts.has_premium migration failed: %s", _e)
        except Exception:
            pass


def _ensure_review_is_auto_column():
    """Добавляем колонку reviews.is_auto, если её ещё нет.

    True = отзыв выставлен автоматически фоновым планировщиком мини-аппа
    (через REVIEW_AUTO_POST_DAYS дней после покупки). Если таблица reviews
    уже создана bot.py без этой колонки — без миграции INSERT авто-отзыва
    упадёт с «column does not exist», и фон-поток просто пропустит запись
    (юзер останется без отзыва). Миграция на старте это чинит.
    """
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE reviews "
                "ADD COLUMN IF NOT EXISTS is_auto BOOLEAN NOT NULL DEFAULT FALSE"
            )
    except Exception as _e:
        try:
            app.logger.warning("reviews.is_auto migration failed: %s", _e)
        except Exception:
            pass


def _ensure_account_spam_status_column():
    """Добавляем колонку accounts.spam_status, если её ещё нет.

    Хранит результат проверки @SpamBot: NULL = не проверен, "checking" =
    проверяется, "clean" = без спам-блока, "spam" = спам-блок активен.
    Проверка запускается автоматически после выставления аккаунта.
    """
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS spam_status VARCHAR(20)"
            )
    except Exception as _e:
        try:
            app.logger.warning("accounts.spam_status migration failed: %s", _e)
        except Exception:
            pass


# Миграции намеренно не выполняются при импорте модуля. Их синхронный DDL
# блокировал cold start при проблемах сети. Схема разворачивается ботом/
# отдельным migration job; приложение остаётся доступным даже при сбое БД.


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

# Кеш аватарок пользователей: telegram_id -> {"url": str|None, "ts": float}
# TTL побольше, потому что photo_url живёт долго (если пользователь
# сменил фото — обновится само при следующем запросе через TTL).
_TG_PHOTO_CACHE: dict[int, dict] = {}
_TG_PHOTO_TTL = 60 * 60 * 6  # 6 часов


def _get_telegram_photo_url(tg_id: int) -> Optional[str]:
    """Возвращает прямую ссылку на маленькую аватарку пользователя
    через Bot API: getUserProfilePhotos + getFile. Результат кешируется
    в памяти процесса на _TG_PHOTO_TTL секунд (None тоже кешируется —
    чтобы не лупить Bot API для пользователей без фото).

    Возвращает:
        str  — абсолютный URL на файл (https://api.telegram.org/file/bot<TOKEN>/<path>)
        None — у пользователя нет фото или не получилось достать
    """
    if not tg_id:
        return None
    now = datetime.now().timestamp()
    cached = _TG_PHOTO_CACHE.get(tg_id)
    if cached is not None and (now - cached.get("ts", 0)) < _TG_PHOTO_TTL:
        return cached.get("url")

    url: Optional[str] = None
    try:
        # 1) getUserProfilePhotos — берём од��у самую свежую фотку
        photos_url = (
            f"https://api.telegram.org/bot{BOT_TOKEN}"
            f"/getUserProfilePhotos?user_id={int(tg_id)}&limit=1&offset=0"
        )
        with urllib.request.urlopen(photos_url, timeout=4) as resp:
            photos_data = json.loads(resp.read().decode("utf-8"))
        photos = (((photos_data or {}).get("result") or {}).get("photos")) or []
        if photos and photos[0]:
            file_id = photos[0][0].get("file_id")
            if file_id:
                # 2) getFile — получаем file_path
                file_url = (
                    f"https://api.telegram.org/bot{BOT_TOKEN}"
                    f"/getFile?file_id={file_id}"
                )
                with urllib.request.urlopen(file_url, timeout=4) as resp2:
                    file_data = json.loads(resp2.read().decode("utf-8"))
                file_path = ((file_data or {}).get("result") or {}).get("file_path")
                if file_path:
                    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    except Exception:
        # Тихо проглатываем — нет фото / сеть лежит / таймаут.
        url = None

    # Кешируем и None тоже — иначе будем долбить Bot API каждый запрос.
    _TG_PHOTO_CACHE[tg_id] = {"url": url, "ts": now}

    # Лёгкая защита от утечки памяти: держим кеш ≤ 2 000 записей.
    if len(_TG_PHOTO_CACHE) > 2000:
        try:
            # удаляем самые старые ~25% записей
            sorted_items = sorted(_TG_PHOTO_CACHE.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in sorted_items[:500]:
                _TG_PHOTO_CACHE.pop(k, None)
        except Exception:
            pass

    return url


def mask_phone(phone: str) -> str:
    """Маскирует номер, оставляя последние 4 цифры."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) <= 4:
        return digits
    return "+" + "*" * (len(digits) - 4) + digits[-4:]


def _compose_full_name(first_name, last_name) -> str:
    """Склеивает first_name + last_name, аккуратно убирая пробелы/None.

    Возвращает:
        'Иван Петров'  — если есть оба
        'Иван'         — если только first
        'Петров'       — если только last (крайне редкий случай)
        ''             — если оба пустые
    """
    parts = []
    if first_name and str(first_name).strip():
        parts.append(str(first_name).strip())
    if last_name and str(last_name).strip():
        parts.append(str(last_name).strip())
    return " ".join(parts)


# ====== МЕСЯЦ И ГОД РЕГИСТРАЦИИ (подпись для описания) ======
# Полный список имён месяцев на русском — для красивого вывода
# «Регистрация: Июль 2021» в описании объявления. Если год/месяц
# не указаны — возвращаем пустую строку (тогда префикс не добавляется).
_MONTH_NAMES_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def format_reg_info(reg_month, reg_year) -> str:
    """Возвращает короткую строку про дату регистрации для описания.

    При��еры:
        format_reg_info(7, 2021) -> 'Регистрация: Июль 2021'
        format_reg_info(None, 2021) -> 'Регистрация: 2021 г.'
        format_reg_info(7, None) -> 'Регистрация: Июль'
        format_reg_info(None, None) -> ''
    """
    try:
        m = int(reg_month) if reg_month not in (None, "", 0) else None
        y = int(reg_year)  if reg_year  not in (None, "", 0) else None
    except (TypeError, ValueError):
        return ""
    if m is not None and not (1 <= m <= 12):
        m = None
    if y is not None and not (2013 <= y <= 2026):
        y = None
    if m is None and y is None:
        return ""
    if m is not None and y is not None:
        return f"Регистрация: {_MONTH_NAMES_RU[m]} {y}"
    if m is not None:
        return f"Регистрация: {_MONTH_NAMES_RU[m]}"
    return f"Регистрация: {y} г."


def build_listing_description(raw_description: Optional[str], reg_month, reg_year) -> Optional[str]:
    """Возвращает ТОЛЬКО свободный текст продавца (без даты регистрации).

    История: раньше функция склеивала «Регистрация: Июль 2021» и
    описание продавца через \n\n — это мешало верстать карточку и
    модалку товара и портило читаемость. Теперь дата регистрации
    отдаётся отдельным полем `reg_text` в API и рендерится в виде
    строки в блоке метаданных рядом с «Сессия верифицирована» /
    «Премиум-аккаунт». Описание остаётся чистым.

    Возвращает:
        - None, если описание пустое (фронт покажет дефолтный текст
          «Аккаунт прошёл проверку…»).
        - строку — текст продавца без префикса даты.
    """
    if raw_description and raw_description.strip():
        return raw_description.strip()
    return None


def get_bot_username() -> Optional[str]:
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
        return _run_async(_get_code_from_session_async(session_string, phone), timeout=30)
    except Exception:
        return None


# ===== P2P-ПРОДАЖА АККАУНТОВ (СХЕМА КАК В bot.py) =====
# Лимиты и комиссия — те же, что в bot.py (vestaccpunt):
COMMISSION_PERCENT = 7.0    # комиссия платформы с продажи
HOLD_PERIOD_HOURS = 24      # часов в холде после продажи
HOLD_RELEASE_CHECK_INTERVAL = 60  # как часто (сек) фоновый поток проверяет холды

# ===== ОТЗЫВЫ (review) =====
# Сколько дней у покупателя есть на ручной отзыв. Если за это время
# отзыв не оставлен — фоновый планировщик выставляет автоматический
# 5-звёздочный отзыв. По аналогии с FunPay: не наказание, а дефолт —
# молчание = «всё ок».
REVIEW_AUTO_POST_DAYS = 7
# Как часто фоновый поток проверяет, не пора ли выставить авто-отзыв.
# 1 час — баланс между свежестью отзыва и нагрузкой на БД.
REVIEW_AUTO_CHECK_INTERVAL = 60 * 60
MIN_LISTING_PRICE = 10.0    # минимальная цена объявления
MAX_LISTING_PRICE = 50000.0 # максимальная цена

# In-memory state для phone-flow (как pending_auth в bot.py).
# Ключ — telegram_id, в нём хранится активный Telethon-клиент,
# phone_code_hash и черновик объявления до прохождения кода/2FA.
_SELL_PENDING: dict = {}

# Страны по телефонным кодам (как в bot.py, ��тобы мини-апп и бот
# определяли страну одинаково). Полный список ниже; здесь — самые
# распространённые префиксы.
_PHONE_PREFIX_COUNTRY = {
    "1": "США", "7": "Россия", "20": "Египет", "27": "ЮАР",
    "30": "Греция", "31": "Нидерланды", "32": "Бельгия", "33": "Франция",
    "34": "Испания", "36": "Венгрия", "39": "Италия", "40": "Румыния",
    "41": "Швейцария", "43": "Австрия", "44": "Великобритания",
    "45": "Дания", "46": "Швеция", "47": "Норвегия", "48": "Польша",
    "49": "Германия", "51": "Перу", "52": "Мексика", "53": "Куба",
    "54": "Аргентина", "55": "Бразилия", "56": "Чили", "57": "Колумбия",
    "58": "Венесуэла", "60": "Малайзия", "61": "Австралия", "62": "��ндонезия",
    "63": "Филиппины", "64": "Новая Зеландия", "65": "Сингапур",
    "66": "Таиланд", "77": "Казахстан", "81": "Яп��ния", "82": "Южная Корея",
    "84": "Вьетнам", "86": "Китай", "90": "Турция", "91": "Индия",
    "92": "Пакистан", "93": "Афганистан", "94": "Шри-Ланка",
    "95": "Мьянма", "98": "Иран", "211": "Южный Судан", "212": "Марокко",
    "213": "Алжир", "216": "Тунис", "218": "Ливия", "220": "Гамбия",
    "221": "Сенегал", "222": "����авритания", "223": "Мали", "224": "Гвинея",
    "225": "Кот-д'Ивуар", "226": "Буркина-Фасо", "227": "Нигер",
    "228": "Того", "229": "Бенин", "230": "Маврикий", "231": "Либерия",
    "232": "Сьерра-Леоне", "233": "Гана", "234": "Нигерия", "235": "Чад",
    "236": "ЦАР", "237": "Камерун", "238": "Кабо-Верде",
    "239": "Сан-Томе и Принсипи", "240": "Экваториальная Гвинея",
    "241": "Габон", "242": "Конго", "243": "ДР Конго", "244": "Ангола",
    "245": "Гвинея-Бисау", "246": "Британская территория в Индийском океане",
    "247": "Остров Вознесения", "248": "Сейшелы", "249": "Судан",
    "250": "Руанда", "251": "Эфиопия", "252": "Сомали", "253": "Джибути",
    "254": "Кения", "255": "Танзания", "256": "Уганда", "257": "Бурунди",
    "258": "Мозамбик", "260": "Замбия", "261": "Мадагаскар",
    "262": "Реюньон", "263": "Зимбабве", "264": "Намибия", "265": "Малави",
    "266": "Лесото", "267": "Ботсвана", "268": "Свазиленд",
    "269": "Коморы", "290": "Острова Святой Елены", "291": "Эритрея",
    "297": "Аруба", "298": "Фареры", "299": "Гренландия", "350": "Гибралтар",
    "351": "Португалия", "352": "Люксембург", "353": "Ирландия",
    "354": "Исландия", "355": "Албания", "356": "Мальта", "357": "Кипр",
    "358": "Финляндия", "359": "Болгария", "370": "Литва", "371": "Латвия",
    "372": "Эстония", "373": "Молдова", "374": "Армения", "375": "Беларусь",
    "376": "Андорра", "377": "Монако", "378": "Сан-Марино",
    "380": "Украина", "381": "Сербия", "382": "Черногория",
    "383": "Косово", "385": "Хорватия", "386": "Словения",
    "387": "Босния и Герцеговина", "389": "Северная Македония",
    "420": "Чехия", "421": "Словакия", "423": "Лихтенштейн",
    "500": "Фолклендские острова", "501": "Белиз", "502": "Гватемала",
    "503": "Сальвадор", "504": "Гондурас", "505": "Никарагуа",
    "506": "Коста-Рика", "507": "Панама", "508": "Сен-Пьер и Микелон",
    "509": "Гаити", "590": "Гваделупа", "591": "Боливия", "592": "Гайана",
    "593": "Эквадор", "594": "Французская Гвиана", "595": "Парагвай",
    "596": "Мартиника", "597": "Суринам", "598": "Уругвай",
    "599": "Карибские Нидерланды", "670": "Восточный Тимор",
    "672": "Норфолк", "673": "Бруней", "674": "Науру", "675": "Папуа — Новая Гвинея",
    "676": "Тонга", "677": "Соломоновы Острова", "678": "Вануату",
    "679": "Фиджи", "680": "Палау", "681": "Уоллис и Футуна",
    "682": "Острова Кука", "683": "Ниуэ", "685": "Самоа", "686": "Кирибати",
    "687": "Новая Каледония", "688": "Тув��лу", "689": "Французская Полине��ия",
    "690": "Токелау", "691": "Федеративные Штаты Микронезии",
    "692": "Маршалловы Острова", "850": "КНДР", "852": "Гонконг",
    "853": "Макао", "855": "Камбоджа", "856": "Лаос", "880": "Бангладеш",
    "886": "Тайвань", "960": "Мальдивы", "961": "Ливан", "962": "Иордания",
    "963": "Сирия", "964": "Ирак", "965": "Кувейт", "966": "Саудовская Аравия",
    "967": "Йемен", "968": "Оман", "970": "Палестина", "971": "ОАЭ",
    "972": "Израиль", "973": "Бахрейн", "974": "Катар", "975": "Бутан",
    "976": "Монголия", "977": "Непал", "992": "Таджикистан",
    "993": "Туркменистан", "994": "Азербайджан", "995": "Грузия",
    "996": "Кыргызстан", "998": "Узбекистан",
}


def _detect_country_by_phone(phone: str) -> str:
    """Определяет страну по номеру телефона (логика как в bot.py)."""
    if not phone:
        return "США"
    digits = phone.strip().lstrip("+")
    # Казахстан +77 идёт первым (как в bot.py)
    if digits.startswith("77"):
        return "Казахстан"
    if digits.startswith("7"):
        return "Россия"
    # Остальные страны по коду (по убыванию длины, чтобы не срезать 1 на 7)
    for code in sorted(_PHONE_PREFIX_COUNTRY.keys(), key=len, reverse=True):
        if digits.startswith(code):
            return _PHONE_PREFIX_COUNTRY[code]
    return "США"


async def _validate_session_string_async(session_str: str) -> dict:
    """Проверка .session: подключается через Telethon, извлекает phone.

    Возвращает:
        {
            'ok': bool,
            'phone': str,                # в формате '+79..'
            'premium': bool,             # Telegram Premium на аккаунте
            'error': str                 # только при ok=False
        }

    Поле `premium` берём из `me.premium` (UserFull / User в Telethon
    экспонирует bool-флаг платной подписки). Если по какой-то причине
    атрибут недоступен (старый слой, ошибка сети) — возвращаем False,
    а не валим валидацию. Продавец всё равно сможет выставить аккаунт,
    просто бейдж «Телеграмм премиум» не появится.
    """
    if not session_str or len(session_str) < 50:
        return {'ok': False, 'error': 'Файл пустой или слишком короткий'}
    client = None
    try:
        sess = StringSession(session_str)
        client = TelegramClient(sess, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return {'ok': False, 'error': 'Сессия не авторизована'}
        me = await client.get_me()
        phone = getattr(me, 'phone', None)
        if not phone:
            return {'ok': False, 'error': 'Не удалось получить номер телефона'}
        # ====== TELEGRAM PREMIUM ======
        # me.premium — bool, True если у аккаунта активна платная подписка.
        # Безопасно приводим к bool на случай, если Telethon вернёт None /
        # int (старые версии). При любой ошибке чтения — False, чтобы
        # валидация не падала только из-за premium-флага.
        try:
            premium_flag = bool(getattr(me, 'premium', False))
        except Exception:
            premium_flag = False
        return {
            'ok': True,
            'phone': '+' + phone,
            'premium': premium_flag,
        }
    except Exception as e:
        return {'ok': False, 'error': f'Ошибка Telethon: {e}'}
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


def _validate_session_string(session_str: str) -> dict:
    """Sync-обёртка для Flask."""
    try:
        return _run_async(_validate_session_string_async(session_str), timeout=20)
    except Exception as e:
        return {'ok': False, 'error': f'Ошибка запуска: {e}'}


async def _send_code_async(phone: str) -> dict:
    """Отправляет код на телефон. Возвращает клиент и phone_code_hash."""
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    sent = await client.send_code_request(phone)
    return {"client": client, "phone_code_hash": sent.phone_code_hash}


def _create_telethon_client():
    """Создаёт новый Telethon-клиент без авторизации (для sign_in)."""
    return TelegramClient(StringSession(), API_ID, API_HASH)


def _create_listing_from_session(
    session, seller_id: int, session_string: str, phone: str,
    title: str, description: str, price: float, origin: Optional[str],
    reg_month: Optional[int] = None, reg_year: Optional[int] = None,
    has_premium: bool = False,
) -> tuple:
    """Создаёт/обновляет Account и новый Listing. Возвращает (listing, account).

    reg_month / reg_year — месяц (1..12) и год (2013..2026) регистрации
    Telegram-аккаунта, их указывает продавец при добавлении объявления.
    При обновлении существующего Account мы НЕ затираем уже сохранённые
    значения, если новые не переданы (None = «оставить как было»).

    has_premium — флаг Telegram Premium на САМОМ аккаунте (не у покупателя).
    Определяется через `client.get_me().premium` в эндпоинтах продажи
    и прокидывается сюда. Всегда обновляем при (пере)загрузке: если
    аккаунт Premium — мы и так уже подключены к нему, лучше отражать
    актуальный статус, чем держать устаревший False.
    """
    country = _detect_country_by_phone(phone)

    existing = session.execute(
        select(Account).where(Account.phone == phone)
    ).scalar_one_or_none()
    if existing:
        existing.session_string = session_string
        existing.is_verified = True
        existing.is_sold = False
        existing.seller_id = seller_id
        existing.country = country
        existing.price = price
        if origin:
            existing.origin = origin
        # Если продавец явно прислал месяц/год — обновляем. Если None —
        # оставляем прежние значения (чтобы повторная загрузка .session
        # для того же номера не стирала ранее указанную дату регистрации).
        if reg_month is not None:
            existing.reg_month = reg_month
        if reg_year is not None:
            existing.reg_year = reg_year
        # Premium-флаг перезаписываем каждый раз: если сессия живая и
        # мы смогли прочитать me — это актуальнее, чем то, что лежит в БД.
        existing.has_premium = bool(has_premium)
        account = existing
    else:
        account = Account(
            phone=phone,
            country=country,
            price=price,
            session_string=session_string,
            is_verified=True,
            is_sold=False,
            seller_id=seller_id,
            origin=origin,
            reg_month=reg_month,
            reg_year=reg_year,
            has_premium=bool(has_premium),
        )
        session.add(account)

    session.flush()

    listing = Listing(
        seller_id=seller_id,
        account_id=account.id,
        title=title,
        description=description,
        price=price,
        origin=origin,
        country=country,
        status="active",
    )
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing, account


# ===== ROUTES ДЛЯ ПРОДАЖИ =====

@app.route("/api/sell_account/session", methods=["POST"])
@require_auth
def api_sell_account_session(telegram_id, tg_user):
    """Создание объявления по .session файлу — аналог h_sell_session_file в bot.py."""
    payload = request.get_json(silent=True) or {}
    try:
        title = (payload.get("title") or "").strip()
        description = (payload.get("description") or "").strip()
        try:
            price = float(payload.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        origin = (payload.get("origin") or "").strip() or None
        session_str = payload.get("session_string") or ""
    except Exception:
        return jsonify({"ok": False, "error": "bad_payload"}), 400

    if not title or len(title) > 100:
        return jsonify({"ok": False, "error": "bad_title"}), 400
    if len(description) > 1000:
        return jsonify({"ok": False, "error": "bad_description"}), 400
    if price < MIN_LISTING_PRICE or price > MAX_LISTING_PRICE:
        return jsonify({"ok": False, "error": "bad_price",
                        "detail": f"Цена от {MIN_LISTING_PRICE:.0f} до {MAX_LISTING_PRICE:.0f}₽"}), 400
    if origin not in ("Авторег", "Саморег", "Фиши��г", "Стиллер", None):
        return jsonify({"ok": False, "error": "bad_origin"}), 400

    # ===== Месяц и год регистрации Telegram-аккаунта =====
    # Продавец указывает их в форме «Продать». Если фронт прислал
    # невалидные значения (вне 1..12 / 2013..2026) — игнорируем их
    # (NULL в БД), чтобы не падать с IntegrityError / TypeError.
    # Не делаем 400 — это опциональная информация, лучше дать объявлению
    # опубликоваться без даты регистрации, чем зарезать публикацию.
    def _parse_month(value):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= 12 else None

    def _parse_year(value):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if 2013 <= n <= 2026 else None

    reg_month = _parse_month(payload.get("reg_month"))
    reg_year  = _parse_year(payload.get("reg_year"))

    valid = _validate_session_string(session_str)
    if not valid["ok"]:
        return jsonify({"ok": False, "error": "invalid_session",
                        "detail": valid.get("error", "")}), 400

    phone = valid["phone"]
    # ====== TELEGRAM PREMIUM ======
    # При продаже по .session мы только что успешно подключились через
    # Telethon и получили me.premium. Не валим публикацию, если флага
    # нет — просто кладём False.
    has_premium = bool(valid.get("premium", False))
    session = SessionLocal()
    try:
        # Гарантируем наличие user-записи
        db_user = session.execute(
            select(User).where(User.telegram_id == telegram_id)
        ).scalar_one_or_none()
        if not db_user:
            db_user = User(telegram_id=telegram_id, username=tg_user.get("username"))
            session.add(db_user)
            session.commit()

        listing, account = _create_listing_from_session(
            session, telegram_id, session_str, phone,
            title, description, price, origin,
            reg_month=reg_month, reg_year=reg_year,
            has_premium=has_premium,
        )
        commission = round(price * COMMISSION_PERCENT / 100.0, 2)
        net = round(price - commission, 2)
        return jsonify({
            "ok": True,
            "listing_id": listing.id,
            "account_id": account.id,
            "country": account.country,
            "phone_masked": ("+" + "*" * (len(phone) - 4) + phone[-4:]) if len(phone) > 4 else phone,
            "commission": commission,
            "net": net,
            "has_premium": has_premium,
        })
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "server_error", "detail": str(e)[:200]}), 500
    finally:
        session.close()


@app.route("/api/sell_account/session_file", methods=["POST"])
@require_auth
def api_sell_account_session_file(telegram_id, tg_user):
    """Загрузка .session файла (SQLite Telethon session) и публикация объявления.

    Принимает multipart/form-data:
        session_file  — бинарный .session файл (SQLite)
        title         — название объявления (обязательно, до 100 символов)
        description   — описание (необязательно, до 1000 символов)
        price         — цена в ₽ (10..50000)
        origin        — Авторег | Саморег | Фишинг | Стиллер | пусто
        reg_month     — месяц регистрации TG-аккаунта (1..12, необязательно)
        reg_year      — год регистрации (2013..2026, необязательно)

    Алгоритм:
        1) Сохраняем файл во временный файл (mkstemp).
        2) Открываем его как SQLiteSession → получаем StringSession через
           StringSession.save(sqlite_session).
        3) Прогоняем полученную строку через _validate_session_string.
        4) При успехе — публикуем Listing через _create_listing_from_session.
        5) Удаляем временный файл.
    """
    # Чтение form-полей
    title       = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    origin      = (request.form.get("origin") or "").strip() or None
    try:
        price = float(request.form.get("price") or 0)
    except (TypeError, ValueError):
        price = 0

    def _parse_month(value):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= 12 else None

    def _parse_year(value):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if 2013 <= n <= 2026 else None

    reg_month = _parse_month(request.form.get("reg_month"))
    reg_year  = _parse_year(request.form.get("reg_year"))

    # Валидация полей
    if not title or len(title) > 100:
        return jsonify({"ok": False, "error": "bad_title"}), 400
    if len(description) > 1000:
        return jsonify({"ok": False, "error": "bad_description"}), 400
    if price < MIN_LISTING_PRICE or price > MAX_LISTING_PRICE:
        return jsonify({"ok": False, "error": "bad_price",
                        "detail": f"Цена от {MIN_LISTING_PRICE:.0f} до {MAX_LISTING_PRICE:.0f}₽"}), 400
    if origin not in ("Авторег", "Саморег", "Фишинг", "Стиллер", None):
        return jsonify({"ok": False, "error": "bad_origin"}), 400

    # Файл
    session_file = request.files.get("session_file")
    if not session_file:
        return jsonify({"ok": False, "error": "no_file",
                        "detail": "Файл .session не передан"}), 400

    filename = session_file.filename or ""
    if not filename.endswith(".session"):
        return jsonify({"ok": False, "error": "bad_file_ext",
                        "detail": "Файл должен иметь расширение .session"}), 400

    # Сохраняем во временный файл
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".session")
    try:
        os.close(tmp_fd)
        session_file.save(tmp_path)

        # Конвертируем SQLiteSession → StringSession
        try:
            sqlite_sess = SQLiteSession(tmp_path[:-8])  # SQLiteSession принимает путь БЕЗ .session
            session_str = StringSession.save(sqlite_sess)
        except Exception as e:
            return jsonify({"ok": False, "error": "bad_session_file",
                            "detail": f"Не удалось прочитать .session файл: {e}"}), 400
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not session_str or len(session_str) < 50:
        return jsonify({"ok": False, "error": "bad_session_file",
                        "detail": "Файл пустой или не содержит данных сессии"}), 400

    # Валидируем сессию через Telethon
    valid = _validate_session_string(session_str)
    if not valid["ok"]:
        return jsonify({"ok": False, "error": "invalid_session",
                        "detail": valid.get("error", "")}), 400

    phone = valid["phone"]
    has_premium = bool(valid.get("premium", False))

    session = SessionLocal()
    try:
        db_user = session.execute(
            select(User).where(User.telegram_id == telegram_id)
        ).scalar_one_or_none()
        if not db_user:
            db_user = User(telegram_id=telegram_id, username=tg_user.get("username"))
            session.add(db_user)
            session.commit()

        listing, account = _create_listing_from_session(
            session, telegram_id, session_str, phone,
            title, description, price, origin,
            reg_month=reg_month, reg_year=reg_year,
            has_premium=has_premium,
        )
        commission = round(price * COMMISSION_PERCENT / 100.0, 2)
        net = round(price - commission, 2)
        return jsonify({
            "ok": True,
            "listing_id": listing.id,
            "account_id": account.id,
            "country": account.country,
            "phone_masked": ("+" + "*" * (len(phone) - 4) + phone[-4:]) if len(phone) > 4 else phone,
            "commission": commission,
            "net": net,
            "has_premium": has_premium,
        })
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "server_error", "detail": str(e)[:200]}), 500
    finally:
        session.close()


@app.route("/api/sell_account/phone/start", methods=["POST"])
@require_auth
def api_sell_account_phone_start(telegram_id, tg_user):
    """Отправляет код подтверждения на телефон (как h_sell_phone в bot.py)."""
    payload = request.get_json(silent=True) or {}
    phone = (payload.get("phone") or "").strip()
    if not phone.startswith("+") or len(phone) < 8:
        return jsonify({"ok": False, "error": "bad_phone"}), 400

    # Сохраняем черновик объявления, если фронт его прислал.
    # Сюда включены и reg_month / reg_year, чтобы при verify/2fa они
    # попали в Account без отдельного API-вызова.
    draft = payload.get("draft") or {}

    try:
        result = _run_async(_send_code_async(phone), timeout=30)
    except Exception as e:
        return jsonify({"ok": False, "error": "send_code_failed",
                        "detail": str(e)[:200]}), 500

    # Закрываем старый клиент для этого юзера, если был
    prev = _SELL_PENDING.pop(telegram_id, None)
    if prev and prev.get("client"):
        try:
            _run_async(prev["client"].disconnect(), timeout=10)
        except Exception:
            pass

    _SELL_PENDING[telegram_id] = {
        "client": result["client"],
        "phone_code_hash": result["phone_code_hash"],
        "phone": phone,
        "draft": draft,
    }
    return jsonify({"ok": True, "phone": phone})


@app.route("/api/sell_account/phone/verify", methods=["POST"])
@require_auth
def api_sell_account_phone_verify(telegram_id, tg_user):
    """Проверяет код и создаёт Account + Listing (как h_sell_code в bot.py)."""
    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or "").strip()

    # ===== ЖЁСТКАЯ ВАЛИДАЦИЯ КОДА =====
    # Telegram-код подтверждения всегда строго 5 цифр. Без этой проверки
    # фронт пропустит любой мусор, а Telethon выкинет невнятную ошибку,
    # которая выглядит как «всё сломалось». Сверяемся с тем, как бот
    # принимает код в h_sell_code: тоже строка, тоже доверяет Telegram,
    # но бот aiogram-ом режет по message.text — тут мы режем сами.
    if not code or not code.isdigit() or len(code) != 5:
        return jsonify({
            "ok": False,
            "error": "bad_code_format",
            "detail": "Код должен состоять из 5 цифр",
        }), 400

    pending = _SELL_PENDING.get(telegram_id)
    if not pending:
        return jsonify({"ok": False, "error": "no_pending_session"}), 400
    phone = pending["phone"]
    phone_code_hash = pending["phone_code_hash"]
    client = pending["client"]

    async def _sign_in():
        return await client.sign_in(
            phone=phone, code=code, phone_code_hash=phone_code_hash
        )

    try:
        try:
            _run_async(_sign_in(), timeout=30)
        except Exception as e:
            from telethon.errors import (
                SessionPasswordNeededError,
                PhoneCodeInvalidError,
                PhoneCodeExpiredError,
            )
            if isinstance(e, SessionPasswordNeededError):
                return jsonify({
                    "ok": False,
                    "error": "need_2fa",
                    "detail": "Аккаунт защищён паролем 2FA. Введите его.",
                }), 400
            if isinstance(e, PhoneCodeInvalidError):
                return jsonify({"ok": False, "error": "bad_code",
                                "detail": "Неверный код"}), 400
            if isinstance(e, PhoneCodeExpiredError):
                return jsonify({"ok": False, "error": "code_expired",
                                "detail": "Код истёк, запросите новый"}), 400
            return jsonify({"ok": False, "error": "sign_in_failed",
                            "detail": str(e)[:200]}), 500

        # Получаем сессию и номер
        session_string = client.session.save()

        async def _get_me():
            # Одним вызовом get_me() забираем и phone, и premium — так
            # делаем один round-trip к Telegram, а не два.
            me = await client.get_me()
            return me

        me_obj = None
        try:
            me_obj = _run_async(_get_me(), timeout=15)
        except Exception:
            me_obj = None

        if me_obj is not None:
            me_phone = "+" + (getattr(me_obj, "phone", "") or "")
            # ====== TELEGRAM PREMIUM ======
            # me.premium — bool. Приводим к bool безопасно: если атрибут
            # по какой-то причине отсутствует / равен None — считаем False.
            try:
                has_premium = bool(getattr(me_obj, "premium", False))
            except Exception:
                has_premium = False
        else:
            me_phone = phone
            has_premium = False

        if not me_phone or me_phone == "+":
            me_phone = phone

        # Достаём черновик объявления
        draft = pending.get("draft") or {}
        title = (draft.get("title") or "").strip()
        description = (draft.get("description") or "").strip()
        try:
            price = float(draft.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        origin = (draft.get("origin") or "").strip() or None
        # Месяц/год регистрации — могут быть null, тогда просто не пишем
        # в Account (там тоже NULL). Это нормальный случай: продавец
        # не обязан указывать дату регистрации.
        try:
            reg_month_raw = draft.get("reg_month")
            reg_year_raw = draft.get("reg_year")
        except Exception:
            reg_month_raw = reg_year_raw = None
        reg_month = int(reg_month_raw) if reg_month_raw not in (None, "", "all") else None
        reg_year  = int(reg_year_raw)  if reg_year_raw  not in (None, "", "all") else None
        if reg_month is not None and not (1 <= reg_month <= 12):
            reg_month = None
        if reg_year is not None and not (2013 <= reg_year <= 2026):
            reg_year = None

        if not title or price < MIN_LISTING_PRICE or price > MAX_LISTING_PRICE:
            return jsonify({"ok": False, "error": "bad_draft"}), 400

        # Создаём объявление в той же БД, что и бот
        session = SessionLocal()
        try:
            db_user = session.execute(
                select(User).where(User.telegram_id == telegram_id)
            ).scalar_one_or_none()
            if not db_user:
                db_user = User(telegram_id=telegram_id, username=tg_user.get("username"))
                session.add(db_user)
                session.commit()

            listing, account = _create_listing_from_session(
                session, telegram_id, session_string, me_phone,
                title, description, price, origin,
                reg_month=reg_month, reg_year=reg_year,
                has_premium=has_premium,
            )
        finally:
            session.close()

        # Чистим pending state
        try:
            _run_async(client.disconnect(), timeout=10)
        except Exception:
            pass
        _SELL_PENDING.pop(telegram_id, None)

        commission = round(price * COMMISSION_PERCENT / 100.0, 2)
        net = round(price - commission, 2)
        return jsonify({
            "ok": True,
            "listing_id": listing.id,
            "account_id": account.id,
            "country": account.country,
            "phone_masked": ("+" + "*" * (len(me_phone) - 4) + me_phone[-4:]) if len(me_phone) > 4 else me_phone,
            "commission": commission,
            "net": net,
            "has_premium": has_premium,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error",
                        "detail": str(e)[:200]}), 500


@app.route("/api/sell_account/phone/2fa", methods=["POST"])
@require_auth
def api_sell_account_phone_2fa(telegram_id, tg_user):
    """Проверяет 2FA-пароль и завершает создание объявления (как h_sell_2fa в bot.py)."""
    payload = request.get_json(silent=True) or {}
    password = (payload.get("password") or "").strip()

    # Пароль 2FA не пустой и не короче 1 символа (Telethon сам решит)
    if not password:
        return jsonify({"ok": False, "error": "bad_password",
                        "detail": "Введите пароль 2FA"}), 400

    pending = _SELL_PENDING.get(telegram_id)
    if not pending:
        return jsonify({"ok": False, "error": "no_pending_session"}), 400
    client = pending["client"]
    phone = pending["phone"]

    async def _check_2fa():
        me = await client.sign_in(password=password)
        return me

    try:
        try:
            _run_async(_check_2fa(), timeout=30)
        except Exception as e:
            from telethon.errors import PasswordHashInvalidError
            if isinstance(e, PasswordHashInvalidError):
                return jsonify({"ok": False, "error": "bad_password",
                                "detail": "Неверный пароль 2FA"}), 400
            return jsonify({"ok": False, "error": "sign_in_failed",
                            "detail": str(e)[:200]}), 500

        session_string = client.session.save()

        async def _get_me():
            # Один get_me() — оттуда и phone, и premium. Это быстрее и
            # не делает второй сетевой запрос.
            return await client.get_me()

        me_obj = None
        try:
            me_obj = _run_async(_get_me(), timeout=15)
        except Exception:
            me_obj = None

        if me_obj is not None:
            me_phone = "+" + (getattr(me_obj, "phone", "") or "")
            # ====== TELEGRAM PREMIUM ======
            try:
                has_premium = bool(getattr(me_obj, "premium", False))
            except Exception:
                has_premium = False
        else:
            me_phone = phone
            has_premium = False

        if not me_phone or me_phone == "+":
            me_phone = phone

        draft = pending.get("draft") or {}
        title = (draft.get("title") or "").strip()
        description = (draft.get("description") or "").strip()
        try:
            price = float(draft.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        origin = (draft.get("origin") or "").strip() or None
        try:
            reg_month_raw = draft.get("reg_month")
            reg_year_raw = draft.get("reg_year")
        except Exception:
            reg_month_raw = reg_year_raw = None
        reg_month = int(reg_month_raw) if reg_month_raw not in (None, "", "all") else None
        reg_year  = int(reg_year_raw)  if reg_year_raw  not in (None, "", "all") else None
        if reg_month is not None and not (1 <= reg_month <= 12):
            reg_month = None
        if reg_year is not None and not (2013 <= reg_year <= 2026):
            reg_year = None

        if not title or price < MIN_LISTING_PRICE or price > MAX_LISTING_PRICE:
            return jsonify({"ok": False, "error": "bad_draft"}), 400

        session = SessionLocal()
        try:
            db_user = session.execute(
                select(User).where(User.telegram_id == telegram_id)
            ).scalar_one_or_none()
            if not db_user:
                db_user = User(telegram_id=telegram_id, username=tg_user.get("username"))
                session.add(db_user)
                session.commit()

            listing, account = _create_listing_from_session(
                session, telegram_id, session_string, me_phone,
                title, description, price, origin,
                reg_month=reg_month, reg_year=reg_year,
                has_premium=has_premium,
            )
        finally:
            session.close()

        try:
            _run_async(client.disconnect(), timeout=10)
        except Exception:
            pass
        _SELL_PENDING.pop(telegram_id, None)

        commission = round(price * COMMISSION_PERCENT / 100.0, 2)
        net = round(price - commission, 2)
        return jsonify({
            "ok": True,
            "listing_id": listing.id,
            "account_id": account.id,
            "country": account.country,
            "phone_masked": ("+" + "*" * (len(me_phone) - 4) + me_phone[-4:]) if len(me_phone) > 4 else me_phone,
            "commission": commission,
            "net": net,
            "has_premium": has_premium,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": "server_error",
                        "detail": str(e)[:200]}), 500


@app.route("/api/sell_account/phone/cancel", methods=["POST"])
@require_auth
def api_sell_account_phone_cancel(telegram_id, tg_user):
    """Отменяет процесс входа п�� телефону (закрывает клиент)."""
    pending = _SELL_PENDING.pop(telegram_id, None)
    if pending and pending.get("client"):
        try:
            _run_async(pending["client"].disconnect(), timeout=10)
        except Exception:
            pass
    return jsonify({"ok": True})


# ===== ПРОВЕРКА СПАМ-БЛОКА ЧЕРЕЗ @SpamBot =====

async def _check_spam_bot_async(session_string: str) -> dict:
    """Заходит на аккаунт через Telethon, пишет /start в @SpamBot,
    ждёт 4 секунды о��вета и анализирует текст.

    Возвращает:
        {"ok": True, "spam_status": "clean"}   — спам-блока нет
        {"ok": True, "spam_status": "spam"}    — спам-блок активен
        {"ok": False, "error": "..."}          — не удалось проверить
    """
    if not session_string or len(session_string) < 50:
        return {"ok": False, "error": "empty_session"}

    client = None
    try:
        sess = StringSession(session_string)
        client = TelegramClient(sess, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return {"ok": False, "error": "not_authorized"}

        # Находим @SpamBot и отправляем /start
        try:
            spambot = await client.get_entity("@SpamBot")
        except Exception as e:
            return {"ok": False, "error": f"cant_find_spambot: {e}"}

        # Сначала читаем историю, чтобы очистить непрочитанные
        await client.send_message(spambot, "/start")

        # Ждём ответ до 5 секунд (несколько попыток с интервалом 1 сек)
        response_text = None
        for _ in range(5):
            await asyncio.sleep(1)
            msgs = await client.get_messages(spambot, limit=3)
            for msg in msgs:
                # Ищем ответное сообщение от самого @SpamBot (не от нас)
                if msg and msg.out is False and msg.text:
                    response_text = msg.text.lower()
                    break
            if response_text:
                break

        if not response_text:
            return {"ok": False, "error": "no_response"}

        # Анализируем ответ @SpamBot
        # "Хороших новостей нет" / "no good news" / "limited" — есть блок
        # "Ограничений нет" / "no limits" / "Good news" — нет блока
        spam_keywords = [
            "ограничения", "ограничен", "spam", "limited",
            "no good news", "нет хороших", "хороших новостей нет",
            "you are limited", "действуют ограничения",
        ]
        clean_keywords = [
            "ограничений нет", "no limits", "no spam",
            "good news", "хорошая новость", "не имеет ограничений",
        ]

        is_spam = any(kw in response_text for kw in spam_keywords)
        is_clean = any(kw in response_text for kw in clean_keywords)

        if is_clean and not is_spam:
            return {"ok": True, "spam_status": "clean"}
        elif is_spam:
            return {"ok": True, "spam_status": "spam"}
        else:
            # Неоднозначный ответ — считаем чистым (не блокируем продажу)
            return {"ok": True, "spam_status": "clean"}

    except Exception as e:
        return {"ok": False, "error": f"telethon_error: {str(e)[:100]}"}
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


def _check_spam_bot(session_string: str) -> dict:
    """Sync-обёртка для _check_spam_bot_async."""
    try:
        return _run_async(_check_spam_bot_async(session_string), timeout=30)
    except Exception as e:
        return {"ok": False, "error": f"run_error: {str(e)[:100]}"}


@app.route("/api/spam_check", methods=["POST"])
@require_auth
def api_spam_check(telegram_id, tg_user):
    """Проверяет аккаунт продавца на спам-блок через @SpamBot.

    Принимает JSON: {"account_id": <int>}
    Только владелец объявления может проверить свой аккаунт.
    Обновляет accounts.spam_status в БД и возвращает результат.
    """
    # Ленивая миграция — на случай если колонка ещё не добавлена
    _ensure_account_spam_status_column()

    payload = request.get_json(silent=True) or {}
    try:
        account_id = int(payload.get("account_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad_account_id"}), 400

    if not account_id:
        return jsonify({"ok": False, "error": "missing_account_id"}), 400

    db_sess = SessionLocal()
    try:
        account = db_sess.execute(
            select(Account).where(Account.id == account_id)
        ).scalar_one_or_none()

        if not account:
            return jsonify({"ok": False, "error": "account_not_found"}), 404

        # Только владелец может проверить (продавец указан в seller_id)
        if account.seller_id != telegram_id:
            return jsonify({"ok": False, "error": "forbidden"}), 403

        session_string = account.session_string
        if not session_string:
            return jsonify({"ok": False, "error": "no_session"}), 400

        # Помечаем «проверяется» (оптимистично)
        account.spam_status = "checking"
        db_sess.commit()
    finally:
        db_sess.close()

    # Запускаем проверку (может занять до 30 сек)
    result = _check_spam_bot(session_string)

    # Сохраняем результат
    db_sess2 = SessionLocal()
    try:
        account2 = db_sess2.execute(
            select(Account).where(Account.id == account_id)
        ).scalar_one_or_none()
        if account2:
            if result.get("ok"):
                account2.spam_status = result["spam_status"]
            else:
                # Не удалось проверить — оставим null, чтобы можно было повторить
                account2.spam_status = None
            db_sess2.commit()
    finally:
        db_sess2.close()

    if not result.get("ok"):
        return jsonify({
            "ok": False,
            "error": result.get("error", "check_failed"),
            "account_id": account_id,
        }), 200  # 200 чтобы фронт мог обработать

    return jsonify({
        "ok": True,
        "account_id": account_id,
        "spam_status": result["spam_status"],
    })


@app.route("/api/my_listings")
@require_auth
def api_my_listings(telegram_id, tg_user):
    """Список своих объявлений — аналог cb_my_sales в bot.py."""
    session = SessionLocal()
    try:
        listings = session.execute(
            select(Listing)
            .where(Listing.seller_id == telegram_id)
            .order_by(Listing.created_at.desc())
            .limit(30)
        ).scalars().all()

        items = []
        for l in listings:
            # Подтянем Account рядом с листингом — чтобы достать
            # reg_month / reg_year для отображения «Регистрация: ...».
            acc = None
            if l.account_id:
                acc = session.execute(
                    select(Account).where(Account.id == l.account_id)
                ).scalar_one_or_none()
            reg_month = acc.reg_month if acc else None
            reg_year  = acc.reg_year  if acc else None
            items.append({
                "id": l.id,
                "account_id": acc.id if acc else None,
                "title": l.title,
                "description": build_listing_description(
                    l.description or "", reg_month, reg_year
                ) or "",
                "price": float(l.price or 0),
                "origin": l.origin or "",
                "country": l.country or "",
                "status": l.status or "active",
                "created_at": l.created_at.isoformat() if l.created_at else None,
                "reg_month": reg_month,
                "reg_year": reg_year,
                "spam_status": acc.spam_status if acc else None,
            })
        return jsonify({"ok": True, "items": items})
    finally:
        session.close()


# ===== HTML-ШАБЛОН (CSS и JS встроены) =====
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#1d4ed8">
    <title>Vest Account — Маркетплейс аккаунтов</title>
    <script async src="https://telegram.org/js/telegram-web-app.js"></script>
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
            padding-bottom: 16px;
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

        /* Сетка стран в модалке фильтров: всегда 2 колонки и СКРОЛЛ.
           36+ кнопок на маленьком экране телефона не влезают — без
           max-height/overflow-y они упираются в нижнюю кромку модалки
           и обрезаются. Скроллим ТОЛЬКО эту сетку, чтобы кнопки
           «Применить/Сбросить» внизу модалки оставались на месте. */
        .filter-grid.cols-scroll {
            grid-template-columns: repeat(2, 1fr);
            max-height: 38vh;
            overflow-y: auto;
            overflow-x: hidden;
            padding: 4px 2px 6px;
            scrollbar-width: thin;
            -webkit-overflow-scrolling: touch;
        }
        .filter-grid.cols-scroll::-webkit-scrollbar { width: 4px; }
        .filter-grid.cols-scroll::-webkit-scrollbar-thumb {
            background: rgba(15, 23, 42, 0.18); border-radius: 4px;
        }
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

        /* ====== Фильтр по дате создания аккаунта (от и до) ====== */
        .filter-date-row {
            display: grid;
            grid-template-columns: 38px 1fr 1fr;
            gap: 6px;
            align-items: center;
        }
        .filter-date-row + .filter-date-row { margin-top: 6px; }
        .filter-date-tag {
            font-size: 11px; font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            text-align: center;
            padding: 8px 0;
            background: var(--gray-50);
            border-radius: 10px;
        }
        .date-select {
            width: 100%;
            box-sizing: border-box;
            padding: 9px 8px;
            border: 1.5px solid var(--gray-200);
            border-radius: 12px;
            background: var(--bg);
            color: var(--gray-700);
            font-size: 12.5px;
            font-weight: 600;
            font-family: inherit;
            cursor: pointer;
            transition: border-color 0.16s, box-shadow 0.16s;
            appearance: none;
            -webkit-appearance: none;
            background-image:
                linear-gradient(45deg, transparent 50%, var(--gray-700) 50%),
                linear-gradient(135deg, var(--gray-700) 50%, transparent 50%);
            background-position:
                calc(100% - 14px) 50%,
                calc(100% - 9px) 50%;
            background-size: 5px 5px, 5px 5px;
            background-repeat: no-repeat;
            padding-right: 24px;
        }
        .date-select:focus {
            outline: none;
            border-color: var(--blue-500);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
        }
        .date-select.has-value {
            background: linear-gradient(135deg, var(--blue-600), var(--blue-700));
            border-color: transparent;
            color: var(--white);
            background-image:
                linear-gradient(45deg, transparent 50%, #fff 50%),
                linear-gradient(135deg, #fff 50%, transparent 50%);
        }

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
            grid-template-columns: 1fr;
            gap: 12px;
            padding: 4px 0 12px;
        }
        /* ====== Карточка товара в каталоге — редизайн ======
           Чистая плоская карточка: верх — флаг + страна + тип, середина —
           бейджи метаданных (verified, reg), низ — цена + продавец одной
           строкой. Тонкие границы, мягкая тень, без избытка декора. */
        .card {
            background: var(--surface);
            border-radius: 18px;
            padding: 14px 14px 12px;
            box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04), 0 1px 2px rgba(15, 23, 42, 0.03);
            cursor: pointer;
            transition: transform 0.18s, box-shadow 0.18s, border-color 0.18s;
            display: flex; flex-direction: column; gap: 10px;
            position: relative;
            border: 1px solid rgba(221, 228, 237, 0.9);
            animation: cardIn 0.32s ease-out backwards;
        }
        .card:hover {
            transform: translateY(-2px);
            box-shadow: 0 14px 28px rgba(15, 23, 42, 0.10), 0 4px 8px rgba(15, 23, 42, 0.04);
            border-color: rgba(91, 61, 240, 0.20);
        }
        .card > * { position: relative; z-index: 1; }
        .card:active { transform: scale(0.985); }

        /* ====== Верх карточки ======
           По новому дизайну: флаг убран, в шапке теперь название лота
           (listing.title) — крупно и читаемо. Страна и дата регистрации
           спускаются в строку характеристик-бейджей ниже. */
        .card-top {
            display: flex; align-items: flex-start; gap: 0;
        }
        .card-flag { display: none; } /* Флаг убран — оставлен как скрытый
           хук, чтобы не ломать чужие правила/скрипты, если они его ищут. */
        .card-title-block {
            display: flex; flex-direction: column; gap: 5px; min-width: 0;
            width: 100%;
        }
        .card-lot-title {
            font-size: 15.5px; font-weight: 700; color: var(--gray-900);
            line-height: 1.3;
            letter-spacing: -0.2px;
            display: -webkit-box; -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            word-break: break-word;
        }
        .card-origin {
            display: inline-flex; align-items: center; gap: 4px;
            font-size: 11px; background: var(--blue-50); color: var(--blue-700);
            padding: 3px 8px; border-radius: 7px;
            font-weight: 600; align-self: flex-start;
        }

        /* ====== Бейджи метаданных ======
           Три тега в один ряд: верифицирован, дата регистрации, страна.
           Все в одной строке, с wrap если не хватает места. По просьбе —
           дата регистрации и страна как отдельные плашки-характеристики,
           НЕ в описании. */
        .card-meta {
            display: flex; flex-wrap: wrap; gap: 5px;
            align-items: center;
        }
        .card-meta-badge {
            display: inline-flex; align-items: center; gap: 4px;
            font-size: 10.5px; font-weight: 600;
            padding: 3px 8px 3px 6px;
            border-radius: 999px;
            white-space: nowrap;
            line-height: 1;
        }
        .card-meta-badge svg { flex-shrink: 0; }
        .card-meta-badge.verified {
            background: rgba(22, 163, 74, 0.10);
            color: #15803d;
        }
        .card-meta-badge.reg {
            background: rgba(91, 61, 240, 0.08);
            color: var(--indigo-600);
        }
        /* Стиль бейджа страны в meta-строке оставлен как фоллбек,
           но страна теперь показывается отдельной строкой в шапке
           карточки (см. .card-country-line ниже). */
        .card-meta-badge.country {
            background: rgba(15, 23, 42, 0.05);
            color: var(--gray-700);
        }
        /* ====== Страна — отдельной строкой в шапке карточки ======
           Раньше страна жила в строке meta-бейджей с иконкой глобуса.
           Теперь она вынесена на отдельную строку под названием лота,
           с мини-эмодзи-флагом страны (it.flag из COUNTRY_FLAGS).
           Шрифт компактнее, цвет — спокойный серый, чтобы не отвлекал
           от названия лота, но страна считывается мгновенно. */
        .card-country-line {
            display: inline-flex; align-items: center; gap: 6px;
            align-self: flex-start;
            font-size: 12px; font-weight: 600;
            color: var(--gray-700);
            line-height: 1.1;
            padding: 2px 8px 2px 4px;
            border-radius: 7px;
            background: rgba(15, 23, 42, 0.04);
        }
        .card-country-flag {
            font-size: 14px; line-height: 1;
            display: inline-flex; align-items: center; justify-content: center;
            /* эмодзи-флаги отображаются как emoji (цветные), а не как текст */
            font-family: "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", "Twemoji Mozilla", sans-serif;
        }
        .card-country-name {
            color: var(--gray-800);
            font-weight: 600;
        }
        /* ====== TELEGRAM PREMIUM BADGE ======
           Зелёный бейдж «Телеграмм премиум» на карточке каталога.
           Рисуется в правом верхнем углу .card, чтобы не толкать
           остальной контент. Не пересекается с .card-flag за счёт
           right/top-якорения и z-index над :after-псевдо. */
        .card-premium-badge {
            position: absolute;
            top: 10px; right: 10px;
            display: inline-flex; align-items: center; gap: 4px;
            background: linear-gradient(135deg, #22c55e, #16a34a);
            color: #ffffff;
            font-size: 10.5px;
            font-weight: 700;
            letter-spacing: 0.2px;
            padding: 5px 9px 5px 7px;
            border-radius: 999px;
            box-shadow: 0 2px 8px rgba(22, 163, 74, 0.35);
            z-index: 3;
            white-space: nowrap;
        }
        .card-premium-badge::before {
            content: '★';
            font-size: 11px;
            line-height: 1;
            color: #fff;
        }
        .card-premium-badge-text {
            line-height: 1;
        }
        /* Зелёный бейдж в детальной модалке (itemModal). */
        .item-premium-badge {
            display: inline-flex; align-items: center; gap: 5px;
            background: linear-gradient(135deg, #22c55e, #16a34a);
            color: #ffffff;
            font-size: 11.5px;
            font-weight: 700;
            padding: 5px 11px 5px 9px;
            border-radius: 999px;
            margin-top: 6px;
            align-self: flex-start;
            box-shadow: 0 2px 8px rgba(22, 163, 74, 0.30);
        }
        .item-premium-badge::before {
            content: '★';
            font-size: 12px;
            line-height: 1;
            color: #fff;
        }
        /* .card-preview (последние 4 цифры номера) удалён из карточки
           по просьбе — больше не показывается. Цена идёт сразу за бейджем страны. */
        .card-price {
            font-size: 22px; font-weight: 800; color: var(--blue-700);
            margin-top: 2px;
            display: flex; align-items: baseline; gap: 4px;
            letter-spacing: -0.5px;
        }
        .card-price .rub { font-size: 14px; color: var(--text-muted); font-weight: 600; }

        /* ====== Продавец в одну строку ======
           Имя + @username + рейтинг идут в одной горизонтальной строке,
           как просили. Никогда не разбивается на 2 строки. */
        .card-seller-line {
            display: flex; align-items: center; gap: 8px;
            padding-top: 10px;
            margin-top: auto;
            border-top: 1px solid rgba(15, 23, 42, 0.06);
        }
        .card-seller-avatar {
            width: 24px; height: 24px;
            border-radius: 50%;
            background: linear-gradient(135deg, var(--blue-500), var(--indigo-600));
            color: var(--white);
            display: flex; align-items: center; justify-content: center;
            font-size: 11px; font-weight: 700;
            flex-shrink: 0;
            box-shadow: 0 2px 6px rgba(91, 61, 240, 0.20);
            overflow: hidden;
        }
        .card-seller-avatar img {
            width: 100%; height: 100%; object-fit: cover; display: block;
        }
        .card-seller-text {
            flex: 1; min-width: 0;
            display: flex; align-items: baseline; gap: 5px;
            white-space: nowrap; overflow: hidden;
            line-height: 1.15;
        }
        .card-seller-name {
            font-size: 12.5px; font-weight: 600; color: var(--gray-800);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            max-width: 60%;
        }
        /* @username в одной строке с именем, более тусклый */
        .card-seller-handle-inline {
            font-size: 11.5px; color: var(--text-muted);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            font-weight: 500;
        }
        .card-seller-rating {
            display: inline-flex; align-items: center; gap: 3px;
            font-size: 11.5px; font-weight: 700;
            color: var(--amber-500);
            background: rgba(245, 158, 11, 0.10);
            padding: 2px 7px; border-radius: 999px;
            flex-shrink: 0;
        }
        .card-seller-rating .star { font-size: 11px; line-height: 1; }
        .card-seller-rating .reviews { color: var(--text-muted); font-weight: 600; font-size: 10.5px; }
        .card-seller-rating.no-rating {
            color: var(--text-muted);
            background: var(--gray-50);
            font-weight: 600;
        }

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
        .purchases-list {
            display: grid;
            grid-template-columns: 1fr;
            gap: 12px;
            padding: 4px 16px 32px;
        }

        /* Карточка покупки — визуально идентична каталожной .card */
        .purchase-card {
            background: var(--surface);
            border-radius: 18px;
            padding: 14px 14px 12px;
            box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04), 0 1px 2px rgba(15, 23, 42, 0.03);
            cursor: pointer;
            transition: transform 0.18s, box-shadow 0.18s, border-color 0.18s;
            display: flex; flex-direction: column; gap: 10px;
            position: relative;
            border: 1px solid rgba(221, 228, 237, 0.9);
            animation: cardIn 0.32s ease-out backwards;
        }
        .purchase-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 14px 28px rgba(15, 23, 42, 0.10), 0 4px 8px rgba(15, 23, 42, 0.04);
            border-color: rgba(91, 61, 240, 0.20);
        }
        .purchase-card:active { transform: scale(0.985); }
        .purchase-card.loading { opacity: 0.7; pointer-events: none; }

        /* Верх карточки: название + страна + происхождение */
        .pcard-top { display: flex; flex-direction: column; gap: 5px; }
        .pcard-title {
            font-size: 15.5px; font-weight: 700; color: var(--gray-900);
            line-height: 1.3; letter-spacing: -0.2px;
            display: -webkit-box; -webkit-line-clamp: 2;
            -webkit-box-orient: vertical; overflow: hidden; word-break: break-word;
        }
        .pcard-country-line {
            display: inline-flex; align-items: center; gap: 6px;
            align-self: flex-start;
            font-size: 12px; font-weight: 600; color: var(--gray-700);
            line-height: 1.1; padding: 2px 8px 2px 4px;
            border-radius: 7px; background: rgba(15, 23, 42, 0.04);
        }
        .pcard-country-flag {
            font-size: 14px; line-height: 1;
            font-family: "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif;
        }
        .pcard-origin {
            display: inline-flex; align-items: center; gap: 4px;
            font-size: 11px; background: var(--blue-50); color: var(--blue-700);
            padding: 3px 8px; border-radius: 7px;
            font-weight: 600; align-self: flex-start;
        }
        /* Мета-бейджи (session, дата) */
        .pcard-meta { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
        .pcard-meta-badge {
            display: inline-flex; align-items: center; gap: 4px;
            font-size: 10.5px; font-weight: 600;
            padding: 3px 8px 3px 6px; border-radius: 999px;
            white-space: nowrap; line-height: 1;
        }
        .pcard-meta-badge.session { background: rgba(22, 163, 74, 0.10); color: #15803d; }
        .pcard-meta-badge.date    { background: rgba(91, 61, 240, 0.08); color: var(--indigo-600); }
        .pcard-meta-badge.paid    { background: rgba(15, 23, 42, 0.05); color: var(--gray-700); }

        /* Цена — крупно как в каталоге */
        .pcard-price {
            font-size: 22px; font-weight: 800; color: var(--blue-700);
            margin-top: 2px; display: flex; align-items: baseline; gap: 4px;
            letter-spacing: -0.5px;
        }
        .pcard-price .rub { font-size: 14px; color: var(--text-muted); font-weight: 600; }

        /* Нижняя строка: дата покупки */
        .pcard-footer {
            display: flex; align-items: center; gap: 8px;
            padding-top: 10px; margin-top: auto;
            border-top: 1px solid rgba(15, 23, 42, 0.06);
            font-size: 12px; color: var(--text-muted);
        }
        .pcard-footer-id { font-weight: 600; color: var(--gray-700); }
        .pcard-footer-dot { opacity: 0.4; }

        /* ====== Боттом-шит деталей покупки ====== */
        .pdetail-sheet .modal-sheet { padding-bottom: 28px; }
        .pdetail-header {
            display: flex; align-items: center; gap: 14px;
            padding: 4px 0 16px; border-bottom: 1px solid var(--gray-100); margin-bottom: 16px;
        }
        .pdetail-flag {
            width: 48px; height: 48px; border-radius: 14px;
            background: linear-gradient(135deg, var(--blue-50), var(--blue-100));
            display: flex; align-items: center; justify-content: center;
            font-size: 26px; line-height: 1; flex-shrink: 0;
            font-family: "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif;
        }
        .pdetail-info { min-width: 0; }
        .pdetail-title {
            font-size: 16px; font-weight: 700; color: var(--gray-900);
            line-height: 1.3; word-break: break-word;
        }
        .pdetail-sub { font-size: 12px; color: var(--text-muted); margin-top: 3px; }
        .pdetail-phone {
            font-size: 13px; font-weight: 700; color: var(--gray-700);
            font-variant-numeric: tabular-nums; margin-top: 4px;
            letter-spacing: 0.3px;
        }
        .pdetail-actions { display: flex; flex-direction: column; gap: 10px; }
        .pur-btn {
            width: 100%; padding: 13px 16px; border-radius: 14px;
            font-size: 14px; font-weight: 600; font-family: inherit;
            border: none; cursor: pointer;
            transition: transform .15s, opacity .15s, box-shadow .15s;
            display: inline-flex; align-items: center; justify-content: center; gap: 8px;
        }
        .pur-btn:active { transform: scale(0.97); }
        .pur-btn.primary {
            background: linear-gradient(135deg, var(--blue-600), var(--blue-700));
            color: #fff; box-shadow: 0 4px 14px rgba(37, 99, 235, 0.32);
        }
        .pur-btn.primary:hover { box-shadow: 0 6px 18px rgba(37, 99, 235, 0.42); }
        .pur-btn.secondary {
            background: var(--gray-100); color: var(--gray-900);
            border: 1px solid var(--gray-200);
        }
        .pur-btn.secondary:hover { background: var(--gray-200); }
        .pur-btn.success {
            background: linear-gradient(135deg, var(--teal-500), #0e9c8c);
            color: #fff; box-shadow: 0 4px 12px rgba(20, 184, 166, 0.32);
        }
        .pur-btn.success:hover { box-shadow: 0 6px 16px rgba(20, 184, 166, 0.42); }
        .pur-btn.danger {
            background: linear-gradient(135deg, #ff5a5f, #d9342b);
            color: #fff; box-shadow: 0 4px 12px rgba(217, 52, 43, 0.30);
        }
        .pur-btn.danger:hover { box-shadow: 0 6px 16px rgba(217, 52, 43, 0.42); }
        .pur-btn:disabled { opacity: 0.55; cursor: not-allowed; box-shadow: none; }

        /* ====== Чаты (список диалогов) ====== */
        .chats-list { display: flex; flex-direction: column; gap: 12px; padding: 0 16px 32px; }
        /* При обновлении списка чатов (polling) мы пересоздаём DOM — а
           анимация cardIn ниже срабатывает каждый раз, что раздражает.
           Класс .no-anim на контейнере отключает анимацию для всех
           дочерних .chat-card. На входе на страницу этот класс НЕ ставим,
           так что первый рендер остаётся плавным. */
        .chats-list.no-anim .chat-card { animation: none; }
        .chat-card {
            background: var(--white);
            border-radius: 18px;
            padding: 0;
            box-shadow: 0 6px 22px rgba(17, 33, 92, 0.08);
            border: 1px solid var(--gray-100);
            overflow: hidden;
            transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.2s ease;
            animation: cardIn 0.32s ease-out backwards;
            position: relative;
            cursor: pointer;
            text-align: left;
            width: 100%;
            font-family: inherit;
            color: inherit;
        }
        .chat-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 12px 32px rgba(17, 33, 92, 0.14);
        }
        .chat-card-strip {
            height: 4px;
            background: linear-gradient(90deg, var(--blue-500), var(--indigo-600), var(--violet-500));
            background-size: 220% 100%;
            animation: gradientShift 8s ease infinite;
        }
        .chat-card-body { padding: 14px 16px 16px; display: flex; align-items: center; gap: 12px; }
        .chat-card-avatar {
            width: 46px; height: 46px;
            border-radius: 14px;
            background: linear-gradient(135deg, var(--blue-50), var(--blue-100));
            display: flex; align-items: center; justify-content: center;
            font-size: 20px; font-weight: 700; color: var(--blue-700);
            flex-shrink: 0;
            box-shadow: inset 0 0 0 1px rgba(91, 61, 240, 0.10);
            text-transform: uppercase;
            position: relative;
            overflow: hidden;
        }
        .chat-card-avatar img {
            position: absolute; inset: 0;
            width: 100%; height: 100%;
            object-fit: cover; object-position: center;
            border-radius: 14px;
            display: block;
        }
        .chat-card-avatar .cc-initial.hidden { display: none; }
        .chat-card-main { flex: 1; min-width: 0; }
        .chat-card-top {
            display: flex; align-items: center; justify-content: space-between;
            gap: 8px; margin-bottom: 4px;
        }
        .chat-card-name {
            font-size: 15px; font-weight: 700; color: var(--gray-900);
            line-height: 1.2;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .chat-card-time {
            font-size: 11px; color: var(--text-muted);
            flex-shrink: 0;
            font-variant-numeric: tabular-nums;
        }
        .chat-card-subname {
            font-size: 11.5px;
            color: var(--gray-500);
            line-height: 1.15;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            margin-top: 1px;
        }
        .chat-card-preview {
            font-size: 13px; color: var(--gray-500);
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            line-height: 1.35;
        }
        .chat-card-preview.has-unread {
            color: var(--gray-900);
            font-weight: 600;
        }
        .chat-card-unread {
            min-width: 20px; height: 20px;
            border-radius: 999px;
            background: linear-gradient(135deg, var(--blue-600), var(--indigo-600));
            color: #fff;
            font-size: 11px; font-weight: 700;
            display: inline-flex; align-items: center; justify-content: center;
            padding: 0 6px;
            margin-top: 6px;
            box-shadow: 0 2px 8px rgba(37, 99, 235, 0.35);
        }

        /* ====== Модалка диалога (чат) ====== */
        .chat-sheet {
            max-width: 560px;
            width: 100%;
            height: 84vh;
            max-height: 84vh;
            display: flex;
            flex-direction: column;
            padding: 0;
            overflow: hidden;
        }
        .chat-modal-head {
            display: flex; align-items: center; gap: 12px;
            padding: 14px 16px;
            border-bottom: 1px solid var(--gray-100);
            background: var(--white);
            flex-shrink: 0;
        }
        .chat-modal-avatar {
            width: 40px; height: 40px;
            border-radius: 12px;
            background: linear-gradient(135deg, var(--blue-50), var(--blue-100));
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; color: var(--blue-700);
            text-transform: uppercase;
            flex-shrink: 0;
            position: relative;
            overflow: hidden;
        }
        .chat-modal-avatar img {
            position: absolute; inset: 0;
            width: 100%; height: 100%;
            object-fit: cover; object-position: center;
            border-radius: 12px;
            display: block;
        }
        .chat-modal-avatar .cma-initial.hidden { display: none; }
        .chat-modal-info { flex: 1; min-width: 0; }
        .chat-modal-name {
            font-size: 15px; font-weight: 700; color: var(--gray-900);
            line-height: 1.2;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .chat-modal-sub {
            font-size: 12px; color: var(--text-muted);
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .chat-modal-close {
            background: var(--gray-100); border: none;
            width: 32px; height: 32px;
            border-radius: 10px;
            font-size: 20px; line-height: 1;
            color: var(--gray-700);
            cursor: pointer;
            flex-shrink: 0;
        }
        .chat-modal-close:hover { background: var(--gray-200); }

        /* ===== Профиль чужого юзера (открывается по клику на ник/аватар) ===== */
        /* Внешний вид повторяет chatModal, но без поля ввода сообщений. */
        .user-profile-sheet {
            max-width: 480px;
            width: 100%;
            background: var(--white);
            border-radius: 24px 24px 0 0;
            padding: 0 0 20px;
            display: flex;
            flex-direction: column;
            max-height: 88vh;
            overflow: hidden;
        }
        .user-profile-head {
            display: flex; align-items: center; gap: 12px;
            padding: 18px 18px 14px;
            border-bottom: 1px solid var(--gray-100);
        }
        .user-profile-avatar {
            width: 56px; height: 56px;
            border-radius: 50%;
            background: linear-gradient(135deg, var(--blue-100), var(--blue-200, #dbeafe));
            color: var(--blue-700, #1d4ed8);
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 22px;
            flex-shrink: 0;
            overflow: hidden;
            position: relative;
        }
        .user-profile-avatar img {
            width: 100%; height: 100%;
            object-fit: cover;
            display: block;
        }
        .user-profile-info { flex: 1; min-width: 0; }
        .user-profile-name {
            font-size: 17px; font-weight: 700;
            color: var(--gray-900);
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            display: flex; align-items: center; gap: 6px;
        }
        .user-profile-admin-badge {
            background: linear-gradient(135deg, #f59e0b, #d97706);
            color: #fff;
            font-size: 10px; font-weight: 700;
            padding: 2px 6px; border-radius: 6px;
            text-transform: uppercase; letter-spacing: 0.04em;
        }
        .user-profile-username {
            font-size: 13px; color: var(--text-muted);
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .user-profile-close {
            background: var(--gray-100); border: none;
            width: 32px; height: 32px;
            border-radius: 10px;
            font-size: 20px; line-height: 1;
            color: var(--gray-700);
            cursor: pointer;
            flex-shrink: 0;
        }
        .user-profile-close:hover { background: var(--gray-200); }
        .user-profile-body {
            padding: 18px 18px 4px;
            overflow-y: auto;
            flex: 1; min-height: 0;
        }
        .user-profile-stats {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 14px;
        }
        .user-profile-stat {
            background: var(--gray-50);
            border: 1px solid var(--gray-100);
            border-radius: 14px;
            padding: 12px 14px;
        }
        .user-profile-stat-label {
            font-size: 11px; color: var(--text-muted);
            text-transform: uppercase; letter-spacing: 0.05em;
            font-weight: 600;
            margin-bottom: 4px;
        }
        .user-profile-stat-value {
            font-size: 17px; font-weight: 700; color: var(--gray-900);
        }
        .user-profile-stat-value .small {
            font-size: 12px; color: var(--text-muted);
            font-weight: 600; margin-left: 2px;
        }
        .user-profile-actions {
            display: flex; flex-direction: column; gap: 8px;
            padding: 12px 18px 0;
        }
        .user-profile-btn {
            display: flex; align-items: center; gap: 10px;
            padding: 12px 14px;
            border-radius: 14px;
            border: 1px solid var(--gray-100);
            background: var(--gray-50);
            font-size: 14px; font-weight: 600;
            color: var(--gray-900);
            cursor: pointer;
            transition: background 0.15s, transform 0.1s;
            width: 100%;
            text-align: left;
        }
        .user-profile-btn:hover { background: var(--gray-100); }
        .user-profile-btn:active { transform: scale(0.98); }
        .user-profile-btn .upb-emoji {
            width: 32px; height: 32px;
            border-radius: 10px;
            background: var(--white);
            display: flex; align-items: center; justify-content: center;
            font-size: 16px;
            flex-shrink: 0;
        }
        .user-profile-btn.primary {
            background: var(--blue-600, #1d4ed8);
            color: #fff;
            border-color: transparent;
        }
        .user-profile-btn.primary:hover { background: var(--blue-700, #1e40af); }
        .user-profile-btn.primary .upb-emoji { background: rgba(255,255,255,0.18); color: #fff; }
        .user-profile-btn.disabled,
        .user-profile-btn:disabled {
            opacity: 0.55; pointer-events: none;
        }
        .user-profile-loader {
            display: flex; align-items: center; justify-content: center;
            padding: 40px 0;
            color: var(--text-muted);
            font-size: 13px;
        }
        .user-profile-loader .spinner {
            width: 24px; height: 24px;
            border: 3px solid var(--gray-100);
            border-top-color: var(--blue-600, #1d4ed8);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin-right: 10px;
        }
        .user-profile-joined {
            font-size: 12px; color: var(--text-muted);
            text-align: center; padding: 6px 18px 0;
        }
        .profile-listings-section { padding: 0 16px 28px; }
        .profile-listings-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .profile-listings-title { font-size: 18px; font-weight: 800; color: var(--gray-900); }
        .profile-listings-count { min-width: 28px; padding: 4px 9px; border-radius: 999px; background: var(--blue-100); color: var(--blue-700); font-size: 12px; font-weight: 800; text-align: center; }
        .profile-listings-grid { display: flex; flex-direction: column; gap: 10px; }
        .profile-listing-card { width: 100%; border: 1px solid var(--gray-100); border-radius: 18px; background: var(--surface); padding: 14px; text-align: left; box-shadow: var(--shadow-sm); color: var(--text); }
        button.profile-listing-card { cursor: pointer; }
        button.profile-listing-card:active { transform: scale(.985); }
        .profile-listing-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
        .profile-listing-name { min-width: 0; font-size: 15px; line-height: 1.4; font-weight: 750; color: var(--gray-900); overflow-wrap: anywhere; }
        .profile-listing-price { flex-shrink: 0; font-size: 17px; font-weight: 850; color: var(--blue-700); }
        .profile-listing-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 9px; color: var(--text-muted); font-size: 12px; }
        .profile-listing-status { padding: 4px 8px; border-radius: 999px; font-weight: 750; }
        .profile-listing-status.active { background: #dcfce7; color: #15803d; }
        .profile-listing-status.sold { background: #fee2e2; color: #b91c1c; }
        .profile-listing-status.cancelled { background: var(--gray-100); color: var(--gray-700); }
        .profile-listings-state { padding: 28px 14px; border: 1px dashed var(--gray-200); border-radius: 18px; color: var(--text-muted); text-align: center; font-size: 14px; }
        .public-profile-hero { position: relative; padding: 28px 18px 22px; background: var(--surface); border-bottom: 1px solid var(--gray-100); }
        .public-profile-main { display: flex; flex-direction: column; align-items: center; gap: 6px; text-align: center; }
        .public-profile-avatar { width: 82px; height: 82px; border-radius: 50%; overflow: hidden; display: flex; align-items: center; justify-content: center; background: var(--blue-100); color: var(--blue-700); font-size: 30px; font-weight: 850; border: 4px solid var(--surface); box-shadow: var(--shadow); }
        .public-profile-avatar img { width: 100%; height: 100%; object-fit: cover; }
        .public-profile-name { margin-top: 5px; color: var(--gray-900); font-size: 22px; font-weight: 850; }
        .public-profile-username, .public-profile-joined { color: var(--text-muted); font-size: 13px; }
        .public-profile-content { padding: 14px 16px 28px; }
        .public-profile-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
        .public-profile-stat { padding: 13px; border: 1px solid var(--gray-100); border-radius: 16px; background: var(--surface); }
        .public-profile-stat span { display: block; color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
        .public-profile-stat b { display: block; margin-top: 3px; color: var(--gray-900); font-size: 18px; }
        .public-profile-message { margin-bottom: 22px; }

        /* Делаем ник/аватар визуально «нажимаемыми» — но без подчёркивания,
           чтобы не ломать текущий дизайн карточек. Просто курсор + лёгкий
           ховер-эффект, который намекает: «это кликабельно». */
        .card-seller-avatar,
        .card-seller-text,
        .item-seller-avatar,
        .item-seller-info,
        .chat-card-avatar,
        .chat-card-name,
        .chat-card-subname,
        .chat-modal-avatar,
        .chat-modal-info {
            cursor: pointer;
        }
        .card-seller-avatar.is-clicked,
        .card-seller-text.is-clicked,
        .item-seller-avatar.is-clicked,
        .item-seller-info.is-clicked,
        .chat-card-avatar.is-clicked,
        .chat-card-name.is-clicked,
        .chat-card-subname.is-clicked,
        .chat-modal-avatar.is-clicked,
        .chat-modal-info.is-clicked {
            opacity: 0.7;
        }
        /* На аватарках в чат-листе и в модалке уже есть лёгкий ховер — даём
           явный визуальный фидбек при наведении, чтобы было ясно: «кликни
           по мне, чтобы открыть профиль». */
        .chat-card-avatar:hover,
        .chat-modal-avatar:hover {
            transform: scale(1.04);
            transition: transform 0.12s;
        }
        .chat-messages {
            flex: 1; min-height: 0;
            overflow-y: auto;
            padding: 14px 14px 8px;
            background: linear-gradient(180deg, #f7f9ff 0%, #eef2fb 100%);
            display: flex; flex-direction: column; gap: 6px;
        }
        /* При поллинге сообщений мы то��е пересоздаём DOM — а cardIn
           ниже «подпрыгивает» каждый раз. Класс .no-anim на контейнере
           отключает анимацию для всех дочерних .chat-bubble. */
        .chat-messages.no-anim .chat-bubble { animation: none; }
        .chat-bubble {
            max-width: 78%;
            padding: 8px 12px;
            border-radius: 14px;
            font-size: 14px; line-height: 1.4;
            word-wrap: break-word; overflow-wrap: anywhere;
            box-shadow: 0 1px 2px rgba(17, 33, 92, 0.06);
            animation: cardIn 0.18s ease-out backwards;
        }
        .chat-bubble.mine {
            align-self: flex-end;
            background: linear-gradient(135deg, var(--blue-600), var(--indigo-600));
            color: #fff;
            border-bottom-right-radius: 4px;
        }
        .chat-bubble.theirs {
            align-self: flex-start;
            background: var(--white);
            color: var(--gray-900);
            border-bottom-left-radius: 4px;
            border: 1px solid var(--gray-100);
        }

        /* ===== Сообщения от Vest Account (sender_id = 0) =====
           Рисуются как «служебные карточки»: белый фон с лёгким
           индиго-бордером, мини-аватарка бота слева (та самая PNG
           из репозитория), имя «Vest Account» сверху, текст и
           опциональные кнопки действия снизу. Видно и покупателю,
           и продавцу — у каждого в своём диалоге с контрагентом. */
        .chat-bubble.bot {
            align-self: stretch;
            max-width: 92%;
            background: linear-gradient(180deg, #f7f8ff 0%, #ffffff 100%);
            color: var(--gray-900);
            border-radius: 14px;
            border: 1px solid rgba(91, 61, 240, 0.18);
            box-shadow: 0 4px 16px rgba(91, 61, 240, 0.08);
            padding: 12px 14px;
            display: flex; flex-direction: column; gap: 8px;
            position: relative;
        }
        .chat-bubble.bot::before {
            content: '';
            position: absolute; left: 0; top: 14px; bottom: 14px;
            width: 3px; border-radius: 0 3px 3px 0;
            background: linear-gradient(180deg, var(--blue-500), var(--indigo-600));
        }
        .chat-bubble-bot-head {
            display: flex; align-items: center; gap: 10px;
        }
        .chat-bubble-bot-avatar {
            width: 32px; height: 32px;
            border-radius: 50%;
            background: linear-gradient(135deg, var(--blue-500), var(--indigo-600));
            flex-shrink: 0;
            overflow: hidden;
            box-shadow: 0 2px 6px rgba(91, 61, 240, 0.28);
            display: flex; align-items: center; justify-content: center;
            color: var(--white); font-weight: 700; font-size: 13px;
            position: relative;
        }
        .chat-bubble-bot-avatar img {
            position: absolute; inset: 0;
            width: 100%; height: 100%;
            object-fit: cover;
            display: block;
        }
        .chat-bubble-bot-avatar .cb-fallback {
            position: relative; z-index: 1;
        }
        .chat-bubble-bot-avatar .cb-fallback.hidden { display: none; }
        .chat-bubble-bot-name {
            font-size: 12.5px; font-weight: 700;
            color: var(--indigo-700);
            letter-spacing: 0.2px;
        }
        .chat-bubble-bot-verified {
            font-size: 10px;
            color: var(--teal-500);
            background: rgba(20, 184, 166, 0.12);
            padding: 2px 7px;
            border-radius: 999px;
            font-weight: 700;
            margin-left: 2px;
        }
        .chat-bubble-bot-body {
            font-size: 14px; line-height: 1.45;
            color: var(--gray-900);
            word-wrap: break-word; overflow-wrap: anywhere;
            padding-left: 4px;
        }

        .chat-bubble-bot-actions {
            display: flex; flex-wrap: wrap; gap: 8px;
            padding-left: 4px;
            margin-top: 2px;
        }
        .chat-bubble-bot-btn {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 8px 14px;
            border-radius: 10px;
            border: none;
            font-family: inherit;
            font-size: 13px; font-weight: 700;
            cursor: pointer;
            background: linear-gradient(135deg, #fff1f0, #ffe5e0);
            color: #b3261e;
            box-shadow: 0 2px 6px rgba(179, 38, 30, 0.18);
            transition: transform 0.15s, box-shadow 0.15s;
        }
        .chat-bubble-bot-btn:active { transform: scale(0.96); box-shadow: 0 1px 3px rgba(179, 38, 30, 0.25); }
        .chat-bubble-bot-btn.secondary {
            background: var(--gray-100);
            color: var(--gray-700);
            box-shadow: none;
        }
        .chat-bubble-bot-btn.secondary:active { background: var(--gray-200); }

        /* ===== Отзыв: звезды + textarea ===== */
        /* Радиокнопки скрываем визуально (но оставляем в DOM для
           семантики и таб-навигации), а звезды рисуем через <label> */
        .review-stars {
            display: flex;
            justify-content: center;
            gap: 4px;
            margin: 6px 0 2px;
        }
        .review-stars input[type="radio"] {
            position: absolute;
            opacity: 0;
            width: 0; height: 0;
            pointer-events: none;
        }
        .review-star {
            font-size: 38px;
            line-height: 1;
            color: var(--gray-300);
            cursor: pointer;
            padding: 2px 4px;
            transition: transform 0.12s, color 0.12s;
            -webkit-tap-highlight-color: transparent;
            user-select: none;
        }
        .review-star:hover { transform: scale(1.08); }
        /* Активная «зажжённая» звезда: используется псевдоселектор
           :checked + label и общий .filled-до-N класс, выставляемый
           из JS (надёжнее, чем CSS-only из-за порядка DOM). */
        .review-stars.has-filled .review-star.filled { color: #f59e0b; }
        .review-stars input[type="radio"]:focus-visible + .review-star {
            outline: 2px solid var(--indigo-500);
            outline-offset: 2px;
            border-radius: 4px;
        }
        .review-stars-label {
            text-align: center;
            font-size: 12.5px;
            color: var(--text-muted);
            margin: 2px 0 12px;
            min-height: 16px;
            font-weight: 600;
        }
        .review-textarea {
            width: 100%;
            box-sizing: border-box;
            border: 1px solid var(--gray-200);
            border-radius: 12px;
            padding: 10px 12px;
            font-family: inherit;
            font-size: 14px;
            line-height: 1.4;
            color: var(--text);
            background: var(--gray-50);
            resize: vertical;
            min-height: 64px;
            max-height: 200px;
            transition: border-color 0.12s, background 0.12s;
        }
        .review-textarea:focus {
            outline: none;
            border-color: var(--indigo-500);
            background: #fff;
        }
        .review-counter {
            font-size: 11px;
            color: var(--text-muted);
            text-align: right;
            margin-top: 2px;
            margin-bottom: 6px;
        }
        .review-msg {
            font-size: 12.5px;
            text-align: center;
            min-height: 16px;
            margin: 4px 0;
            font-weight: 600;
        }
        .review-msg.error { color: #b3261e; }
        .review-msg.success { color: #16a34a; }
        .chat-bubble-bot-foot {
            font-size: 10.5px;
            color: var(--text-muted);
            opacity: 0.75;
            text-align: right;
            font-variant-numeric: tabular-nums;
        }
        .chat-bubble-time {
            display: block;
            font-size: 10.5px;
            margin-top: 4px;
            opacity: 0.7;
            font-variant-numeric: tabular-nums;
        }
        .chat-bubble.mine .chat-bubble-time { color: rgba(255, 255, 255, 0.85); }

        /* Нижня�� строка пузырька: время + галочки. Галочки показываем
           Т��ЛЬКО для «своих» сообщений (mine). Своих сообщений собеседник
           не увидит — только наш пузырь. */
        .chat-bubble-foot {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            margin-top: 4px;
            float: right;
            margin-left: 8px;
        }
        .chat-bubble-foot .chat-bubble-time {
            display: inline;
            margin-top: 0;
            float: none;
        }
        .chat-ticks {
            display: inline-flex;
            align-items: center;
            line-height: 1;
            font-size: 13px;
            /* лёгкий «шрифт-галочек» — используем обы��ные символы,
               чтобы не зависеть от наличия SVG-шрифта в системе. */
        }
        .chat-ticks.pending {
            /* 1 серая галочка — получатель ещё не открыл диалог */
            color: rgba(255, 255, 255, 0.55);
        }
        .chat-ticks.read {
            /* 2 синие галочки — получатель прочитал */
            color: #4FC3F7;
        }
        .chat-ticks svg {
            width: 14px;
            height: 14px;
            display: block;
        }
        .chat-ticks .tick-stack {
            position: relative;
            width: 18px;
            height: 14px;
            flex-shrink: 0;
        }
        .chat-ticks .tick-stack svg {
            position: absolute;
            top: 0; left: 0;
        }
        .chat-ticks .tick-stack svg:nth-child(2) {
            left: 5px;
        }
        .chat-composer {
            display: flex; align-items: flex-end; gap: 8px;
            padding: 10px 12px;
            border-top: 1px solid var(--gray-100);
            background: var(--white);
            flex-shrink: 0;
        }
        .chat-input {
            flex: 1; min-width: 0;
            border: 1px solid var(--gray-200);
            border-radius: 14px;
            padding: 10px 12px;
            font-family: inherit;
            font-size: 14px; line-height: 1.35;
            color: var(--gray-900);
            background: var(--gray-50);
            resize: none;
            max-height: 120px;
            outline: none;
            transition: border-color 0.15s, background 0.15s;
        }
        .chat-input:focus {
            border-color: var(--blue-500);
            background: var(--white);
        }
        .chat-send {
            width: 40px; height: 40px;
            border-radius: 12px;
            border: none;
            background: linear-gradient(135deg, var(--blue-600), var(--indigo-600));
            color: #fff;
            font-size: 18px;
            cursor: pointer;
            flex-shrink: 0;
            display: inline-flex; align-items: center; justify-content: center;
            box-shadow: 0 4px 12px rgba(37, 99, 235, 0.32);
            transition: transform 0.15s, box-shadow 0.15s, opacity 0.15s;
        }
        .chat-send:hover { box-shadow: 0 6px 16px rgba(37, 99, 235, 0.42); }
        .chat-send:active { transform: scale(0.95); }
        .chat-send:disabled { opacity: 0.5; cursor: not-allowed; box-shadow: none; }
        .chat-empty {
            text-align: center; color: var(--text-muted);
            padding: 24px 12px; font-size: 13px;
        }

        /* Бейдж непрочитанных в боковом меню */
        .side-menu-item .sm-badge { background: var(--blue-600); color: #fff; }
        .side-menu-item .sm-badge:empty,
        .side-menu-item .sm-badge.hidden { display: none; }
        .sm-badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; margin-left: auto; }

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
        /* ====== Бургер в шапке (правый верхний угол) ====== */
        .burger-btn {
            width: 40px; height: 40px; border-radius: 12px;
            background: rgba(255, 255, 255, 0.18);
            border: 1px solid rgba(255, 255, 255, 0.25);
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            gap: 4px; cursor: pointer; padding: 0;
            margin-left: auto;
            transition: background 0.15s, transform 0.15s;
            backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
        }
        .burger-btn:active { transform: scale(0.94); background: rgba(255, 255, 255, 0.28); }
        .burger-btn span {
            display: block; width: 18px; height: 2px;
            background: var(--white); border-radius: 2px;
            transition: transform 0.25s, opacity 0.25s;
        }
        .burger-btn.open span:nth-child(1) { transform: translateY(6px) rotate(45deg); }
        .burger-btn.open span:nth-child(2) { opacity: 0; }
        .burger-btn.open span:nth-child(3) { transform: translateY(-6px) rotate(-45deg); }

        /* ====== Боковое выезжающее меню ====== */
        .side-menu {
            position: fixed; inset: 0; z-index: 110;
            pointer-events: none;
        }
        .side-menu.open { pointer-events: auto; }
        .side-menu-backdrop {
            position: absolute; inset: 0;
            background: rgba(15, 23, 42, 0.45);
            backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
            opacity: 0; transition: opacity 0.25s ease-out;
        }
        .side-menu.open .side-menu-backdrop { opacity: 1; }
        .side-menu-panel {
            position: absolute; top: 0; right: 0; bottom: 0;
            width: 78%; max-width: 300px;
            background: var(--white);
            box-shadow: -8px 0 32px rgba(15, 23, 42, 0.18);
            transform: translateX(100%);
            transition: transform 0.28s cubic-bezier(0.32, 0.72, 0, 1);
            display: flex; flex-direction: column;
            padding-top: calc(14px + env(safe-area-inset-top));
            padding-bottom: calc(14px + env(safe-area-inset-bottom));
        }
        .side-menu.open .side-menu-panel { transform: translateX(0); }
        .side-menu-head {
            padding: 18px 20px 14px;
            border-bottom: 1px solid var(--gray-100);
        }
        .side-menu-head .sm-name {
            font-size: 17px; font-weight: 700; color: var(--gray-900);
            margin-bottom: 2px;
        }
        .side-menu-head .sm-sub {
            font-size: 12px; color: var(--text-muted);
        }
        .side-menu-list {
            display: flex; flex-direction: column;
            padding: 8px 0;
            overflow-y: auto;
            flex: 1;
        }
        .side-menu-item {
            display: flex; align-items: center; gap: 12px;
            padding: 13px 20px;
            background: none; border: none;
            text-align: left; width: 100%;
            font-family: inherit; font-size: 14.5px; font-weight: 600;
            color: var(--gray-700);
            cursor: pointer; transition: background 0.15s;
        }
        .side-menu-item:hover { background: var(--gray-50); }
        .side-menu-item.active { color: var(--indigo-600); background: rgba(91, 61, 240, 0.06); }
        .side-menu-item .sm-emoji {
            width: 28px; height: 28px; border-radius: 8px;
            background: var(--gray-100);
            display: flex; align-items: center; justify-content: center;
            font-size: 15px;
            flex-shrink: 0;
        }
        .side-menu-item.active .sm-emoji {
            background: rgba(91, 61, 240, 0.14);
        }
        .side-menu-item .sm-label { flex: 1; }
        .side-menu-item .sm-badge {
            font-size: 10px; font-weight: 700;
            padding: 3px 8px; border-radius: 999px;
            background: rgba(245, 158, 11, 0.16);
            color: #b45309;
            letter-spacing: 0.3px;
            text-transform: uppercase;
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
            /* Чтобы при большом числе стран (или маленьком экране телефона)
               модалка скроллилась внутри, а не «вылезала» за экран. */
            max-height: 92vh;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
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
        /* Кнопка «Назад» в шапках вторичных страниц.
           Раньше это была белая «таблетка» со стрелкой ← поверх синего
           градиента — на свету сливалась с фоном и читалась странно.
           Теперь — белый круг с тенью и аккуратным SVG-шевроном, который
           одинаково хорошо виден и на тёмной шапке, и на светлых
           всплывающих страницах (попап продажи, попап пополнения). */
        .profile-back {
            position: absolute; top: 14px; left: 14px;
            width: 40px; height: 40px; border-radius: 50%;
            background: rgba(255, 255, 255, 0.92);
            border: 1px solid rgba(255, 255, 255, 0.6);
            color: var(--blue-700);
            display: flex; align-items: center; justify-content: center;
            cursor: pointer; backdrop-filter: blur(10px);
            box-shadow: 0 4px 14px rgba(15, 23, 42, 0.18);
            transition: transform 0.15s ease, background 0.15s ease, box-shadow 0.15s ease;
            font-family: inherit;
            padding: 0;
            z-index: 5;
        }
        .profile-back svg { width: 18px; height: 18px; display: block; }
        .profile-back:hover { background: var(--white); box-shadow: 0 6px 18px rgba(15, 23, 42, 0.22); }
        .profile-back:active { transform: scale(0.92); background: var(--blue-50); }
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

        <!-- ====== Промокод — кнопка в профиле, открывает мини-окно (общая БД с ботом) ====== -->
        .promo-card {
            margin: 16px;
            background:
                linear-gradient(135deg, rgba(91, 61, 240, 0.08), rgba(20, 184, 166, 0.06)),
                linear-gradient(180deg, var(--surface), var(--gray-50));
            border: 1.5px solid rgba(91, 61, 240, 0.20);
            border-radius: var(--radius);
            padding: 16px 16px 14px;
            box-shadow: var(--shadow-sm);
            position: relative;
            overflow: hidden;
            transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.25s ease,
                        border-color 0.25s ease;
        }
        .promo-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 14px 32px rgba(91, 61, 240, 0.18);
            border-color: rgba(91, 61, 240, 0.32);
        }
        .promo-card:active { transform: translateY(0) scale(0.995); }
        .promo-card::before {
            content: ''; position: absolute; top: -40px; right: -40px;
            width: 140px; height: 140px;
            background: radial-gradient(circle, rgba(139, 92, 246, 0.20), transparent 65%);
            border-radius: 50%;
            pointer-events: none;
            animation: cardGlow 6s ease-in-out infinite;
        }
        .promo-card::after {
            content: '';
            position: absolute;
            top: 0; left: -120%;
            width: 60%; height: 100%;
            background: linear-gradient(120deg, transparent 0%, rgba(255,255,255,0.18) 50%, transparent 100%);
            pointer-events: none;
            transition: left 0.9s ease;
        }
        .promo-card:hover::after { left: 120%; }
        .promo-card > * { position: relative; z-index: 1; }
        .promo-card-head {
            display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
        }
        .promo-card-icon {
            width: 40px; height: 40px; border-radius: 12px;
            background: linear-gradient(135deg, var(--violet-500), var(--indigo-600));
            color: var(--white);
            display: flex; align-items: center; justify-content: center;
            font-size: 20px;
            box-shadow: 0 6px 16px rgba(91, 61, 240, 0.35);
            flex-shrink: 0;
            animation: iconBob 3.5s ease-in-out infinite;
        }
        .promo-card-title { flex: 1; min-width: 0; }
        .promo-card-name {
            font-size: 15px; font-weight: 700; color: var(--gray-900);
        }
        .promo-card-sub {
            font-size: 11.5px; color: var(--text-muted); margin-top: 2px;
        }
        .promo-card-btn {
            margin-top: 4px;
            width: auto;
            align-self: flex-start;
            padding: 8px 14px;
            font-size: 12.5px;
            font-weight: 600;
            border-radius: 999px;
            letter-spacing: 0.2px;
            box-shadow: 0 4px 14px rgba(91, 61, 240, 0.30);
            background: linear-gradient(135deg, var(--violet-500), var(--indigo-600));
            gap: 6px;
        }
        .promo-card-btn > span:first-child { font-size: 13px; }
        .promo-card-btn:hover { box-shadow: 0 6px 20px rgba(91, 61, 240, 0.42); }
        /* Promo как action-item — стильный эмодзи */
        .action-item.promo-action { position: relative; overflow: hidden; }
        .promo-emoji {
            background: linear-gradient(135deg, var(--violet-500), var(--indigo-600)) !important;
            color: var(--white);
            box-shadow: 0 4px 12px rgba(91, 61, 240, 0.35);
            animation: iconBob 3.5s ease-in-out infinite;
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

        /* ====== Страница «Продать аккаунт» ====== */
        .sell-info {
            background: linear-gradient(135deg, var(--blue-50), var(--surface));
            border: 1.5px solid var(--gray-200);
            border-radius: var(--radius);
            padding: 16px;
            margin: 0 16px 16px;
            font-size: 13px;
            line-height: 1.55;
            color: var(--gray-700);
        }
        .sell-info b { color: var(--gray-900); }
        .sell-info .sell-info-row { margin: 2px 0; }
        .sell-form-card {
            background: var(--surface);
            border: 1.5px solid var(--gray-200);
            border-radius: var(--radius);
            padding: 16px;
            margin: 0 16px 16px;
            box-shadow: var(--shadow-sm);
        }
        .sell-form-card h3 {
            margin: 0 0 12px;
            font-size: 15px;
            font-weight: 700;
            color: var(--gray-900);
            display: flex; align-items: center; gap: 8px;
        }
        .sell-label {
            display: block;
            font-size: 12px;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.4px;
            margin: 10px 0 6px;
        }
        .sell-input, .sell-textarea {
            width: 100%;
            box-sizing: border-box;
            padding: 12px 14px;
            border: 1.5px solid var(--gray-200);
            border-radius: 12px;
            background: var(--bg);
            color: var(--gray-900);
            font-size: 14px;
            font-family: inherit;
            transition: border-color 0.18s, box-shadow 0.18s;
        }
        .sell-input:focus, .sell-textarea:focus {
            outline: none;
            border-color: var(--blue-500);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
        }
        .sell-textarea { min-height: 76px; resize: vertical; }
        .sell-input.invalid, .sell-textarea.invalid {
            border-color: #ef4444;
            box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.15);
        }
        /* ====== Строка «Месяц + Год» для даты регистрации ====== */
        .sell-reg-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-top: 6px;
        }
        .sell-reg-row .sell-input {
            /* select по умолчанию выглядит иначе, чем input/textarea.
               Делаем одинаковую высоту + скругления + фокус-обводку,
               чтобы два селекта в строке читались как единое поле. */
            appearance: none;
            -webkit-appearance: none;
            background-image:
                linear-gradient(45deg, transparent 50%, var(--gray-700) 50%),
                linear-gradient(135deg, var(--gray-700) 50%, transparent 50%);
            background-position:
                calc(100% - 16px) 50%,
                calc(100% - 11px) 50%;
            background-size: 6px 6px, 6px 6px;
            background-repeat: no-repeat;
            padding-right: 30px;
        }
        .sell-reg-row .sell-input.has-value {
            border-color: var(--blue-500);
            background-color: var(--blue-50);
            color: var(--blue-700);
            font-weight: 700;
        }
        .sell-hint {
            font-size: 11.5px;
            color: var(--text-muted);
            margin-top: 4px;
        }
        .sell-hint.error { color: #ef4444; }
        .sell-origin-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
            margin-top: 6px;
        }
        .sell-origin-btn {
            padding: 10px 12px;
            border: 1.5px solid var(--gray-200);
            background: var(--bg);
            border-radius: 12px;
            font-size: 13px;
            font-weight: 600;
            color: var(--gray-700);
            cursor: pointer;
            transition: all 0.16s;
            font-family: inherit;
        }
        .sell-origin-btn.active {
            border-color: var(--blue-500);
            background: linear-gradient(135deg, var(--blue-50), var(--surface));
            color: var(--blue-600);
            box-shadow: 0 4px 12px rgba(37, 99, 235, 0.18);
        }
        .sell-mode-tabs {
            display: flex;
            gap: 8px;
            margin: 0 16px 14px;
            padding: 4px;
            background: var(--gray-100);
            border-radius: 14px;
        }
        .sell-mode-tab {
            flex: 1;
            padding: 10px 8px;
            border-radius: 10px;
            border: none;
            background: transparent;
            color: var(--text-muted);
            font-weight: 600;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.18s;
            font-family: inherit;
        }
        .sell-mode-tab.active {
            background: var(--surface);
            color: var(--gray-900);
            box-shadow: var(--shadow-sm);
        }
        .sell-file-zone {
            border: 1.5px dashed var(--gray-200);
            border-radius: 14px;
            padding: 22px 16px;
            text-align: center;
            color: var(--text-muted);
            font-size: 13px;
            background: var(--bg);
            cursor: pointer;
            transition: all 0.18s;
        }
        .sell-file-zone:hover, .sell-file-zone.drag {
            border-color: var(--blue-500);
            background: var(--blue-50);
            color: var(--blue-600);
        }
        .sell-file-zone .file-emoji { font-size: 30px; display: block; margin-bottom: 8px; }
        .sell-file-zone .file-name {
            display: block;
            margin-top: 8px;
            color: var(--gray-900);
            font-weight: 600;
            font-size: 13px;
        }
        .sell-file-zone .file-name.ok { color: var(--green-600, #16a34a); }
        .sell-file-zone .file-name.bad { color: #ef4444; }
        .sell-step-num {
            display: inline-flex;
            align-items: center; justify-content: center;
            width: 22px; height: 22px;
            background: var(--blue-500);
            color: #fff;
            border-radius: 50%;
            font-size: 12px; font-weight: 700;
            margin-right: 4px;
        }
        .sell-my-listings {
            margin: 0 16px 16px;
        }
        .sell-my-item {
            background: var(--surface);
            border: 1.5px solid var(--gray-200);
            border-radius: var(--radius);
            padding: 12px 14px;
            margin-bottom: 10px;
            box-shadow: var(--shadow-sm);
        }
        .sell-my-item .mi-row {
            display: flex; justify-content: space-between; align-items: center;
            gap: 8px;
        }
        .sell-my-item .mi-title {
            font-weight: 700;
            color: var(--gray-900);
            font-size: 14px;
            flex: 1;
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .sell-my-item .mi-price {
            font-weight: 700;
            color: var(--blue-600);
            font-size: 14px;
        }
        .sell-my-item .mi-sub {
            margin-top: 4px;
            font-size: 12px;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .sell-status-pill {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 8px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
        }
        .sell-status-pill.active { background: rgba(34, 197, 94, 0.12); color: #15803d; }
        .sell-status-pill.sold { background: rgba(239, 68, 68, 0.12); color: #b91c1c; }
        .sell-status-pill.cancelled { background: rgba(148, 163, 184, 0.18); color: #475569; }
        /* ===== Spam-статус бейдж ===== */
        .spam-badge {
            display: inline-flex; align-items: center; gap: 3px;
            font-size: 10px; font-weight: 700; padding: 2px 7px;
            border-radius: 99px; letter-spacing: 0.02em;
        }
        .spam-badge.clean {
            background: rgba(34, 197, 94, 0.13);
            color: #15803d;
            border: 1px solid rgba(34, 197, 94, 0.30);
        }
        .spam-badge.spam {
            background: rgba(239, 68, 68, 0.13);
            color: #b91c1c;
            border: 1px solid rgba(239, 68, 68, 0.30);
        }
        .spam-badge.checking {
            background: rgba(245, 158, 11, 0.13);
            color: #b45309;
            border: 1px solid rgba(245, 158, 11, 0.30);
        }
        .spam-badge-dot {
            width: 5px; height: 5px; border-radius: 50%;
            display: inline-block; flex-shrink: 0;
        }
        .spam-badge.clean .spam-badge-dot { background: #16a34a; }
        .spam-badge.spam .spam-badge-dot { background: #dc2626; }
        .spam-badge.checking .spam-badge-dot {
            background: #d97706;
            animation: pulse-dot 1.2s ease-in-out infinite;
        }
        @keyframes pulse-dot {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        /* ===== Блок статуса валидации перед покупкой ===== */
        .validate-status {
            display: flex; align-items: flex-start; gap: 10px;
            padding: 12px 14px; border-radius: 12px; margin-bottom: 10px;
            font-size: 13px; line-height: 1.5; font-weight: 500;
        }
        .validate-status.checking {
            background: rgba(245, 158, 11, 0.10);
            border: 1px solid rgba(245, 158, 11, 0.25);
            color: #92400e;
        }
        .validate-status.valid {
            background: rgba(34, 197, 94, 0.10);
            border: 1px solid rgba(34, 197, 94, 0.28);
            color: #14532d;
        }
        .validate-status.invalid {
            background: rgba(239, 68, 68, 0.10);
            border: 1px solid rgba(239, 68, 68, 0.28);
            color: #7f1d1d;
        }
        .validate-status-icon {
            font-size: 18px; flex-shrink: 0; line-height: 1.3;
        }
        .validate-status-text { flex: 1; }
        .validate-status-spinner {
            width: 18px; height: 18px; border-radius: 50%;
            border: 2px solid rgba(245,158,11,0.25);
            border-top-color: #d97706;
            animation: spin 0.8s linear infinite;
            flex-shrink: 0; margin-top: 1px;
        }
        /* Кнопка "Купить (подтвердить)" — зелёная */
        .btn-confirm-buy {
            width: 100%; padding: 14px; border: none; border-radius: 14px;
            background: linear-gradient(135deg, #16a34a 0%, #15803d 100%);
            color: #fff; font-size: 15px; font-weight: 700;
            cursor: pointer; transition: transform 0.15s, box-shadow 0.15s;
            box-shadow: 0 4px 14px rgba(22, 163, 74, 0.35);
            margin-bottom: 10px;
        }
        .btn-confirm-buy:active { transform: scale(0.97); }
        .btn-confirm-buy.loading { opacity: 0.7; pointer-events: none; }

        /* Строка с инфо о выставлении аккаунта */
        .sell-listing-progress {
            display: flex; align-items: center; gap: 8px;
            font-size: 12px; color: var(--text-muted);
            padding: 8px 12px; margin-top: 6px;
            background: rgba(91, 61, 240, 0.05);
            border-radius: 10px;
        }
        .sell-listing-progress .progress-spinner {
            width: 14px; height: 14px; border-radius: 50%;
            border: 2px solid rgba(91, 61, 240, 0.2);
            border-top-color: var(--indigo-600);
            animation: spin 0.8s linear infinite; flex-shrink: 0;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .sell-code-input {
            letter-spacing: 6px;
            text-align: center;
            font-size: 22px;
            font-weight: 700;
        }
        .sell-loader {
            display: inline-block;
            width: 16px; height: 16px;
            border: 2px solid var(--gray-200);
            border-top-color: var(--blue-500);
            border-radius: 50%;
            animation: spin 0.7s linear infinite;
            vertical-align: middle;
            margin-right: 8px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

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
            background: linear-gradient(135deg, var(--gray-50), rgba(91, 61, 240, 0.04));
            border-radius: 14px;
            border: 1px solid var(--gray-100);
        }
        .item-seller-avatar {
            width: 40px; height: 40px; border-radius: 50%;
            background: linear-gradient(135deg, var(--blue-500), var(--indigo-600));
            color: var(--white); display: flex; align-items: center; justify-content: center;
            font-size: 16px; font-weight: 700; flex-shrink: 0;
            overflow: hidden;
            box-shadow: 0 3px 10px rgba(91, 61, 240, 0.25);
        }
        .item-seller-avatar img {
            width: 100%; height: 100%; object-fit: cover; display: block;
        }
        .item-seller-info { flex: 1; min-width: 0; }
        .item-seller-name {
            font-size: 14.5px; font-weight: 700; color: var(--gray-900);
            /* ОДНОЙ СТРОКОЙ: имя + @username рядом.
               Внутри <span>name</span> + <span class="item-seller-handle-inline">@user</span>,
               перенос строки запрещён. */
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            line-height: 1.25;
            max-width: 100%;
            display: block;
        }
        /* @username в одной строке с именем — более тусклый, но не
           прячется на новую строку */
        .item-seller-handle-inline {
            font-size: 12.5px; color: var(--text-muted);
            font-weight: 500;
            white-space: nowrap;
        }
        .item-seller-handle {
            font-size: 11.5px; color: var(--text-muted);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            margin-top: 1px;
        }
        .item-seller-rating {
            font-size: 13px; color: #f59e0b; font-weight: 700;
            margin-top: 4px;
            display: inline-flex; align-items: center; gap: 3px;
        }
        .item-chat-btn {
            display: inline-flex; align-items: center; gap: 5px;
            padding: 8px 12px;
            border-radius: 12px;
            border: 1px solid var(--blue-500);
            background: var(--white);
            color: var(--blue-700);
            font-size: 12.5px; font-weight: 700;
            cursor: pointer;
            font-family: inherit;
            flex-shrink: 0;
            transition: background 0.15s, color 0.15s, transform 0.15s, box-shadow 0.15s;
        }
        .item-chat-btn:hover {
            background: linear-gradient(135deg, var(--blue-600), var(--indigo-600));
            color: #fff;
            box-shadow: 0 4px 12px rgba(37, 99, 235, 0.32);
        }
        .item-chat-btn:active { transform: scale(0.96); }
        .item-chat-btn.hidden { display: none; }
        .item-chat-btn .icb-emoji { font-size: 14px; line-height: 1; }
        /* ====== Описание аккаунта в детальной модалке — редизайн ======
           Более «продающий» hero-блок: акцентный фон, крупный текст,
           подчёркнутый заголовок, аккуратный разделитель. Конте��т,
           который рисуется внутрь (см. .item-description-grid ниже),
           живёт на подсветке по строкам — глаз сразу видит ключевые
           плюшки аккаунта, даже если продавец не оставил текста. */
        .item-description {
            font-size: 14px; color: var(--gray-700); line-height: 1.55;
            padding: 14px 14px 12px; margin-bottom: 14px;
            background: linear-gradient(135deg, var(--blue-50), #eef2ff);
            border-radius: 14px;
            border: 1px solid var(--blue-100);
            white-space: pre-wrap;
            word-wrap: break-word;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
        }
        .item-description-title {
            display: flex; align-items: center; gap: 8px;
            font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
            text-transform: uppercase; color: var(--blue-700);
            margin-bottom: 8px;
        }
        .item-description-title .item-description-icon {
            display: inline-flex; align-items: center; justify-content: center;
            width: 20px; height: 20px; border-radius: 6px;
            background: var(--blue-100); font-size: 12px;
        }
        .item-description-body {
            font-size: 14px; color: var(--gray-800);
            line-height: 1.55;
        }
        .item-description-body p { margin: 0 0 8px; }
        .item-description-body p:last-child { margin-bottom: 0; }
        .item-description-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 6px;
            margin-top: 10px;
            padding-top: 10px;
            border-top: 1px dashed rgba(91, 61, 240, 0.18);
        }
        .item-description-grid .desc-row {
            display: flex; align-items: flex-start; gap: 8px;
            font-size: 13px; color: var(--gray-700);
            line-height: 1.4;
        }
        .item-description-grid .desc-row .desc-emoji {
            flex-shrink: 0;
            width: 18px; text-align: center;
            font-size: 13px; line-height: 1.4;
        }
        .item-description-grid .desc-row .desc-text { flex: 1; min-width: 0; }
        .item-description-grid .desc-row strong { color: var(--gray-900); font-weight: 600; }
        /* ====== Блок метаданных «Сессия проверена / Регистрация» ======
           Аккуратный список с мягкими разделителями. Скрытые фичи
           (.hidden) не отображаются вовсе. */
        .item-features {
            list-style: none;
            margin-bottom: 16px;
            background: var(--surface);
            border: 1px solid var(--gray-100);
            border-radius: 14px;
            overflow: hidden;
        }
        .item-features li {
            padding: 10px 14px;
            font-size: 13.5px;
            color: var(--gray-700);
            border-bottom: 1px solid var(--gray-100);
            display: flex; align-items: center; gap: 8px;
            line-height: 1.4;
        }
        .item-features li:last-child { border-bottom: none; }
        .item-features li.hidden { display: none; }
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

        /* ====== Улучшенные анимации и переходы ====== */
        @keyframes scaleIn {
            from { opacity: 0; transform: scale(0.92); }
            to { opacity: 1; transform: scale(1); }
        }
        @keyframes slideUpSpring {
            0%   { transform: translateY(100%); }
            70%  { transform: translateY(-6px); }
            100% { transform: translateY(0); }
        }
        @keyframes shimmer {
            0%   { background-position: -200% 0; }
            100% { background-position: 200% 0; }
        }
        @keyframes glow {
            0%, 100% { box-shadow: 0 6px 20px rgba(59, 130, 246, 0.35); }
            50%      { box-shadow: 0 6px 32px rgba(91, 61, 240, 0.55); }
        }
        @keyframes ripple {
            0%   { transform: scale(0.8); opacity: 0.6; }
            100% { transform: scale(2.4); opacity: 0; }
        }
        @keyframes cardFloat {
            0%, 100% { transform: translateY(0); }
            50%      { transform: translateY(-3px); }
        }
        @keyframes cardGlow {
            0%, 100% { transform: scale(1);   opacity: 0.85; }
            50%      { transform: scale(1.15); opacity: 1; }
        }
        @keyframes iconBob {
            0%, 100% { transform: translateY(0) rotate(0); }
            50%      { transform: translateY(-3px) rotate(-6deg); }
        }
        @keyframes sheenSlide {
            0%   { transform: translateX(-120%) skewX(-18deg); }
            100% { transform: translateX(220%)  skewX(-18deg); }
        }
        @keyframes gradientShift {
            0%, 100% { background-position: 0% 50%; }
            50%      { background-position: 100% 50%; }
        }
        @keyframes barPulse {
            0%, 100% { transform: scaleX(0.6); opacity: 0.65; }
            50%      { transform: scaleX(1);   opacity: 1; }
        }
        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25%      { transform: translateX(-4px); }
            75%      { transform: translateX(4px); }
        }
        @keyframes bounceIn {
            0%   { opacity: 0; transform: scale(0.3); }
            50%  { opacity: 1; transform: scale(1.06); }
            70%  { transform: scale(0.97); }
            100% { transform: scale(1); }
        }
        @keyframes floaty {
            0%, 100% { transform: translateY(0) rotate(0); }
            50%      { transform: translateY(-4px) rotate(-1deg); }
        }
        @keyframes cardFlash {
            0%   { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.65); }
            100% { box-shadow: 0 0 0 28px rgba(34, 197, 94, 0); }
        }

        /* Карточки каталога: улучшенный переход + анимация появления */
        .card {
            animation: cardIn 0.32s ease-out backwards;
            transition: transform 0.22s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.22s ease;
        }
        .card:hover {
            transform: translateY(-4px) scale(1.015);
            box-shadow: 0 14px 36px rgba(42, 82, 212, 0.18);
        }
        .card:active { transform: scale(0.97); box-shadow: var(--shadow-sm); }
        .card.flash { animation: cardFlash 0.7s ease-out; }

        /* Primary-кнопка: shimmer-эффект при наведении */
        .btn-primary {
            position: relative;
            overflow: hidden;
        }
        .btn-primary::before {
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(
                120deg,
                transparent 0%,
                rgba(255, 255, 255, 0.32) 50%,
                transparent 100%
            );
            transform: translateX(-100%);
            transition: transform 0.5s ease;
            pointer-events: none;
        }
        .btn-primary:hover::before { transform: translateX(100%); }
        .btn-primary:active { transform: scale(0.97); }
        /* Состояние загрузки для primary */
        .btn-primary.loading {
            pointer-events: none;
            background: linear-gradient(
                90deg,
                var(--blue-600) 0%,
                var(--indigo-600) 50%,
                var(--blue-600) 100%
            );
            background-size: 200% 100%;
            animation: shimmer 1.2s linear infinite, glow 1.4s ease-in-out infinite;
        }

        /* Спиннер: gradient-оборот */
        .spinner {
            background: conic-gradient(from 0deg, var(--blue-100), var(--blue-500), var(--indigo-500), var(--blue-100));
            border: none;
            -webkit-mask: radial-gradient(circle, transparent 38%, black 39%);
                    mask: radial-gradient(circle, transparent 38%, black 39%);
            animation: spin 0.9s linear infinite;
        }

        /* Модалка: плавный slide-up с эффектом пружинки */
        .modal-sheet { animation: slideUpSpring 0.42s cubic-bezier(0.34, 1.56, 0.64, 1); }

        /* Переключение страниц: более плавный ease */
        .page.active { animation: fadeUp 0.32s cubic-bezier(0.34, 1.56, 0.64, 1); }

        /* Тряска для ошибочных состояний */
        .shake { animation: shake 0.4s ease-in-out; }

        /* Тост: bounce-in */
        .toast.show { animation: bounceIn 0.42s cubic-bezier(0.34, 1.56, 0.64, 1); }

        /* Чипы фильтров: плавный hover + лёгкая floaty в активном состоянии */
        .filter-chip {
            transition: all 0.18s cubic-bezier(0.34, 1.56, 0.64, 1);
        }
        .filter-chip:hover { transform: translateY(-2px); }
        .filter-chip.active { animation: floaty 1.6s ease-in-out infinite; }

        /* Бейдж фильтра: пульсирующее свечение */
        .filter-btn-badge:not([hidden]) { animation: glow 1.6s ease-in-out infinite; }

        /* Balance pill при обновлении */
        .balance-pill.refreshing {
            animation: pulse 0.8s ease-in-out infinite, glow 1.2s ease-in-out infinite;
        }

        /* Аватар в hero профиля — плавающий */
        .profile-hero .avatar { animation: floaty 3.5s ease-in-out infinite; }

        /* Карточки каталога: лёгкий sheen при наведении
           (имитация «радара» — пробегающее свечение слева направо).
           Удалён по просьбе — оставляем .card п��осто .card без декора. */

        /* Шапка: живой градиент (медленное смещение оттенков) */
        .app-header {
            background-size: 220% 220%;
            animation: gradientShift 14s ease infinite;
        }

        

        /* Balance pill — едва заметный glow */
        .balance-pill { animation: floaty 4s ease-in-out infinite; }

        /* Promo card — мягкое «дыхание» */
        .promo-card { animation: cardFloat 5s ease-in-out infinite; }

        /* Inputs: плавный focus-ring + scale */
        .promo-input { transition: border-color 0.18s, background 0.18s, box-shadow 0.18s, transform 0.18s; }
        .promo-input:focus { transform: scale(1.005); }

        /* Bounce-in для успешных промо-сообщений */
        .promo-msg.success { animation: bounceIn 0.5s cubic-bezier(0.34, 1.56, 0.64, 1); }

        /* Code-big — медленное свечение, чтобы привлекал внимание */
        .code-big { animation: glow 2s ease-in-out infinite; }

        /* Кликабельные элементы: лёгкое уменьшение при нажатии */
        .filter-btn:active, .balance-pill:active,
        .action-item:active, .topup-method:active, .side-menu-item:active {
            transform: scale(0.96);
        }

        /* Адаптив */
        @media (min-width: 600px) {
            .catalog-grid { grid-template-columns: 1fr; max-width: 720px; margin: 0 auto; }
        }
        @media (min-width: 900px) {
            .catalog-grid { grid-template-columns: 1fr; }
            /* Центрируем всё приложение как колонку */
            body {
                max-width: 720px;
                margin: 0 auto;
                box-shadow: 0 0 80px rgba(0,0,0,0.08);
                background-color: var(--bg);
                min-height: 100vh;
            }
            /* Шапка течёт в потоке центрированного body — никаких left/transform */
            .app-header {
                left: auto;
                transform: none;
                width: auto;
            }
            /* Боковое меню тоже прибито к правому краю колонки */
            .side-menu-panel { border-radius: 0; }
        }
        @keyframes cardIn {
            from { opacity: 0; transform: translateY(10px) scale(0.985); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }
        .card:nth-child(1) { animation-delay: 0.02s; }
        .card:nth-child(2) { animation-delay: 0.04s; }
        .card:nth-child(3) { animation-delay: 0.06s; }
        .card:nth-child(4) { animation-delay: 0.08s; }
        .card:nth-child(5) { animation-delay: 0.10s; }
        .card:nth-child(6) { animation-delay: 0.12s; }

        /* ====== Vest Account 2026 — спокойный, быстрый UI ====== */
        :root {
            --brand: #2563eb;
            --brand-dark: #173ea5;
            --accent: #14b8a6;
            --bg: #f5f7fb;
            --surface: #ffffff;
            --text: #101828;
            --text-muted: #667085;
            --gray-50: #f8fafc;
            --gray-100: #f1f5f9;
            --gray-200: #e2e8f0;
            --gray-700: #344054;
            --gray-900: #101828;
            --blue-50: #eff6ff;
            --blue-100: #dbeafe;
            --blue-500: #3b82f6;
            --blue-600: #2563eb;
            --blue-700: #1d4ed8;
            --blue-900: #172554;
            --indigo-600: #2563eb;
            --indigo-700: #1d4ed8;
            --teal-400: #2dd4bf;
            --teal-500: #14b8a6;
            --shadow-sm: 0 1px 3px rgba(16,24,40,.06);
            --shadow: 0 8px 24px rgba(16,24,40,.08);
            --shadow-lg: 0 20px 44px rgba(16,24,40,.12);
        }
        html { background: var(--bg); }
        body { background: var(--bg); background-image: none; min-height: 100%; }
        button, input, textarea, select { font: inherit; }
        button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible {
            outline: 3px solid rgba(37,99,235,.24); outline-offset: 2px;
        }
        .app-header {
            top: 0; margin: 10px 12px 0; padding: 10px 12px;
            border-radius: 18px; background: rgba(255,255,255,.94); color: var(--text);
            border: 1px solid rgba(226,232,240,.9); box-shadow: var(--shadow);
            backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
        }
        .app-header::before { display: none; }
        .avatar { width: 40px; height: 40px; border: 1px solid var(--gray-200); background: var(--blue-100); }
        .avatar-fallback { color: var(--brand-dark); font-size: 15px; }
        .user-name { color: var(--text); font-weight: 750; }
        .user-meta { color: var(--text-muted); opacity: 1; }
        .balance-pill { animation: none; background: var(--blue-50); color: var(--brand-dark); border-color: var(--blue-100); padding: 7px 10px; }
        .balance-pill .balance-cur { background: var(--brand); color: #fff; }
        .burger-btn span { background: var(--text); }
        .hero { padding: 32px 18px 10px; }
        .hero::before { content: 'VEST ACCOUNT'; display: inline-flex; color: var(--brand); font-size: 11px; font-weight: 800; letter-spacing: .14em; margin-bottom: 10px; }
        .hero-title { font-size: clamp(30px, 9vw, 42px); line-height: 1.02; letter-spacing: -.045em; color: var(--text); }
        .hero-title span { background: none; -webkit-text-fill-color: var(--brand); color: var(--brand); }
        .hero-sub { color: var(--text-muted); line-height: 1.55; }
        .section { padding: 12px 16px 4px; }
        .filter-bar { background: var(--surface); border: 1px solid var(--gray-200); border-radius: 16px; padding: 8px; box-shadow: var(--shadow-sm); }
        .filter-btn { background: var(--brand); box-shadow: none; border-radius: 11px; padding: 10px 13px; }
        .filter-summary { padding-right: 6px; }
        .section-title { font-size: 19px; letter-spacing: -.02em; }
        .section-count { color: var(--brand-dark); background: var(--blue-100); }
        .catalog-grid { gap: 10px; }
        .card { border-radius: 18px; border-color: var(--gray-200); padding: 16px; box-shadow: var(--shadow-sm); animation: cardIn .22s ease-out backwards; }
        .card:hover { border-color: var(--blue-100); box-shadow: var(--shadow); }
        .btn-primary, .filter-chip.active, .cat-pill.active { background: var(--brand); background-image: none; box-shadow: none; }
        .modal-sheet, .sell-form-card, .balance-card, .stat-card, .action-item { border: 1px solid var(--gray-200); box-shadow: var(--shadow-sm); }
        .loader { min-height: 150px; }
        .spinner { border-color: var(--gray-200); border-top-color: var(--brand); }
        .load-failure { margin: 8px 0 20px; padding: 24px 18px; background: var(--surface); border: 1px solid var(--gray-200); border-radius: 18px; text-align: center; }
        .load-failure strong { display: block; font-size: 17px; margin-bottom: 6px; }
        .load-failure p { color: var(--text-muted); margin-bottom: 14px; line-height: 1.5; }
        .load-failure button { border: 0; border-radius: 12px; background: var(--brand); color: #fff; padding: 10px 16px; font-weight: 700; }
        /* ====== Тёмная тема: сначала системная, затем ручная ====== */
        /* Базовые переменные тёмной темы вынесены в отдельный набор */
        @media (prefers-color-scheme: dark) {
            :root:not([data-theme="light"]) {
                --bg:#0b1220; --surface:#121b2b; --text:#f8fafc; --text-muted:#94a3b8;
                --gray-50:#172033; --gray-100:#1e293b; --gray-200:#293548;
                --gray-700:#cbd5e1; --gray-900:#f8fafc;
                --blue-50:#142448; --blue-100:#1c376d; --blue-900:#dbeafe;
            }
            :root:not([data-theme="light"]) .app-header { background: rgba(18,27,43,.94); }
            :root:not([data-theme="light"]) .balance-pill .balance-cur { color: #fff; }
            :root:not([data-theme="light"]) .card, :root:not([data-theme="light"]) .filter-bar { border-color: var(--gray-200); }
        }
        /* Ручная тёмная тема (переопределяет системную) */
        [data-theme="dark"] {
            --bg:#0b1220; --surface:#121b2b; --text:#f8fafc; --text-muted:#94a3b8;
            --gray-50:#172033; --gray-100:#1e293b; --gray-200:#293548;
            --gray-700:#cbd5e1; --gray-900:#f8fafc;
            --blue-50:#142448; --blue-100:#1c376d; --blue-900:#dbeafe;
        }
        [data-theme="dark"] .app-header { background: rgba(18,27,43,.94) !important; }
        [data-theme="dark"] .balance-pill .balance-cur { color: #fff; }
        [data-theme="dark"] .card, [data-theme="dark"] .filter-bar { border-color: var(--gray-200); }
        /* Ручная светлая тема (переопределяет системную тёмную) */
        [data-theme="light"] {
            --bg:#f5f7fb; --surface:#ffffff; --text:#101828; --text-muted:#667085;
            --gray-50:#f8fafc; --gray-100:#f1f5f9; --gray-200:#e2e8f0;
            --gray-700:#344054; --gray-900:#101828;
            --blue-50:#eff6ff; --blue-100:#dbeafe; --blue-900:#172554;
        }
        [data-theme="light"] .app-header { background: rgba(255,255,255,.94) !important; color: var(--text); }
        [data-theme="light"] .balance-pill .balance-cur { color: #fff; }

        /* ====== Кнопка пополнения (+) рядом с балансом ====== */
        .topup-btn {
            display: inline-flex; align-items: center; justify-content: center;
            width: 28px; height: 28px; border-radius: 50%;
            background: rgba(255,255,255,.22); border: 1px solid rgba(255,255,255,.28);
            color: inherit; font-size: 18px; font-weight: 700; line-height: 1;
            cursor: pointer; flex-shrink: 0;
            transition: background 0.18s, transform 0.14s;
            font-family: inherit; padding: 0;
        }
        .topup-btn:active { transform: scale(0.88); background: rgba(255,255,255,.38); }
        /* В светлой теме */
        .app-header:not([style*="gradient"]) .topup-btn,
        [data-theme="light"] .topup-btn,
        @media not all { /* fallback */ } {
        }
        /* Светлый стиль шапки (2026 redesign) */
        .topup-btn {
            background: var(--blue-50, rgba(255,255,255,.22));
            border-color: var(--blue-100, rgba(255,255,255,.28));
            color: var(--brand, currentColor);
        }
        [data-theme="dark"] .topup-btn,
        @media (prefers-color-scheme: dark) { /* ничего — переменные сами подхватят */ }

        /* ====== Переключатель темы в боковом меню ====== */
        .side-menu-theme-row {
            display: flex; align-items: center;
            padding: 10px 16px; gap: 10px;
            border-top: 1px solid var(--gray-100);
            margin-top: 4px;
        }
        .side-menu-theme-label { flex: 1; font-size: 14px; color: var(--text-muted); }
        .theme-toggle {
            display: flex; align-items: center;
            background: var(--gray-100); border-radius: 20px;
            padding: 2px; gap: 2px; border: none; cursor: pointer;
            font-family: inherit;
        }
        .theme-toggle-opt {
            display: flex; align-items: center; justify-content: center;
            width: 32px; height: 28px; border-radius: 16px;
            font-size: 14px; transition: background 0.18s;
            flex-shrink: 0;
        }
        .theme-toggle-opt.active {
            background: var(--surface);
            box-shadow: 0 1px 4px rgba(0,0,0,0.10);
        }

        @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; } }
    </style>
    <!-- Применяем тему до рендера, ��тобы избежать мигания (FOUC) -->
    <script>
        (function() {
            try {
                var t = localStorage.getItem('va_theme');
                if (t === 'light') document.documentElement.setAttribute('data-theme', 'light');
                else if (t === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
            } catch (e) {}
        })();
    </script>
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
            <button class="balance-pill" id="balancePill" aria-label="Перейти к профилю">
                <span class="balance-cur">&#8381;</span>
                <span id="balanceValue">—</span>
            </button>
            <button class="topup-btn" id="headerTopupBtn" type="button" aria-label="Пополнить баланс">+</button>
            <button class="burger-btn" id="burgerBtn" type="button" aria-label="Меню" aria-controls="sideMenu" aria-expanded="false">
                <span></span><span></span><span></span>
            </button>
        </header>

        <section class="hero">
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
            <button class="profile-back" id="profileBack" aria-label="Назад">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path d="M15 5 L8 12 L15 19" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </button>
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

        <div class="profile-actions">
            <button class="action-item promo-action" id="openPromoModal">
                <div class="action-emoji promo-emoji">🎁</div>
                <div class="action-text">
                    <div class="action-title">Промокод</div>
                    <div class="action-desc">Активировать и пополнить баланс</div>
                </div>
                <div class="action-arrow">›</div>
            </button>
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

        <section class="profile-listings-section" aria-labelledby="myProfileListingsTitle">
            <div class="profile-listings-head">
                <h2 class="profile-listings-title" id="myProfileListingsTitle">Мои объявления</h2>
                <span class="profile-listings-count" id="profileListingsCount">0</span>
            </div>
            <div class="profile-listings-grid" id="profileListings">
                <div class="profile-listings-state">Загружаем объявления…</div>
            </div>
        </section>
    </div>

    <!-- ====== Публичный профиль пользователя ====== -->
    <div class="page" id="pageUserProfile">
        <section class="public-profile-hero">
            <button class="profile-back" id="userProfileBack" type="button" aria-label="Назад">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path d="M15 5 L8 12 L15 19" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </button>
            <div class="public-profile-main">
                <div class="public-profile-avatar" id="userProfileAvatar"><span>?</span></div>
                <h1 class="public-profile-name" id="userProfileName">Профиль</h1>
                <div class="public-profile-username" id="userProfileUsername">Загрузка…</div>
                <div class="public-profile-joined hidden" id="userProfileJoined"></div>
            </div>
        </section>
        <div class="public-profile-content">
            <div class="public-profile-stats" id="userProfileStats">
                <div class="public-profile-stat"><span>Рейтинг</span><b id="userProfileRating">—</b></div>
                <div class="public-profile-stat"><span>Отзывы</span><b id="userProfileReviews">—</b></div>
            </div>
            <button class="btn-primary public-profile-message hidden" id="userProfileMessageBtn" type="button">Написать сообщение</button>
            <section aria-labelledby="userProfileListingsTitle">
                <div class="profile-listings-head">
                    <h2 class="profile-listings-title" id="userProfileListingsTitle">Объявления</h2>
                    <span class="profile-listings-count" id="userProfileListingsCount">0</span>
                </div>
                <div class="profile-listings-grid" id="userProfileListings">
                    <div class="profile-listings-state">Загружаем профиль…</div>
                </div>
            </section>
        </div>
    </div>

    <!-- ====== Мои покупки (полноценная страница) ====== -->
    <div class="page" id="pagePurchases">
        <div class="topup-hero">
            <button class="profile-back" id="purchasesBack" aria-label="Назад">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path d="M15 5 L8 12 L15 19" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </button>
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

    <!-- ====== Боттом-шит деталей покупки ====== -->
    <div class="modal hidden pdetail-sheet" id="purchaseDetailModal">
        <div class="modal-backdrop" data-close="purchaseDetailModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <div class="pdetail-header">
                <div class="pdetail-flag" id="pdetailFlag">&#x1F30D;</div>
                <div class="pdetail-info">
                    <div class="pdetail-title" id="pdetailTitle">Telegram-аккаунт</div>
                    <div class="pdetail-phone" id="pdetailPhone">—</div>
                    <div class="pdetail-sub" id="pdetailSub">—</div>
                </div>
            </div>
            <div class="pdetail-actions" id="pdetailActions">
                <button class="pur-btn primary" id="pdetailCodeBtn">
                    &#x1F510; Получить код подтверждения
                </button>
                <button class="pur-btn secondary" id="pdetailSessionBtn">
                    &#x1F4C4; Скачать .session файл
                </button>
                <button class="pur-btn success" id="pdetailJsonBtn">
                    &#x7B;&#x7D; Скачать JSON
                </button>
                <button class="pur-btn danger" id="pdetailDisputeBtn">
                    &#x26A0;&#xFE0F; Открыть спор
                </button>
            </div>
        </div>
    </div>

    <!-- ====== Модалка кода подтверждения ====== -->
    <div class="modal hidden" id="codeModal">
        <div class="modal-backdrop" data-close="codeModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <h3 class="modal-title" id="codeModalTitle">🔐 Код подтверждения</h3>
            <div class="code-modal-body" id="codeModalBody">
                <div class="code-phone" id="codePhone">��</div>
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

    <!-- ====== Пополнение баланса — только инфо, пополняем в боте ====== -->
    <div class="page" id="pageTopup">
        <div class="topup-hero">
            <button class="profile-back" id="topupBack" aria-label="Назад">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path d="M15 5 L8 12 L15 19" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </button>
            <div class="topup-title">Пополнение баланса</div>
            <div class="topup-sub">Пополнение происходит в боте — нажмите кнопку ниже</div>
        </div>

        <div class="topup-methods">
            <button class="btn-primary" id="topupGoBotBtn" style="margin-top: 16px;">
                <span>🤖</span><span>Открыть бота для пополнения</span>
            </button>
            <div class="topup-method-text" style="text-align: center; color: var(--text-muted); margin-top: 14px; padding: 0 16px; font-size: 13px; line-height: 1.5;">
                В боте доступны СБП, CryptoBot и банковская карта.<br>
                После оплаты баланс обновится автоматически.
            </div>
        </div>
    </div>

    <!-- ====== Продать аккаунт (P2P) — аналог раздела «Продать» в боте ====== -->
    <div class="page" id="pageSell">
        <div class="topup-hero">
            <button class="profile-back" id="sellBack" aria-label="Назад">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path d="M15 5 L8 12 L15 19" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </button>
            <div class="topup-title">💸 Продать аккаунт</div>
            <div class="topup-sub">Создайте объявление — оно появится в маркетплейсе</div>
        </div>

        <div class="sell-info">
            <div class="sell-info-row"><b>Как это работает:</b></div>
            <div class="sell-info-row">1️⃣ Укажите <b>название</b>, <b>описание</b>, <b>происхождение</b> и <b>цену</b></div>
            <div class="sell-info-row">2️⃣ Нажмите <b>«Далее»</b> — выберите <b>номер + код</b> или загрузите <b>.session файл</b></div>
            <div class="sell-info-row">3️⃣ Объявление появится в каталоге</div>
            <div class="sell-info-row" style="margin-top: 6px;">💰 Комиссия платформы: <b id="sellCommission">7%</b></div>
            <div class="sell-info-row">⏳ Деньги в холде: <b id="sellHold">24 ч.</b> после продажи</div>
        </div>

        <!-- ===== ШАГ 1: название + описание + происхождение + цена + «Далее» ===== -->
        <div class="sell-form-card" id="sellStep1">
            <h3><span class="sell-step-num">1</span>Параметры объявления</h3>

            <label class="sell-label">Название</label>
            <input class="sell-input" id="sellTitle" type="text" maxlength="100"
                   placeholder="Например: Telegram Premium 2025">
            <div class="sell-hint">До 100 символов. Это увидит покупатель.</div>

            <label class="sell-label">Описание</label>
            <textarea class="sell-textarea" id="sellDescription" maxlength="1000"
                      placeholder="Регистрация 2022, есть 2FA, активный, подписан на 50 каналов…"></textarea>
            <div class="sell-hint">До 1000 символов. Или оставьте пустым.</div>

            <!-- ===== Месяц и год регистрации Telegram-аккаунта =====
                 Эти данные указывает продавец при добавлении объявления.
                 Они автоматически попадают в описание покупателя в виде
                 строки «Регистрация: Июль 2021». Оба поля необязательные —
                 если не указаны, в описании будет только свободный текст. -->
            <label class="sell-label">Дата регистрации аккаунта</label>
            <div class="sell-reg-row">
                <select class="sell-input sell-reg-month" id="sellRegMonth">
                    <option value="">Месяц</option>
                    <option value="1">Январь</option>
                    <option value="2">Февраль</option>
                    <option value="3">Март</option>
                    <option value="4">Апрель</option>
                    <option value="5">Май</option>
                    <option value="6">Июнь</option>
                    <option value="7">Июль</option>
                    <option value="8">Август</option>
                    <option value="9">Сентябрь</option>
                    <option value="10">Октябрь</option>
                    <option value="11">Ноябрь</option>
                    <option value="12">Декабрь</option>
                </select>
                <select class="sell-input sell-reg-year" id="sellRegYear">
                    <option value="">Год</option>
                    <option value="2026">2026</option>
                    <option value="2025">2025</option>
                    <option value="2024">2024</option>
                    <option value="2023">2023</option>
                    <option value="2022">2022</option>
                    <option value="2021">2021</option>
                    <option value="2020">2020</option>
                    <option value="2019">2019</option>
                    <option value="2018">2018</option>
                    <option value="2017">2017</option>
                    <option value="2016">2016</option>
                    <option value="2015">2015</option>
                    <option value="2014">2014</option>
                    <option value="2013">2013</option>
                </select>
            </div>
            <div class="sell-hint">
                Укажите месяц и год, когда был зарегистрирован этот Telegram-аккаунт.
                Покупатель увидит «Регистрация: …» в описании объявления.
                Можно оставить пустым.
            </div>

            <label class="sell-label">Происхождение аккаунта</label>
            <div class="sell-origin-grid" id="sellOriginGrid">
                <button type="button" class="sell-origin-btn" data-origin="Авторег">🤖 Авторег</button>
                <button type="button" class="sell-origin-btn" data-origin="Саморег">👤 Саморег</button>
                <button type="button" class="sell-origin-btn" data-origin="Фишинг">🎣 Фишинг</button>
                <button type="button" class="sell-origin-btn" data-origin="Стиллер">🕵️ Стиллер</button>
            </div>
            <div class="sell-hint">Это увидят покупатели в карточке объявления.</div>

            <label class="sell-label">Цена (₽)</label>
            <input class="sell-input" id="sellPrice" type="number" min="10" max="50000"
                   placeholder="Например: 500">
            <div class="sell-hint" id="sellPriceHint">От 10 до 50 000 ₽. Комиссия 7% будет удержана при продаже.</div>

            <button type="button" class="btn-primary" id="sellNextBtn" style="margin-top: 16px;">
                <span>Далее</span><span>→</span>
            </button>
            <div class="sell-hint" id="sellStep1Error" style="color:#ef4444; margin-top:6px; display:none;"></div>
        </div>

        <!-- ===== ШАГ 2: выбор режима + ввод данных ===== -->
        <div class="sell-form-card" id="sellStep2" style="display:none;">
            <h3><span class="sell-step-num">2</span>Вход в аккаунт</h3>

            <!-- Переключатель режима: номер+код / .session файл -->
            <div class="sell-mode-tabs" role="tablist" aria-label="Способ добавления аккаунта">
                <button type="button" class="sell-mode-tab active" id="sellModeTabPhone"
                        role="tab" aria-selected="true" aria-controls="sellPhoneSection"
                        data-mode="phone">
                    📱 Номер + код
                </button>
                <button type="button" class="sell-mode-tab" id="sellModeTabFile"
                        role="tab" aria-selected="false" aria-controls="sellFileSection"
                        data-mode="file">
                    📂 .session файл
                </button>
            </div>

            <!-- ---- Режим: номер + код ---- -->
            <div id="sellPhoneSection" role="tabpanel" aria-labelledby="sellModeTabPhone">
                <label class="sell-label">Номер телефона</label>
                <input class="sell-input" id="sellPhone" type="tel" placeholder="+79001234567">
                <div class="sell-hint">В международном формате с «+». На этот номер придёт код Telegram.</div>

                <button type="button" class="btn-primary" id="sellPhoneSendBtn" style="margin-top: 14px;">
                    <span>📨</span><span>Отправить код</span>
                </button>

                <div id="sellCodeWrap" style="display:none; margin-top: 18px;">
                    <label class="sell-label">Код подтверждения</label>
                    <input class="sell-input sell-code-input" id="sellCode" type="text"
                           inputmode="numeric" pattern="\d{5}" maxlength="5" placeholder="00000"
                           autocomplete="one-time-code">
                    <div class="sell-hint">Код придёт в Telegram на указанный номер. Ровно 5 цифр.</div>

                    <button type="button" class="btn-primary" id="sellPublishBtn" style="margin-top: 14px;">
                        <span id="sellPublishBtnText">Подтвердить</span>
                    </button>
                </div>

                <!-- 2FA (показывается бэкендом, если аккаунт защищён паролем) -->
                <div id="sell2faWrap" style="display:none; margin-top: 14px;">
                    <label class="sell-label">Пароль 2FA (облачный пароль)</label>
                    <input class="sell-input" id="sell2fa" type="password" placeholder="••••••••">
                    <div class="sell-hint">Аккаунт защищён 2FA. Введите облачный пароль и снова нажмите «Подтвердить».</div>
                    <button type="button" class="btn-primary" id="sellPublish2faBtn" style="margin-top: 14px;">
                        <span id="sellPublish2faBtnText">Подтвердить 2FA</span>
                    </button>
                </div>
            </div>

            <!-- ---- Режим: .session файл ---- -->
            <div id="sellFileSection" role="tabpanel" aria-labelledby="sellModeTabFile" style="display:none;">
                <!-- Drag-and-drop / click зона -->
                <div class="sell-file-zone" id="sellFileZone" tabindex="0" role="button"
                     aria-label="Нажмите или перетащите .session файл для загрузки">
                    <span class="file-emoji">📂</span>
                    <span>Нажмите или перетащите <b>.session</b> файл сюда</span>
                    <span class="file-name" id="sellFileName"></span>
                </div>
                <!-- Скрытый file input -->
                <input type="file" id="sellFileInput" accept=".session"
                       style="display:none;" aria-hidden="true">
                <div class="sell-hint" style="margin-top: 8px;">
                    Принимаются только файлы Telethon с расширением <b>.session</b>.
                    Файл будет проверен на активность перед публикацией.
                </div>

                <button type="button" class="btn-primary" id="sellFilePublishBtn"
                        style="margin-top: 14px;" disabled>
                    <span id="sellFilePublishBtnText">Проверить и опубликовать</span>
                </button>
            </div>

            <div class="sell-hint" id="sellStep2Error" style="color:#ef4444; margin-top:10px; display:none;"></div>

            <button type="button" class="btn-secondary" id="sellBackBtn" style="margin-top: 14px;">
                &#8592; Назад
            </button>
        </div>

        <!-- Итоговая сводка (виден только на шаге 2, для подтверждения) -->
        <div class="sell-form-card" id="sellSummaryCard" style="display:none;">
            <h3>📋 Проверьте объявление</h3>
            <div id="sellSummary" style="font-size: 13.5px; line-height: 1.7; color: var(--gray-700);">
                Заполните шаг 1 — здесь появится сводка.
            </div>
            <button type="button" class="btn-secondary" id="sellCancelBtn" style="margin-top: 14px;">
                Отменить и начать заново
            </button>
        </div>

        <!-- Мои объявления -->
        <div class="sell-my-listings">
            <h3 style="font-size: 14px; font-weight: 700; color: var(--gray-900); margin: 4px 0 10px;">
                📋 Мои объявления
            </h3>
            <div id="sellMyListings"></div>
        </div>
    </div>

    <!-- ====== Чаты (P2P диалоги с продавцами) ====== -->
    <div class="page" id="pageChats">
        <div class="topup-hero">
            <button class="profile-back" id="chatsBack" aria-label="Назад">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path d="M15 5 L8 12 L15 19" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </button>
            <div class="topup-title">💬 Чаты</div>
            <div class="topup-sub">Диалоги с продавцами и покупателями</div>
        </div>

        <div class="chats-list" id="chatsList">
            <!-- Карточки диалогов рендерятся JS-ом -->
        </div>

        <div class="loader hidden" id="chatsLoader">
            <div class="spinner"></div>
        </div>

        <div class="empty-state hidden" id="chatsEmpty">
            <div class="empty-emoji">💬</div>
            <p>У вас пока нет диалогов</p>
            <p class="empty-sub">Откройте карточку аккаунта и нажмите «Написать продавцу»</p>
        </div>
    </div>

    <!-- ====== Модалка диалога с польз��вателем ====== -->
    <div class="modal hidden" id="chatModal">
        <div class="modal-backdrop" data-close="chatModal"></div>
        <div class="modal-sheet chat-sheet">
            <div class="modal-handle"></div>
            <div class="chat-modal-head">
                <div class="chat-modal-avatar" id="chatModalAvatar">👤</div>
                <div class="chat-modal-info">
                    <div class="chat-modal-name" id="chatModalName">Собеседник</div>
                    <div class="chat-modal-sub" id="chatModalSub">telegram</div>
                </div>
                <button class="chat-modal-close" data-close="chatModal" aria-label="Закрыть">×</button>
            </div>
            <div class="chat-messages" id="chatMessages">
                <!-- Сообщения рендерятся JS-ом -->
            </div>
            <form class="chat-composer" id="chatComposer" autocomplete="off">
                <textarea class="chat-input" id="chatInput" rows="1" maxlength="4000"
                          placeholder="Напишите сообщение…"></textarea>
                <button type="submit" class="chat-send" id="chatSend" aria-label="Отправить">➤</button>
            </form>
        </div>
    </div>

    <!-- ====== Профиль чужого пользователя (открывается по клику на ник/аватар) ====== -->
    <!--
        Отдельная модалка, чтобы не путать с pageProfile (там показан СВОЙ
        профиль с балансом и холдом). Здесь — только публичная информация:
        имя, @username, аватар, рейтинг, отзывы, дата регистрации.
        Поддерживает две кнопки действия: «Написать» (создаёт/открывает ча��
        с этим юзером) и «Открыть своего профиля» (если кликнули по себе).
    -->
    <div class="modal hidden" id="userModal">
        <div class="modal-backdrop" data-close="userModal"></div>
        <div class="modal-sheet user-profile-sheet">
            <div class="modal-handle"></div>
            <div class="user-profile-head">
                <div class="user-profile-avatar" id="userModalAvatar">
                    <span class="up-initial" id="userModalInitial">?</span>
                </div>
                <div class="user-profile-info">
                    <div class="user-profile-name" id="userModalName">—</div>
                    <div class="user-profile-username" id="userModalUsername">—</div>
                </div>
                <button class="user-profile-close" data-close="userModal" aria-label="Закрыть">×</button>
            </div>
            <div class="user-profile-body" id="userModalBody">
                <div class="user-profile-loader" id="userModalLoader">
                    <span class="spinner"></span>
                    <span>Загружаем профиль…</span>
                </div>
                <div class="user-profile-stats hidden" id="userModalStats">
                    <div class="user-profile-stat">
                        <div class="user-profile-stat-label">Рейтинг</div>
                        <div class="user-profile-stat-value" id="userModalRating">5.0<span class="small">★</span></div>
                    </div>
                    <div class="user-profile-stat">
                        <div class="user-profile-stat-label">Отзывы</div>
                        <div class="user-profile-stat-value" id="userModalReviews">0</div>
                    </div>
                </div>
                <div class="user-profile-joined hidden" id="userModalJoined"></div>
            </div>
            <div class="user-profile-actions hidden" id="userModalActions">
                <button class="user-profile-btn primary hidden" id="userModalMessageBtn" type="button">
                    <span class="upb-emoji">💬</span>
                    <span>Написать сообщение</span>
                </button>
                <button class="user-profile-btn hidden" id="userModalSelfBtn" type="button">
                    <span class="upb-emoji">👤</span>
                    <span>Это вы — открыть свой профиль</span>
                </button>
            </div>
        </div>
    </div>

    <!-- Боковое выезжающее меню (открывается по бургеру в шапке) -->
    <div class="side-menu" id="sideMenu" aria-hidden="true">
        <div class="side-menu-backdrop" id="sideMenuBackdrop"></div>
        <aside class="side-menu-panel" role="dialog" aria-label="Меню">
            <div class="side-menu-head">
                <div class="sm-name">Меню</div>
                <div class="sm-sub">Vest Account</div>
            </div>
            <div class="side-menu-list">
                <button class="side-menu-item active" data-page="pageCatalog">
                    <span class="sm-emoji">🛍️</span><span class="sm-label">Каталог</span>
                </button>
                <button class="side-menu-item" data-page="pageSell">
                    <span class="sm-emoji">💸</span><span class="sm-label">Продать</span>
                </button>
                <button class="side-menu-item" data-page="pagePurchases">
                    <span class="sm-emoji">📦</span><span class="sm-label">Мои покупки</span>
                </button>
                <button class="side-menu-item" data-page="pageProfile">
                    <span class="sm-emoji">👤</span><span class="sm-label">Профиль</span>
                </button>
                <button class="side-menu-item" id="sideMenuChats" data-page="pageChats">
                    <span class="sm-emoji">💬</span><span class="sm-label">Чаты</span>
                    <span class="sm-badge hidden" id="sideMenuChatsBadge">0</span>
                </button>
                <button class="side-menu-item" id="sideMenuSupport">
                    <span class="sm-emoji">❓</span><span class="sm-label">Помощь</span>
                </button>
                <button class="side-menu-item" id="sideMenuClose">
                    <span class="sm-emoji">&#x21A9;&#xFE0F;</span><span class="sm-label">Закрыть меню</span>
                </button>
            </div>
            <!-- Переключатель темы -->
            <div class="side-menu-theme-row">
                <span class="side-menu-theme-label">Тема</span>
                <button class="theme-toggle" id="themeToggle" type="button" aria-label="Переключить тему">
                    <span class="theme-toggle-opt active" data-theme-val="light" aria-label="Светлая">&#9728;&#65039;</span>
                    <span class="theme-toggle-opt" data-theme-val="dark" aria-label="Тёмная">&#127765;</span>
                    <span class="theme-toggle-opt" data-theme-val="auto" aria-label="Системная">&#10036;&#65039;</span>
                </button>
            </div>
        </aside>
    </div>

    <!-- Модалка фильтров (все страны + происхождение + цена) -->
    <div class="modal hidden" id="filtersModal">
        <div class="modal-backdrop" data-close="filtersModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <h3 class="modal-title">🎛️ Фильтры</h3>

            <div class="filter-modal-section">
                <div class="filter-modal-label">Страна</div>
                <!-- cols-scroll: 2 колонки + вертикальный скролл внутри сетки,
                     чтобы 36+ стран не растягивали модалку на весь экран. -->
                <div class="filter-grid cols-scroll" id="filtersCountryGrid">
                    <!-- страны рендерятся JS-ом: первая кнопка «Все» + все страны -->
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

            <!-- Новая секция: дата создания аккаунта (от и до: месяц + год) -->
            <div class="filter-modal-section">
                <div class="filter-modal-label">📅 Дата создания аккаунта</div>
                <div class="filter-date-row">
                    <span class="filter-date-tag">От</span>
                    <select class="date-select" id="filterFromMonth" data-kind="fromMonth">
                        <option value="all">Месяц</option>
                        <!-- 1..12 рендерятся JS-ом -->
                    </select>
                    <select class="date-select" id="filterFromYear" data-kind="fromYear">
                        <option value="all">Год</option>
                        <!-- 2013..2026 рендерятся JS-ом -->
                    </select>
                </div>
                <div class="filter-date-row">
                    <span class="filter-date-tag">До</span>
                    <select class="date-select" id="filterToMonth" data-kind="toMonth">
                        <option value="all">Месяц</option>
                    </select>
                    <select class="date-select" id="filterToYear" data-kind="toYear">
                        <option value="all">Год</option>
                    </select>
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
                    <!-- Сю��а фронт рендерит «Имя Фамилия @username» одной строкой -->
                    <div class="item-seller-name" id="itemSeller">Продавец</div>
                    <!-- Старый stack-handle спрятан: ник теперь идёт инлайном -->
                    <div class="item-seller-handle hidden" id="itemSellerHandle"></div>
                    <div class="item-seller-rating" id="itemRating">— ★</div>
                </div>
                <button class="item-chat-btn hidden" id="itemChatBtn" type="button">
                    <span class="icb-emoji">💬</span><span>Написать</span>
                </button>
            </div>

            <div class="item-description" id="itemDescription">Описание появится после загрузки.</div>

            <!-- Метаданные аккаунта: верификация, дата регистрации, премиум.
                 Дата регистрации теперь НЕ в описании, а отдельной строкой —
                 как и просили (рядом с «Сессия верифицирована»). -->
            <ul class="item-features" id="itemFeatures">
                <li data-feature="verified">✅ Сессия прошла верификацию</li>
                <li data-feature="reg" class="hidden">📅 <span id="itemRegText">—</span></li>
                <li data-feature="premium" class="hidden">⭐ Telegram Premium на аккаунте</li>
                <li>⚡ Выдача сразу после оплаты</li>
                <li>🔒 Без банов на момент продажи</li>
            </ul>

            <!-- Статус валидации аккаунта (показывается при нажатии «Купить») -->
            <div class="validate-status hidden" id="validateStatus" role="status" aria-live="polite">
                <span class="validate-status-icon" id="validateStatusIcon"></span>
                <span class="validate-status-text" id="validateStatusText"></span>
            </div>

            <!-- Кнопка подтверждения покупки (появляется только после успешной валидации) -->
            <button class="btn-confirm-buy hidden" id="buyConfirmBtn" type="button">
                Купить (подтвердить)
            </button>

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

    <!-- Модалка отзыва: 1–5 звёзд + опциональный комментарий -->
    <div class="modal hidden" id="reviewModal">
        <div class="modal-backdrop" data-close="reviewModal"></div>
        <div class="modal-sheet">
            <div class="modal-handle"></div>
            <h3 class="modal-title">⭐ Оставить отзыв</h3>
            <p class="support-text" id="reviewModalHint">
                Оцените покупку от 1 до 5 звёзд. Если оставить отзыв не получится — он выставится автоматически через 7 дней (5⭐).
            </p>
            <!-- Звёзды: радиокнопки, скрытые визуально, но семантически верные.
                 Клик по «звезде» (label) меняет выбранную и подсветку.
                 CSS-правила .review-stars и .review-star — ниже в стилях. -->
            <div class="review-stars" id="reviewStars" role="radiogroup" aria-label="Оценка">
                <input type="radio" name="review-rating" id="rv-1" value="1">
                <label for="rv-1" class="review-star" data-value="1" title="1">★</label>
                <input type="radio" name="review-rating" id="rv-2" value="2">
                <label for="rv-2" class="review-star" data-value="2" title="2">★</label>
                <input type="radio" name="review-rating" id="rv-3" value="3">
                <label for="rv-3" class="review-star" data-value="3" title="3">★</label>
                <input type="radio" name="review-rating" id="rv-4" value="4">
                <label for="rv-4" class="review-star" data-value="4" title="4">★</label>
                <input type="radio" name="review-rating" id="rv-5" value="5" checked>
                <label for="rv-5" class="review-star" data-value="5" title="5">★</label>
            </div>
            <div class="review-stars-label" id="reviewStarsLabel">5 из 5 — отлично</div>
            <textarea
                id="reviewComment"
                class="review-textarea"
                placeholder="Комментарий (необязательно)"
                maxlength="1000"
                rows="3"
            ></textarea>
            <div class="review-counter" id="reviewCounter">0 / 1000</div>
            <div class="review-msg" id="reviewMsg"></div>
            <button class="btn-primary" id="reviewSubmitBtn">Отправить отзыв</button>
            <button class="btn-secondary" data-close="reviewModal">Закрыть</button>
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
                // Фильтр по дате создания аккаунта (от и до: месяц/год).
                // 'all' = не ограничено. Допустимо: month=1..12, year=2013..2026.
                createdFromMonth: 'all',
                createdFromYear: 'all',
                createdToMonth: 'all',
                createdToYear: 'all',
                catalog: [],
                categories: [],
                currentPage: 'pageCatalog',
                botUsername: null,
                lastBalanceSync: null,
                // Прямая ссылка на бота для пополнения / перехода.
                // Можно переопределить через /api/bot-info, но фолбэк зашит.
                BOT_URL: 'https://t.me/testvestaccs_bot',
                SUPPORT_URL: 'https://t.me/VestGameSupport',
            };

            const $ = (id) => document.getElementById(id);
            const dom = {
                balancePill: $('balancePill'),
                headerTopupBtn: $('headerTopupBtn'),
                purchaseDetailModal: $('purchaseDetailModal'),
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
                pageSell: $('pageSell'),
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
                buyConfirmBtn: $('buyConfirmBtn'),
                validateStatus: $('validateStatus'),
                validateStatusIcon: $('validateStatusIcon'),
                validateStatusText: $('validateStatusText'),
                itemChatBtn: $('itemChatBtn'),
                openFilters: $('openFilters'),
                filtersModal: $('filtersModal'),
                filtersCountryGrid: $('filtersCountryGrid'),
                filtersOriginGrid: $('filtersOriginGrid'),
                filtersPriceGrid: $('filtersPriceGrid'),
                filterFromMonth: $('filterFromMonth'),
                filterFromYear: $('filterFromYear'),
                filterToMonth: $('filterToMonth'),
                filterToYear: $('filterToYear'),
                filtersApply: $('filtersApply'),
                filtersReset: $('filtersReset'),
                supportModal: $('supportModal'),
                supportBtn: $('supportBtn'),
                // Модалка отзыва
                reviewModal: $('reviewModal'),
                reviewStars: $('reviewStars'),
                reviewStarsLabel: $('reviewStarsLabel'),
                reviewComment: $('reviewComment'),
                reviewCounter: $('reviewCounter'),
                reviewMsg: $('reviewMsg'),
                reviewSubmitBtn: $('reviewSubmitBtn'),
                toast: $('toast'),
                burgerBtn: $('burgerBtn'),
                sideMenu: $('sideMenu'),
                sideMenuBackdrop: $('sideMenuBackdrop'),
                sideMenuChats: $('sideMenuChats'),
                sideMenuChatsBadge: $('sideMenuChatsBadge'),
                sideMenuSupport: $('sideMenuSupport'),
                sideMenuClose: $('sideMenuClose'),
                // Чаты
                pageChats: $('pageChats'),
                chatsList: $('chatsList'),
                chatsLoader: $('chatsLoader'),
                chatsEmpty: $('chatsEmpty'),
                chatsBack: $('chatsBack'),
                chatModal: $('chatModal'),
                chatModalAvatar: $('chatModalAvatar'),
                chatModalName: $('chatModalName'),
                chatModalSub: $('chatModalSub'),
                chatMessages: $('chatMessages'),
                chatComposer: $('chatComposer'),
                chatInput: $('chatInput'),
                chatSend: $('chatSend'),
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
                    // Время покупки тоже показываем по Москве (MSK),
                    // а не в локальной таймзоне браузера.
                    const d = new Date(iso);
                    const fmtDate = new Intl.DateTimeFormat('ru-RU', {
                        day: '2-digit', month: '2-digit', year: '2-digit', timeZone: MSK_TZ,
                    });
                    const fmtTime = new Intl.DateTimeFormat('ru-RU', {
                        hour: '2-digit', minute: '2-digit', timeZone: MSK_TZ,
                    });
                    const datePart = fmtDate.format(d);          // dd.MM.yy
                    const timePart = fmtTime.format(d);          // HH:mm
                    return `${datePart} ${timePart}`;
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
                    const country = p.country || '';
                    const flag = flags[country] || '🌍';
                    const date = formatPurchaseDate(p.created_at);
                    const hasSession = !!p.has_session;
                    const shortId = '#' + String(p.id).slice(-6);
                    const lotTitle = (p.title && String(p.title).trim()) ? String(p.title).trim() : 'Telegram-аккаунт';

                    // Строка страны
                    const countryLineHtml = country
                        ? `<div class="pcard-country-line"><span class="pcard-country-flag">${escapeHtml(flag)}</span><span>${escapeHtml(country)}</span></div>`
                        : '';

                    // Происхождение
                    const ORIGIN_ICONS = { 'Авторег':'🤖', 'Саморег':'👤', 'Фишинг':'🎣', 'Стиллер':'💀' };
                    const originLabel = p.origin || '';
                    const originIcon = ORIGIN_ICONS[originLabel] || '📦';
                    const originHtml = `<span class="pcard-origin">${escapeHtml(originIcon)} ${escapeHtml(originLabel || 'Неизвестно')}</span>`;

                    // Мета-бейджи
                    const metaBadges = [];
                    if (hasSession) metaBadges.push(`<span class="pcard-meta-badge session"><svg width="10" height="10" viewBox="0 0 10 10" fill="none"><circle cx="5" cy="5" r="4.5" fill="#16a34a" opacity=".18"/><path d="M3 5l1.4 1.4L7 3.5" stroke="#15803d" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>Сессия</span>`);
                    if (date) metaBadges.push(`<span class="pcard-meta-badge date">&#x1F4C5; ${escapeHtml(date)}</span>`);
                    if (p.payment_method) metaBadges.push(`<span class="pcard-meta-badge paid">${escapeHtml(p.payment_method)}</span>`);
                    const metaHtml = metaBadges.length ? `<div class="pcard-meta">${metaBadges.join('')}</div>` : '';

                    card.innerHTML = `
                        <div class="pcard-top">
                            <div class="pcard-title">${escapeHtml(lotTitle)}</div>
                            ${countryLineHtml}
                            ${originHtml}
                        </div>
                        ${metaHtml}
                        <div class="pcard-price">${formatRub(p.amount)}<span class="rub">&#x20BD;</span></div>
                        <div class="pcard-footer">
                            <span class="pcard-footer-id">${escapeHtml(shortId)}</span>
                            <span class="pcard-footer-dot">·</span>
                            <span>${escapeHtml(date)}</span>
                        </div>
                    `;

                    // Клик по карточке — открыть боттом-шит с деталями
                    card.addEventListener('click', () => openPurchaseDetail(p));
                    list.appendChild(card);
                }
            }

            /* ===== Боттом-шит деталей покупки ===== */
            const ORIGIN_ICONS_MAP = { 'Авторег':'🤖', 'Саморег':'👤', 'Фишинг':'🎣', 'Стиллер':'💀' };
            const FLAGS_MAP = {
                'США':'🇺🇸','Россия':'🇷🇺','Индия':'🇮🇳','Германия':'🇩🇪',
                'Бразилия':'🇧🇷','Индонезия':'🇮🇩','Казахстан':'🇰🇿',
                'Украина':'🇺🇦','Беларусь':'🇧🇾','Вьетнам':'🇻🇳',
                'Филиппины':'🇵🇭','Мьянма':'🇲🇲','Мексика':'🇲🇽',
                'Турция':'🇹🇷','Польша':'🇵🇱','Великобритания':'🇬🇧',
                'Аргентина':'🇦🇷',
            };

            function openPurchaseDetail(p) {
                const hasSession = !!p.has_session;
                const flag = FLAGS_MAP[p.country || ''] || '🌍';
                const lotTitle = (p.title && String(p.title).trim()) ? String(p.title).trim() : 'Telegram-аккаунт';
                const shortId = '#' + String(p.id).slice(-6);
                const date = formatPurchaseDate(p.created_at);
                const originLabel = p.origin || '';
                const originIcon = ORIGIN_ICONS_MAP[originLabel] || '📦';

                // Заполняем шапку шита
                const flagEl = document.getElementById('pdetailFlag');
                const titleEl = document.getElementById('pdetailTitle');
                const phoneEl = document.getElementById('pdetailPhone');
                const subEl   = document.getElementById('pdetailSub');
                if (flagEl)  flagEl.textContent  = flag;
                if (titleEl) titleEl.textContent = lotTitle;
                if (phoneEl) phoneEl.textContent = p.phone ? '+' + String(p.phone).replace(/^\+/, '') : '—';
                if (subEl)   subEl.textContent   = `${shortId} · ${date}${p.country ? ' · ' + p.country : ''}${originLabel ? ' · ' + originIcon + ' ' + originLabel : ''}`;

                // Управляем кнопками
                const codeBtn    = document.getElementById('pdetailCodeBtn');
                const sessionBtn = document.getElementById('pdetailSessionBtn');
                const jsonBtn    = document.getElementById('pdetailJsonBtn');
                const disputeBtn = document.getElementById('pdetailDisputeBtn');

                if (codeBtn)    codeBtn.disabled    = !hasSession;
                if (sessionBtn) sessionBtn.disabled = !hasSession;
                if (jsonBtn)    jsonBtn.disabled     = !hasSession;

                // Перевязываем слушатели (убираем старые клоном)
                function rebind(el, fn) {
                    if (!el) return;
                    const fresh = el.cloneNode(true);
                    el.parentNode.replaceChild(fresh, el);
                    fresh.addEventListener('click', fn);
                }
                rebind(document.getElementById('pdetailCodeBtn'), () => {
                    closePurchaseDetail();
                    fetchPurchaseCode(p.id, null);
                });
                rebind(document.getElementById('pdetailSessionBtn'), () => {
                    closePurchaseDetail();
                    downloadPurchaseFile(p.id, 'session', null);
                });
                rebind(document.getElementById('pdetailJsonBtn'), () => {
                    closePurchaseDetail();
                    downloadPurchaseFile(p.id, 'json', null);
                });
                rebind(document.getElementById('pdetailDisputeBtn'), () => {
                    closePurchaseDetail();
                    try { switchPage('pageCatalog'); } catch (e) {}
                    if (typeof openSupport === 'function') openSupport();
                    else showToast('Откройте поддержку через меню', 'info');
                });

                openModal('purchaseDetailModal');
            }

            function closePurchaseDetail() {
                closeModal('purchaseDetailModal');
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
                                <div class="code-hint">Код действителен ограниченное время. При необходимости можно запрос��ть повторно.</div>
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
                    // от <a download>, поэтому к��адём подпись в URL.
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
                const prevPage = state.currentPage || null;
                const pages = ['pageCatalog', 'pageProfile', 'pageUserProfile', 'pagePurchases', 'pageTopup', 'pageSell', 'pageChats'];
                pages.forEach((p) => {
                    const el = document.getElementById(p);
                    if (!el) return;
                    el.classList.toggle('active', p === pageId);
                });
                state.currentPage = pageId;
                // nav highlight (боковое меню)
                document.querySelectorAll('.side-menu-item[data-page]').forEach((b) => {
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
  loadProfileListings();
  }
                // при открытии «Мои покупки» — подтянем список
                if (pageId === 'pagePurchases') {
                    loadPurchases();
                }
                // При открытии «Продать» — обновим сводку и список моих объявлений
                if (pageId === 'pageSell') {
                    updateSellSummary();
                    loadMyListings();
                }
                // При открытии «Чаты» — подтянем список диалогов
                if (pageId === 'pageChats') {
                    loadChats();
                }
                // Если уходим со страницы «Продать» — отменим pending phone-flow,
                // чтобы Telethon-клиент на сервере не висел зря. Это best-effort.
                if (prevPage === 'pageSell' && pageId !== 'pageSell') {
                    try {
                        fetch('/api/sell_account/phone/cancel', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ initData: state.initData || '' }),
                        }).catch(() => {});
                    } catch (e) { /* noop */ }
                }
            }

            /* ===== Чаты (P2P диалоги) ===== */
            const chatsState = {
                items: [],
                loading: false,
                activePeerId: null,
                activeThreadId: null,
                activeMessages: [],
                sending: false,
                // Кеш фото собеседников: peerId -> photo_url (или null если нет фото).
                // Используем и при первом рендере (если успели подгрузить), и при открытии модалки.
                peerPhotos: {},
                // Какие peer-ы сейчас подгружаются (чтобы не лупить /api/user/<id>/avatar параллельно).
                peerPhotoInFlight: {},
            };

            function chatDisplayName(peer, fallbackTelegramId) {
                if (!peer) return 'Собеседник';
                if (peer.username) return '@' + peer.username;
                if (peer.first_name) return peer.first_name;
                if (fallbackTelegramId) return 'id ' + fallbackTelegramId;
                return 'Собеседник';
            }

            // Формирует «человеческое» имя собеседника с приоритетом:
            // 1) first_name (например, «Иван»)
            // 2) @username (если имя не известно)
            // 3) id XXX (если ничего нет)
            // Используется во всех места��: список чатов, заголовок модалки,
            // авто-открытие чата после покупки, превью в карточке диалога.
            function peerDisplay(c) {
                if (!c) return 'Собесе��ник';
                if (c.peer_first_name) return c.peer_first_name;
                if (c.peer_username) return '@' + c.peer_username;
                return 'id ' + c.peer_id;
            }

            // Подпись «под именем» — что-то вроде @username или просто
            // telegram id (если username нет). Если есть first_name и
            // username — покажем @username как вторую строку.
            function peerSub(c) {
                if (!c) return '';
                if (c.peer_username) return '@' + c.peer_username;
                return 'telegram id ' + c.peer_id;
            }

            function chatInitial(name) {
                const s = String(name || '').replace(/^[@\s]+/, '');
                return (s.charAt(0) || '?').toUpperCase();
            }

            // Применяет реальное фото peer-а к аватар-контейнеру, если оно есть в кеше.
            // initialClass — имя класса, в котором лежит буква-фолбэк.
            // Если в кеше есть URL — подменяет на <img>, иначе оставляет фолбэк.
            function applyPeerPhotoToEl(el, peerId, initialClass) {
                if (!el) return;
                peerId = String(peerId || '');
                const url = chatsState.peerPhotos[peerId];
                if (!url) return;
                // Удаляем старый img, если был
                const oldImg = el.querySelector('img');
                if (oldImg && oldImg.src === url) return;
                if (oldImg) oldImg.remove();
                const img = document.createElement('img');
                img.src = url;
                img.alt = '';
                img.referrerPolicy = 'no-referrer';
                img.loading = 'lazy';
                img.onerror = () => { try { img.remove(); } catch (e) {} };
                el.appendChild(img);
                const fb = el.querySelector('.' + initialClass);
                if (fb) fb.classList.add('hidden');
            }

            // Ленивая подгрузка фото peer-а через /api/user/<id>/avatar.
            // Кладёт результат в chatsState.peerPhotos �� обновляет ВСЕ видимые
            // .chat-card-avatar[data-peer-id="X"] и .chat-modal-avatar (если peer
            // сейчас открыт). Безопасно дёргать много раз — дедуп по inFlight.
            async function ensurePeerPhoto(peerId) {
                peerId = String(peerId || '');
                if (!peerId) return;
                if (chatsState.peerPhotos[peerId] !== undefined) {
                    // Уже загружено (или явно null — нет фото). Просто применим к DOM.
                    applyPhotoToAllSlots(peerId);
                    return;
                }
                if (chatsState.peerPhotoInFlight[peerId]) return;
                chatsState.peerPhotoInFlight[peerId] = true;
                try {
                    const r = await api('/api/user/' + encodeURIComponent(peerId) + '/avatar');
                    const url = (r && r.ok && r.data && r.data.photo_url) ? r.data.photo_url : null;
                    chatsState.peerPhotos[peerId] = url;  // null — тоже сохраняем
                    applyPhotoToAllSlots(peerId);
                } catch (e) { /* noop */ }
                finally {
                    chatsState.peerPhotoInFlight[peerId] = false;
                }
            }

            // Пройдёмся по всем .chat-card-avatar[data-peer-id] и по модалке,
            // чтобы подставить фото peer-а, которое только что подгрузилось.
            function applyPhotoToAllSlots(peerId) {
                peerId = String(peerId || '');
                if (!peerId) return;
                const slots = document.querySelectorAll('.chat-card-avatar[data-peer-id="' + peerId + '"]');
                for (const s of slots) applyPeerPhotoToEl(s, peerId, 'cc-initial');
                const modalAvatar = document.getElementById('chatModalAvatar');
                if (modalAvatar && String(chatsState.activePeerId) === peerId) {
                    applyPeerPhotoToEl(modalAvatar, peerId, 'cma-initial');
                }
            }

            // ====== МОСКОВСКОЕ ВРЕМЯ (MSK) ======
            // Раньше время чата и покупок рендерилось в локальной таймзоне
            // браузера. Из-за этого у пользователей с TZ != Europe/Moscow
            // (или с системным +3 вместо МСК) в чате светилось «+3» / другое
            // смещение. Теперь все временные метки фиксируются в Europe/Moscow.
            const MSK_TZ = 'Europe/Moscow';
            const MSK_TIME_OPTS = { hour: '2-digit', minute: '2-digit', timeZone: MSK_TZ };
            const MSK_WEEKDAY_OPTS = { weekday: 'short', hour: '2-digit', minute: '2-digit', timeZone: MSK_TZ };
            const MSK_DATE_OPTS = { day: '2-digit', month: '2-digit', timeZone: MSK_TZ };

            function formatChatTime(iso) {
                if (!iso) return '';
                try {
                    const d = new Date(iso);
                    const now = new Date();
                    // Сравниваем дни в МСК, чтобы "сегодня" считалось по Москве.
                    const mskDay = new Intl.DateTimeFormat('ru-RU', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone: MSK_TZ }).format(d);
                    const mskNow = new Intl.DateTimeFormat('ru-RU', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone: MSK_TZ }).format(now);
                    const sameDay = mskDay === mskNow;
                    if (sameDay) {
                        return d.toLocaleTimeString('ru-RU', MSK_TIME_OPTS);
                    }
                    const diff = (now - d) / 1000;
                    if (diff < 60 * 60 * 24 * 7) {
                        return d.toLocaleDateString('ru-RU', MSK_WEEKDAY_OPTS);
                    }
                    return d.toLocaleDateString('ru-RU', MSK_DATE_OPTS);
                } catch (e) {
                    return '';
                }
            }

            function formatChatBubbleTime(iso) {
                if (!iso) return '';
                try {
                    const d = new Date(iso);
                    return d.toLocaleTimeString('ru-RU', MSK_TIME_OPTS);
                } catch (e) { return ''; }
            }

            async function loadChats() {
                if (chatsState.loading) return;
                chatsState.loading = true;
                if (dom.chatsLoader) dom.chatsLoader.classList.remove('hidden');
                if (dom.chatsEmpty) dom.chatsEmpty.classList.add('hidden');
                try {
                    const r = await api('/api/chats');
                    if (r.ok) {
                        chatsState.items = r.data.chats || [];
                        renderChats(true);
                        updateChatsBadge(r.data.unread_total || 0);
                    } else {
                        chatsState.items = [];
                        renderChats(true);
                    }
                } catch (e) {
                    console.error('loadChats error', e);
                    chatsState.items = [];
                    renderChats(true);
                } finally {
                    chatsState.loading = false;
                    if (dom.chatsLoader) dom.chatsLoader.classList.add('hidden');
                }
            }

            // Тихая версия для polling — без лоадера, не мигает.
            // Просто подтягивает список и бейдж.
            // ВАЖНО: передаём animate=false, чтобы карточки не "подпрыгивали"
            // каждые 0.5 секунды — а играли анимацию только при первом
            // открытии страницы чатов (loadChats).
            let _chatsPollInFlight = false;
            async function pollChats() {
                if (_chatsPollInFlight) return;
                _chatsPollInFlight = true;
                try {
                    const r = await api('/api/chats');
                    if (r.ok) {
                        chatsState.items = r.data.chats || [];
                        renderChats(false);
                        updateChatsBadge(r.data.unread_total || 0);
                    }
                } catch (e) { /* noop */ }
                finally {
                    _chatsPollInFlight = false;
                }
            }

            // animate=true  → первый рендер (вход на страницу) — карточки
            //                  "прилетают" с cardIn-анимацией.
            // animate=false → обновление (polling) — карточки молча
            //                  перерисовываются, без анимации.
            function renderChats(animate) {
                const list = dom.chatsList;
                if (!list) return;
                // Отключаем cardIn-анимацию дочерних .chat-card при обновлении,
                // чтобы при polling (каждые 0.5 сек) карточки не "мигали".
                if (animate === false) {
                    list.classList.add('no-anim');
                } else {
                    list.classList.remove('no-anim');
                }
                list.innerHTML = '';
                if (!chatsState.items.length) {
                    if (dom.chatsEmpty) dom.chatsEmpty.classList.remove('hidden');
                    return;
                }
                if (dom.chatsEmpty) dom.chatsEmpty.classList.add('hidden');

                for (const c of chatsState.items) {
                    const card = document.createElement('button');
                    card.type = 'button';
                    card.className = 'chat-card';
                    card.dataset.peerId = String(c.peer_id);
                    // Приоритет: first_name → @username → id
                    const display = peerDisplay(c);
                    const sub = peerSub(c);
                    const initial = chatInitial(display);
                    const time = formatChatTime(c.last_message_at);
                    // sender_id === 0 — это сообщение от Vest Account (бот).
                    // Показываем его в превью с собственным префиксом, а не
                    // как входящее от собеседник��.
                    const _lastSid = c.last_message_sender_id;
                    const _myId = (state.tgUser && state.tgUser.id) || 0;
                    let _prefix = '';
                    if (_lastSid === 0) _prefix = 'Vest Account: ';
                    else if (_lastSid === _myId) _prefix = 'Вы: ';
                    const preview = c.last_message
                        ? _prefix + c.last_message
                        : 'Нет сообщений';
                    const hasUnread = (c.unread || 0) > 0;

                    card.innerHTML = `
                        <div class="chat-card-strip"></div>
                        <div class="chat-card-body">
                            <div class="chat-card-avatar" data-peer-id="${c.peer_id}">
                                <span class="cc-initial">${escapeHtml(initial)}</span>
                            </div>
                            <div class="chat-card-main">
                                <div class="chat-card-top">
                                    <div class="chat-card-name" data-peer-id="${c.peer_id}">${escapeHtml(display)}</div>
                                    <div class="chat-card-time">${escapeHtml(time)}</div>
                                </div>
                                <div class="chat-card-subname" data-peer-id="${c.peer_id}">${escapeHtml(sub)}</div>
                                <div class="chat-card-preview ${hasUnread ? 'has-unread' : ''}">${escapeHtml(preview)}</div>
                                ${hasUnread ? `<div class="chat-card-unread">${c.unread}</div>` : ''}
                            </div>
                        </div>
                    `;
                    card.addEventListener('click', () => openChat(c));
                    // ====== Клик по аватару/имени в спис��е чатов ======
                    // Открывает публичный профиль, а не диалог. Клик по
                    // превью или по пустому месту карточки — открывает чат,
                    // как и раньше.
                    const openPeerProfile = (ev) => {
                        ev.preventDefault();
                        ev.stopPropagation();
                        if (ev.currentTarget) {
                            ev.currentTarget.classList.add('is-clicked');
                            setTimeout(() => {
                                if (ev.currentTarget) ev.currentTarget.classList.remove('is-clicked');
                            }, 180);
                        }
                        // Прелоад-данные peer-а уже есть в `c` — не дёргаем
                        // сеть дважды: покажем то, что знаем, пока грузится /public.
                        const photoUrl = chatsState.peerPhotos[String(c.peer_id)] || null;
                        openUserProfile(Number(c.peer_id), {
                            preload: {
                                name: c.peer_first_name || (c.peer_username ? '@' + c.peer_username : ('id ' + c.peer_id)),
                                username: c.peer_username || '',
                                photo_url: photoUrl,
                            },
                        });
                    };
                    const av = card.querySelector('.chat-card-avatar');
                    const nm = card.querySelector('.chat-card-name');
                    const sn = card.querySelector('.chat-card-subname');
                    if (av) av.addEventListener('click', openPeerProfile);
                    if (nm) nm.addEventListener('click', openPeerProfile);
                    if (sn) sn.addEventListener('click', openPeerProfile);
                    list.appendChild(card);

                    // Если фото этого peer-а уже лежит в кеше (например, мы только
                    // что открыли с ним диалог) — сразу вставим его в аватарку.
                    const slot = card.querySelector('.chat-card-avatar');
                    if (slot && chatsState.peerPhotos[String(c.peer_id)]) {
                        applyPeerPhotoToEl(slot, c.peer_id, 'cc-initial');
                    } else {
                        // Иначе — подтянем лениво (без блокировки UI).
                        ensurePeerPhoto(c.peer_id);
                    }
                }
            }

            function updateChatsBadge(n) {
                const badge = dom.sideMenuChatsBadge;
                if (!badge) return;
                const v = Number(n) || 0;
                if (v > 0) {
                    badge.textContent = v > 99 ? '99+' : String(v);
                    badge.classList.remove('hidden');
                } else {
                    badge.textContent = '0';
                    badge.classList.add('hidden');
                }
            }

            async function refreshUnreadBadge() {
                // Лёгкий запрос только ради счётчика. Не светится в UI при ошибках.
                try {
                    const r = await api('/api/chats/unread_count');
                    if (r && r.ok) updateChatsBadge(r.data.unread || 0);
                } catch (e) { /* noop */ }
            }

            async function openChat(c) {
                chatsState.activePeerId = c.peer_id;
                chatsState.activeMessages = [];
                // Шапка модалки: имя (first_name / @username) и подпись @username / id
                const display = peerDisplay(c);
                const sub = peerSub(c);
                if (dom.chatModalName) dom.chatModalName.textContent = display;
                if (dom.chatModalSub) dom.chatModalSub.textContent = sub;
                // Привязываем chat-id к шапке модалки — чтобы клик по аватару/имени
                // открывал публичный профиль собеседника.
                if (dom.chatModalName) dom.chatModalName.dataset.peerId = String(c.peer_id);
                if (dom.chatModalSub)  dom.chatModalSub.dataset.peerId  = String(c.peer_id);
                if (dom.chatModalAvatar) {
                    dom.chatModalAvatar.dataset.peerId = String(c.peer_id);
                    // data-* для прелоада профиля — тот же набор, что и в chat-card
                    dom.chatModalAvatar.dataset.peerFirstName = c.peer_first_name || '';
                    dom.chatModalAvatar.dataset.peerUsername   = c.peer_username   || '';
                }
                // Аватарка в шапке модалки: сначала буква-фолбэк, при подгрузке
                // реального фото — заменится на <img> через applyPeerPhotoToEl.
                if (dom.chatModalAvatar) {
                    dom.chatModalAvatar.innerHTML = '<span class="cma-initial">' + escapeHtml(chatInitial(display)) + '</span>';
                    if (chatsState.peerPhotos[String(c.peer_id)]) {
                        applyPeerPhotoToEl(dom.chatModalAvatar, c.peer_id, 'cma-initial');
                    } else {
                        ensurePeerPhoto(c.peer_id);
                    }
                }
                if (dom.chatMessages) dom.chatMessages.innerHTML = '<div class="chat-empty">Загрузка…</div>';
                openModal('chatModal');

                // 1) Подтянем сообщения
                try {
                    const r = await api(`/api/chats/${encodeURIComponent(c.peer_id)}/messages`);
                    if (r.ok) {
                        chatsState.activeThreadId = r.data.thread_id;
                        chatsState.activeMessages = r.data.messages || [];
                        renderChatMessages(true);   // открыли диалог — пузырьки “прилетают”
                        scrollChatToBottom();
                    } else {
                        if (dom.chatMessages) dom.chatMessages.innerHTML = '<div class="chat-empty">Не удалось загрузить сообщения</div>';
                    }
                } catch (e) {
                    if (dom.chatMessages) dom.chatMessages.innerHTML = '<div class="chat-empty">Сеть: ' + escapeHtml(e.message || '') + '</div>';
                }
                // 2) Пометим входящие как прочитанные
                try {
                    await api(`/api/chats/${encodeURIComponent(c.peer_id)}/read`, { method: 'POST' });
                    // 3) Сразу обновим список и счётчик.
                    // Используем pollChats (animate=false) — иначе loadChats()
                    // включил бы cardIn-анимацию при каждом открытии диалога.
                    await pollChats();
                } catch (e) { /* noop */ }
            }

            function renderChatMessages(animate) {
                const cont = dom.chatMessages;
                if (!cont) return;
                // При открытии диалога / отправке своего сообщения анимация
                // пузырьков играет, при polling — выключаем, чтобы они не
                // «прыгали» каждые 0.5 секунды.
                if (animate === false) {
                    cont.classList.add('no-anim');
                } else {
                    cont.classList.remove('no-anim');
                }
                cont.innerHTML = '';
                const msgs = chatsState.activeMessages || [];
                if (!msgs.length) {
                    cont.innerHTML = '<div class="chat-empty">Сообщений пока нет. Напишите первым!</div>';
                    return;
                }
                const myId = (state.tgUser && state.tgUser.id) || 0;
                for (const m of msgs) {
                    // sender_id === 0 — это «Vest Account». Рисуем отдельным
                    // шаблоном: аватарка-бота, имя, тело сообщения и опциональные
                    // кнопки действий (например, «Открыть спор»).
                    if (m.sender_id === 0) {
                        cont.appendChild(renderBotMessage(m));
                        continue;
                    }
                    const div = document.createElement('div');
                    const mine = (m.sender_id === myId) || m.mine;
                    div.className = 'chat-bubble ' + (mine ? 'mine' : 'theirs');
                    const safe = escapeHtml(m.text).replace(/\n/g, '<br>');
                    // Для своих сообщений рисуем «ножку» с временем и галочками
                    // (1 серая — не прочитано получателем, 2 синие — прочитано).
                    // У собеседника read_at не заполнен => галочки серие,
                    // как только он открыл диалог (POST /api/chats/<peer>/read)
                    // — наш read_at станет != null и галочки посинеют.
                    let ticksHtml = '';
                    if (mine) {
                        const isRead = !!m.read_at;
                        const cls = isRead ? 'read' : 'pending';
                        // Две SVG-галочки. У «непрочитано» рисуем одну,
                        // у «прочитано» — две (вторая со смещением вправо).
                        if (isRead) {
                            ticksHtml = (
                                '<span class="chat-ticks ' + cls + '" aria-label="Прочитано">' +
                                    '<span class="tick-stack">' +
                                        svgTick() +
                                        svgTick() +
                                    '</span>' +
                                '</span>'
                            );
                        } else {
                            ticksHtml = (
                                '<span class="chat-ticks ' + cls + '" aria-label="Отправлено">' +
                                    svgTick() +
                                '</span>'
                            );
                        }
                    }
                    const footHtml = mine
                        ? ('<span class="chat-bubble-foot">' +
                           '<span class="chat-bubble-time">' + escapeHtml(formatChatBubbleTime(m.created_at)) + '</span>' +
                           ticksHtml +
                           '</span>')
                        : ('<span class="chat-bubble-time">' + escapeHtml(formatChatBubbleTime(m.created_at)) + '</span>');
                    div.innerHTML = safe + footHtml;
                    cont.appendChild(div);
                }
            }

            // Рендер «служебного» сообщения от Vest Account.
            // Текст может содержать маркеры кнопок:
            //   [[BTN:action|label]]            — простая кнопка
            //   [[BTN:action:param|label]]      — кнопка с параметром
            // (например, open_review:42 — оставить отзыв по покупке #42).
            // Маркеры вырезаются из видимого текста и рендерятся как
            // настоящие <button> под пузырьком.
            function renderBotMessage(m) {
                const wrap = document.createElement('div');
                wrap.className = 'chat-bubble bot';
                const rawText = String(m.text || '');
                // Соберём кнопки и заодно вычистим маркеры из видимого текста.
                // Регекс совпадает с серверным BOT_BTN_TOKEN_RE:
                //   action: [a-z_]+, опционально :digits, затем |label
                const actions = [];
                const cleanText = rawText.replace(
                    /\[\[BTN:([a-z_]+)(?::(\d+))?\|([^\]]+)\]\]/g,
                    (_m, action, param, label) => {
                        actions.push({
                            action: String(action),
                            param: param ? String(param) : null,
                            label: String(label),
                        });
                        return '';
                    }
                ).trim();

                const headHtml = (
                    '<div class="chat-bubble-bot-head">' +
                        '<div class="chat-bubble-bot-avatar" data-bot-avatar="1">' +
                            '<span class="cb-fallback">V</span>' +
                        '</div>' +
                        '<div>' +
                            '<div class="chat-bubble-bot-name">Vest Account' +
                                '<span class="chat-bubble-bot-verified">bot</span>' +
                            '</div>' +
                        '</div>' +
                    '</div>'
                );

                const bodyHtml = (
                    '<div class="chat-bubble-bot-body">' +
                        escapeHtml(cleanText).replace(/\n/g, '<br>') +
                    '</div>'
                );

                let actionsHtml = '';
                if (actions.length) {
                    actionsHtml = '<div class="chat-bubble-bot-actions">' +
                        actions.map((a) => {
                            // Кнопка с отзывом делается «secondary» (не красной),
                            // чтобы не пугать — отзыв это не жалоба.
                            const extraClass = (a.action === 'open_review')
                                ? ' secondary'
                                : '';
                            const paramAttr = a.param
                                ? ' data-bot-param="' + escapeHtml(a.param) + '"'
                                : '';
                            return (
                                '<button type="button" ' +
                                'class="chat-bubble-bot-btn' + extraClass + '" ' +
                                'data-bot-action="' + escapeHtml(a.action) + '"' +
                                paramAttr + '>' +
                                escapeHtml(a.label) +
                                '</button>'
                            );
                        }).join('') +
                    '</div>';
                }

                const footHtml = (
                    '<div class="chat-bubble-bot-foot">' +
                        escapeHtml(formatChatBubbleTime(m.created_at)) +
                    '</div>'
                );

                wrap.innerHTML = headHtml + bodyHtml + actionsHtml + footHtml;

                // Подгрузим аватарку бота (кешируется браузером на сутки).
                const avSlot = wrap.querySelector('[data-bot-avatar="1"]');
                if (avSlot) {
                    const img = document.createElement('img');
                    img.src = '/api/bot_avatar';
                    img.alt = 'Vest Account';
                    img.referrerPolicy = 'no-referrer';
                    img.loading = 'lazy';
                    img.onload = () => {
                        const fb = avSlot.querySelector('.cb-fallback');
                        if (fb) fb.classList.add('hidden');
                    };
                    img.onerror = () => { /* fallback «V» уже на месте */ };
                    avSlot.appendChild(img);
                }

                // Навесим обработчики кнопок действий.
                wrap.querySelectorAll('[data-bot-action]').forEach((btn) => {
                    btn.addEventListener('click', () => {
                        const action = btn.getAttribute('data-bot-action');
                        const param = btn.getAttribute('data-bot-param');
                        handleBotAction(action, param);
                    });
                });

                return wrap;
            }

            // Обработчик кнопок действий из бот-сообщений.
            //  - open_dispute — открыть существующее модальное окно поддержки.
            //  - open_review[:purchase_id] — открыть модалку отзыва.
            function handleBotAction(action, param) {
                if (action === 'open_dispute') {
                    // Закрываем диалог, чтобы пользователь увидел именно поддержку,
                    // а не чат с продавцом.
                    try { closeModal('chatModal'); } catch (e) { /* noop */ }
                    if (typeof openSupport === 'function') {
                        openSupport();
                    } else {
                        showToast('Поддержка скоро откроется', 'info');
                    }
                    return;
                }
                if (action === 'open_review') {
                    // param — это purchase_id, который сервер вложил в маркер.
                    // Если параметр не распарсился — откроем модалку без него
                    // и попросим пользователя выбрать покупку.
                    let pid = 0;
                    if (param) {
                        const n = parseInt(param, 10);
                        if (Number.isFinite(n) && n > 0) pid = n;
                    }
                    openReviewModal(pid);
                    return;
                }
                // Неизвестное действие — просто сообщим.
                showToast('Действи�� пока недоступно', 'info');
            }

            // Маленькая inline-SVG-галочка. Не зависит от внешних шрифтов.
            function svgTick() {
                return (
                    '<svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">' +
                        '<path d="M3 8.5 L6.5 12 L13 4.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' +
                    '</svg>'
                );
            }

            function scrollChatToBottom() {
                const cont = dom.chatMessages;
                if (!cont) return;
                // двойной rAF: ждём отрисовку, потом скроллим
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    cont.scrollTop = cont.scrollHeight;
                }));
            }

            function bindChatEvents() {
                if (!dom.chatComposer) return;
                dom.chatComposer.addEventListener('submit', async (ev) => {
                    ev.preventDefault();
                    await sendChatMessage();
                });
                if (dom.chatInput) {
                    // Enter — отправляем, Shift+Enter — новая строка
                    dom.chatInput.addEventListener('keydown', (ev) => {
                        if (ev.key === 'Enter' && !ev.shiftKey) {
                            ev.preventDefault();
                            sendChatMessage();
                        }
                    });
                    // Авто-рост textarea
                    dom.chatInput.addEventListener('input', () => {
                        dom.chatInput.style.height = 'auto';
                        dom.chatInput.style.height = Math.min(dom.chatInput.scrollHeight, 120) + 'px';
                    });
                }
                // Закрытие модалки по data-close (уже работает глобально), но дополнительно
                // обновим бейдж после закрытия.
                document.querySelectorAll('[data-close="chatModal"]').forEach((b) => {
                    b.addEventListener('click', () => {
                        refreshUnreadBadge();
                    });
                });
            }

            async function sendChatMessage() {
                if (chatsState.sending) return;
                const peerId = chatsState.activePeerId;
                if (!peerId) return;
                const input = dom.chatInput;
                const text = (input.value || '').trim();
                if (!text) return;
                chatsState.sending = true;
                if (dom.chatSend) dom.chatSend.disabled = true;
                try {
                    const r = await api(`/api/chats/${encodeURIComponent(peerId)}/messages`, {
                        method: 'POST',
                        body: JSON.stringify({ text }),
                    });
                    if (r.ok) {
                        chatsState.activeMessages.push(r.data.message);
                        chatsState.activeThreadId = r.data.thread_id;
                        input.value = '';
                        input.style.height = 'auto';
                        renderChatMessages(true);   // своё только что улетело — пузырёк “прилетает”
                        scrollChatToBottom();
                        // Тихо обновим список (последнее сообщение могло поменять порядок).
                        // Используем pollChats (animate=false), чтобы список не мигал —
                        // иначе loadChats() включил бы cardIn-анимацию на каждой отправке.
                        pollChats();
                    } else {
                        const err = (r.data && r.data.error) || 'send_failed';
                        showToast('Не удалось отправить: ' + err, 'error');
                    }
                } catch (e) {
                    showToast('Сеть: ' + (e.message || 'ошибка'), 'error');
                } finally {
                    chatsState.sending = false;
                    if (dom.chatSend) dom.chatSend.disabled = false;
                }
            }

            // Запуск периодического опроса. Раз в 0.5 сек опрашиваем
            // список чатов и активный диалог (если он открыт) — как на
            // FunPay/WhatsApp, чтобы сообщения приходили «вживую».
            // Когда ни одна чат-модалка не открыта — поллим только
            // счётчик непрочитанных, чтобы бейдж был живой,
            // но без лишней нагрузки на БД.
            function startChatsPolling() {
                setInterval(() => {
                    // Чат-список — это СТРАНИЦА pageChats, а не модалка.
                    // Чат-диалог — это модалка chatModal, которая открывается
                    // через openModal(id) — снимает класс .hidden.
                    const onChatsPage = state.currentPage === 'pageChats';
                    const chatDialogOpen = dom.chatModal
                        && !dom.chatModal.classList.contains('hidden');
                    if (onChatsPage || chatDialogOpen) {
                        if (onChatsPage) {
                            pollChats();
                        }
                        if (chatDialogOpen && chatsState.activePeerId) {
                            pollActiveMessages();
                        }
                        refreshUnreadBadge();
                    } else {
                        refreshUnreadBadge();
                    }
                }, 500);
            }

            // Тихий опрос активного диалога: подтягивает новые сообщения в
            // реальном времени (раз в 0.5 сек из startChatsPolling).
            // Если пришло новое сообщение от собеседника — оно сразу
            // появляется в DOM без перезахода в чат.
            let _pollInFlight = false;
            async function pollActiveMessages() {
                if (_pollInFlight) return;
                const peerId = chatsState.activePeerId;
                if (!peerId) return;
                _pollInFlight = true;
                try {
                    const r = await api(`/api/chats/${encodeURIComponent(peerId)}/messages`);
                    if (r.ok) {
                        const fresh = r.data.messages || [];
                        const prevIds = new Set(chatsState.activeMessages.map(m => m.id));
                        const newOnes = fresh.filter(m => !prevIds.has(m.id));
                        const myId = (state.tgUser && state.tgUser.id) || 0;
                        const newFromPeer = newOnes.filter(m => m.sender_id !== myId);

                        if (newOnes.length) {
                            chatsState.activeMessages = fresh;
                            chatsState.activeThreadId = r.data.thread_id;
                            renderChatMessages(false);  // polling — без анимации
                            // Новое сообщение от собеседника — всегда скроллим
                            // вниз, чтобы юзер его сразу увидел.
                            // Своё новое — скроллим только если уже внизу.
                            const cont = dom.chatMessages;
                            if (cont) {
                                if (newFromPeer.length > 0) {
                                    scrollChatToBottom();
                                } else {
                                    const nearBottom = (cont.scrollHeight - cont.scrollTop - cont.clientHeight) < 120;
                                    if (nearBottom) scrollChatToBottom();
                                }
                            }
                            // Тут же помечаем входящие как прочитанные
                            // (чтобы у отправителя посинели галочки).
                            try {
                                await api(`/api/chats/${encodeURIComponent(peerId)}/read`, { method: 'POST' });
                                refreshUnreadBadge();
                            } catch (e) { /* noop */ }
                        } else if (fresh.length !== chatsState.activeMessages.length) {
                            // Что-то подчистилось (например, БД-операции) — синхронизируем.
                            chatsState.activeMessages = fresh;
                            renderChatMessages(false);  // polling — без анимации
                        }

                        // Тихо подмешаем обновления read_at (галочки синеют).
                        const byId = new Map(fresh.map(m => [m.id, m]));
                        let changed = false;
                        for (const m of chatsState.activeMessages) {
                            const upd = byId.get(m.id);
                            if (upd && upd.read_at && !m.read_at) {
                                m.read_at = upd.read_at;
                                changed = true;
                            }
                        }
                        if (changed) renderChatMessages(false);  // polling — без анимации
                    }
                } catch (e) {
                    // Сеть/таймаут — просто молча пропускаем цикл.
                } finally {
                    _pollInFlight = false;
                }
            }

            // Открыть диалог по telegram_id (вызывается из openItem при клике на «Написать»).
            // Если диалога ещё нет — он создаётся на сервере при первой отправке.
            async function openChatByPeerId(peerId, peerMeta) {
                peerId = Number(peerId);
                if (!peerId || !Number.isFinite(peerId)) {
                    showToast('Не удалось начать чат', 'error');
                    return;
                }
                const myId = (state.tgUser && state.tgUser.id) || 0;
                if (peerId === myId) {
                    showToast('Нельзя написать самому себе', 'info');
                    return;
                }
                // Убедимся, что thread существует (start возвращает существующий или новый).
                try {
                    await api('/api/chats/start', {
                        method: 'POST',
                        body: JSON.stringify({ peer_id: peerId }),
                    });
                } catch (e) { /* noop — send тоже создаст */ }
                // Берём имя/username собеседника из открытых чатов, если он там есть —
                // иначе fallback на то, что передали в peerMeta.
                const known = (chatsState.items || []).find(x => Number(x.peer_id) === peerId);
                const peerObj = {
                    peer_id: peerId,
                    peer_username: (known && known.peer_username) || (peerMeta && peerMeta.username) || null,
                    peer_first_name: (known && known.peer_first_name) || (peerMeta && peerMeta.first_name) || null,
                };
                openChat({
                    ...peerObj,
                    unread: 0,
                    last_message: null,
                    last_message_at: null,
                    last_message_sender_id: null,
                });
            }

            /* ===== Боковое меню ===== */
            /* ===== Тема (светлая / тёмная / авто) ===== */
            const THEME_KEY = 'va_theme';

            function applyTheme(val) {
                // val: 'light' | 'dark' | 'auto'
                const html = document.documentElement;
                if (val === 'light') {
                    html.setAttribute('data-theme', 'light');
                } else if (val === 'dark') {
                    html.setAttribute('data-theme', 'dark');
                } else {
                    // auto — убираем атрибут, работает @media (prefers-color-scheme)
                    html.removeAttribute('data-theme');
                }
                // Обновляем активную опцию в тогглере
                document.querySelectorAll('.theme-toggle-opt').forEach((el) => {
                    el.classList.toggle('active', el.dataset.themeVal === val);
                });
                // Сохраняем
                try { localStorage.setItem(THEME_KEY, val); } catch (e) {}
            }

            function initTheme() {
                let saved = 'auto';
                try { saved = localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) {}
                applyTheme(saved);
            }

            function bindThemeToggle() {
                const toggle = document.getElementById('themeToggle');
                if (!toggle) return;
                toggle.addEventListener('click', (e) => {
                    const opt = e.target.closest('.theme-toggle-opt');
                    if (!opt) return;
                    applyTheme(opt.dataset.themeVal);
                });
            }

            function openSideMenu() {
                if (!dom.sideMenu) return;
                dom.sideMenu.classList.add('open');
                dom.sideMenu.setAttribute('aria-hidden', 'false');
                if (dom.burgerBtn) {
                    dom.burgerBtn.classList.add('open');
                    dom.burgerBtn.setAttribute('aria-expanded', 'true');
                }
            }
            function closeSideMenu() {
                if (!dom.sideMenu) return;
                dom.sideMenu.classList.remove('open');
                dom.sideMenu.setAttribute('aria-hidden', 'true');
                if (dom.burgerBtn) {
                    dom.burgerBtn.classList.remove('open');
                    dom.burgerBtn.setAttribute('aria-expanded', 'false');
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

                // Ник — приоритет ИМЯ (first_name + last_name), иначе @username.
                // В шапке мини-аппа хотим видеть имя человека, а не его ник,
                // чтобы легче узнавать аккаунт в списке чатов / каталоге.
                if (nameEl) {
                    const fullName = [u.first_name, u.last_name].filter(Boolean).join(' ').trim();
                    if (fullName) {
                        nameEl.textContent = fullName;
                    } else if (u.username) {
                        nameEl.textContent = u.username;
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
                    dom.profileUsername.textContent = u.username ? u.username : 'id ' + u.id;
                }
            }

            /* ===== Страница «Продать аккаунт» =====
               Логика — аналог раздела «Продать» в bot.py (vestaccpunt):
               1) название → 2) описание → 3) цена → 4) происхождение →
               5) загрузка (.session или код) → публикация Listing.
            */
            const sellState = {
                origin: null,             // Авторег | Саморег | Фишинг | Стиллер
                phoneSent: false,         // код уже отправлен на телефон?
                needs2fa: false,          // нужен ли 2FA после кода?
                busy: false,
                step: 1,                  // 1 — параметры, 2 — телефон/код/файл
                mode: 'phone',            // 'phone' | 'file'
                sessionFile: null,        // File-объект загруженного .session
            };

            function sellReadForm() {
                const title = (document.getElementById('sellTitle')?.value || '').trim();
                const description = (document.getElementById('sellDescription')?.value || '').trim();
                const priceStr = (document.getElementById('sellPrice')?.value || '').trim();
                const price = priceStr ? Number(priceStr) : 0;
                // Месяц и год регистрации Telegram-аккаунта — указывает
                // продавец при добавлении. Могут быть пустыми (тогда бэк
                // просто не запишет их в Account). Возвращаем как числа,
                // либо null.
                const regMonthStr = (document.getElementById('sellRegMonth')?.value || '').trim();
                const regYearStr  = (document.getElementById('sellRegYear')?.value  || '').trim();
                const regMonth = regMonthStr ? Number(regMonthStr) : null;
                const regYear  = regYearStr  ? Number(regYearStr)  : null;
                return { title, description, price, regMonth, regYear };
            }

            function sellValidateStep1({ title, description, price, regMonth, regYear }) {
                if (!title) return { ok: false, msg: 'Введите название объявления' };
                if (title.length > 100) return { ok: false, msg: 'Название — максимум 100 символов' };
                if (description.length > 1000) return { ok: false, msg: 'Описание — максимум 1000 символов' };
                if (!price || price < 10) return { ok: false, msg: 'Минимальная цена — 10₽' };
                if (price > 50000) return { ok: false, msg: 'Максимальная цена — 50 000₽' };
                if (!sellState.origin) return { ok: false, msg: 'Выберите происхождение' };
                // Месяц/год — необязательные. Если оба пустые — ок.
                // Если указан только один (месяц без года или год без месяца)
                // — тоже ок, бэк просто запишет только то, что есть.
                // Главное — диапазоны валидны (селекты их сами ограничивают,
                // но защитимся от подмены через devtools).
                if (regMonth !== null && !(regMonth >= 1 && regMonth <= 12)) {
                    return { ok: false, msg: 'Месяц регистрации — от 1 до 12' };
                }
                if (regYear !== null && !(regYear >= 2013 && regYear <= 2026)) {
                    return { ok: false, msg: 'Год регистрации — от 2013 до 2026' };
                }
                return { ok: true };
            }

            function showStep1Error(msg) {
                const el = document.getElementById('sellStep1Error');
                if (!el) return;
                if (msg) {
                    el.textContent = msg;
                    el.style.display = '';
                } else {
                    el.style.display = 'none';
                    el.textContent = '';
                }
            }

            function showStep2Error(msg) {
                const el = document.getElementById('sellStep2Error');
                if (!el) return;
                if (msg) {
                    el.textContent = msg;
                    el.style.display = '';
                } else {
                    el.style.display = 'none';
                    el.textContent = '';
                }
            }

            function updateSellSummary() {
                const { title, description, price, regMonth, regYear } = sellReadForm();
                const sumEl = document.getElementById('sellSummary');
                const summaryCard = document.getElementById('sellSummaryCard');
                if (!sumEl) return;

                const commission = Math.round((price || 0) * 7 / 100);
                const net = Math.round((price || 0) - commission);
                const origin = sellState.origin || '—';

                // Дата регистрации для сводки: «Июль 2021» / «2021» / «Июль» / «не указана».
                const _RU_MONTHS = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                    'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
                let regLine = 'не указана';
                if (regMonth && regYear) regLine = _RU_MONTHS[regMonth] + ' ' + regYear;
                else if (regMonth) regLine = _RU_MONTHS[regMonth];
                else if (regYear)  regLine = regYear + ' г.';

                if (!title && !price) {
                    sumEl.innerHTML = '<i>Заполните шаг 1 — здесь появится сводка.</i>';
                    return;
                }

                sumEl.innerHTML =
                    '<b>' + escapeHtml(title || '—') + '</b><br>' +
                    '💬 ' + escapeHtml(description || '<i>без описания</i>') + '<br>' +
                    '📅 Регистрация: <b>' + escapeHtml(regLine) + '</b><br>' +
                    '💰 Цена: <b>' + (price || 0) + '₽</b> · комиссия 7%: ' + commission + '₽ · вам поступит: <b>' + net + '₽</b><br>' +
                    '🏷 Происхождение: <b>' + escapeHtml(origin) + '</b>';

                // Сводку показываем только когда мы на шаге 2 (там она нужна для подтверждения)
                if (summaryCard) summaryCard.style.display = (sellState.step === 2) ? '' : 'none';
            }

            function goToStep2() {
                showStep1Error('');
                const data = sellReadForm();
                const v = sellValidateStep1(data);
                if (!v.ok) {
                    showStep1Error(v.msg);
                    // Подсветить проблемные поля
                    if (!data.title) document.getElementById('sellTitle')?.classList.add('invalid');
                    if (data.description.length > 1000) document.getElementById('sellDescription')?.classList.add('invalid');
                    if (!data.price || data.price < 10 || data.price > 50000) document.getElementById('sellPrice')?.classList.add('invalid');
                    return;
                }

                sellState.step = 2;
                document.getElementById('sellStep1').style.display = 'none';
                document.getElementById('sellStep2').style.display = '';
                updateSellSummary();
                // Убедимся, что активный режим отображается корректно
                switchSellMode(sellState.mode, false);
                // Прокрутить к началу формы
                window.scrollTo({ top: 0, behavior: 'smooth' });
                setTimeout(() => {
                    if (sellState.mode === 'phone') {
                        const ph = document.getElementById('sellPhone');
                        if (ph) ph.focus();
                    }
                }, 250);
            }

            function goBackToStep1() {
                sellState.step = 1;
                document.getElementById('sellStep1').style.display = '';
                document.getElementById('sellStep2').style.display = 'none';
                showStep2Error('');
                updateSellSummary();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }

            async function sellPhoneSendCode() {
                if (sellState.busy) return;
                const phone = (document.getElementById('sellPhone')?.value || '').trim();
                if (!phone || !phone.startsWith('+') || phone.length < 8) {
                    showStep2Error('Введите номер в фо��мате +79001234567');
                    return;
                }
                showStep2Error('');

                // Подтянем черновик из шага 1, чтобы бэкенд мог сразу сохранить
                const { title, description, price, regMonth, regYear } = sellReadForm();

                sellState.busy = true;
                const btn = document.getElementById('sellPhoneSendBtn');
                if (btn) btn.disabled = true;
                const oldHtml = btn ? btn.innerHTML : '';
                if (btn) btn.innerHTML = '<span class="sell-loader"></span><span>Отправляем…</span>';
                try {
                    const r = await api('/api/sell_account/phone/start', {
                        method: 'POST',
                        body: JSON.stringify({
                            phone,
                            draft: {
                                title,
                                description,
                                price,
                                origin: sellState.origin,
                                // Месяц/год регистрации — бэкенд их проверит
                                // (диапазоны) �� запишет в Account.reg_month / .reg_year.
                                reg_month: regMonth,
                                reg_year: regYear,
                            },
                        }),
                    });
                    if (!r.ok) {
                        showStep2Error('Не удалось отправить код: ' + (r.data?.detail || r.data?.error || r.error || ''));
                        return;
                    }
                    sellState.phoneSent = true;
                    sellState.needs2fa = false;
                    const codeWrap = document.getElementById('sellCodeWrap');
                    const codeInput = document.getElementById('sellCode');
                    if (codeWrap) codeWrap.style.display = '';
                    if (codeInput) {
                        codeInput.value = '';
                        setTimeout(() => codeInput.focus(), 150);
                    }
                    showToast('✅ Код отправлен на ' + phone + ' — введите его и нажмите «Подтвердить»', 'success');
                } catch (e) {
                    showStep2Error('Сеть: ' + e.message);
                } finally {
                    sellState.busy = false;
                    if (btn) {
                        btn.disabled = false;
                        btn.innerHTML = oldHtml;
                    }
                }
            }

            async function submitSellCode() {
                if (sellState.busy) return;
                const code = (document.getElementById('sellCode')?.value || '').trim();
                // Telegram-код подтверждения — строго 5 цифр.
                // Без этой проверки фронт пропустит любой мусор и пользователь
                // увидит ошибку, хотя на самом деле просто ввёл не то.
                if (!/^\d{5}$/.test(code)) {
                    showStep2Error('Код должен состоять ровно из 5 цифр');
                    return;
                }
                showStep2Error('');

                sellState.busy = true;
                const pubBtn = document.getElementById('sellPublishBtn');
                const btnText = document.getElementById('sellPublishBtnText');
                if (pubBtn) pubBtn.disabled = true;
                const oldText = btnText ? btnText.textContent : '';
                if (btnText) btnText.innerHTML = '<span class="sell-loader"></span>Проверяем…';
                try {
                    const r = await api('/api/sell_account/phone/verify', {
                        method: 'POST',
                        body: JSON.stringify({ code }),
                    });
                    if (!r.ok) {
                        if (r.data?.error === 'need_2fa') {
                            sellState.needs2fa = true;
                            const twofaWrap = document.getElementById('sell2faWrap');
                            if (twofaWrap) twofaWrap.style.display = '';
                            showStep2Error('Аккаунт защищён 2FA. Введите облачный пароль ниже и нажмите «Подтвердить 2FA».');
                            return;
                        }
                        showStep2Error('Ошибка: ' + (r.data?.detail || r.data?.error || r.error || ''));
                        return;
                    }
                    onListingPublished(r.data);
                } catch (e) {
                    showStep2Error('Сеть: ' + e.message);
                } finally {
                    sellState.busy = false;
                    if (pubBtn) pubBtn.disabled = false;
                    if (btnText) btnText.textContent = oldText;
                }
            }

            async function submitSell2FA() {
                if (sellState.busy) return;
                const password = (document.getElementById('sell2fa')?.value || '').trim();
                if (!password) {
                    showStep2Error('Введите пароль 2FA');
                    return;
                }
                showStep2Error('');

                sellState.busy = true;
                const btn = document.getElementById('sellPublish2faBtn');
                const btnText = document.getElementById('sellPublish2faBtnText');
                if (btn) btn.disabled = true;
                const oldText = btnText ? btnText.textContent : '';
                if (btnText) btnText.innerHTML = '<span class="sell-loader"></span>Проверяем…';
                try {
                    const r = await api('/api/sell_account/phone/2fa', {
                        method: 'POST',
                        body: JSON.stringify({ password }),
                    });
                    if (!r.ok) {
                        showStep2Error('Ошибка: ' + (r.data?.detail || r.data?.error || r.error || ''));
                        return;
                    }
                    onListingPublished(r.data);
                } catch (e) {
                    showStep2Error('Сеть: ' + e.message);
                } finally {
                    sellState.busy = false;
                    if (btn) btn.disabled = false;
                    if (btnText) btnText.textContent = oldText;
                }
            }

            /* ===== Переключение режима шага 2: номер+код / .session файл ===== */
            function switchSellMode(mode, resetFileState) {
                if (resetFileState === undefined) resetFileState = true;
                sellState.mode = mode;

                const tabPhone = document.getElementById('sellModeTabPhone');
                const tabFile  = document.getElementById('sellModeTabFile');
                const secPhone = document.getElementById('sellPhoneSection');
                const secFile  = document.getElementById('sellFileSection');

                if (tabPhone) {
                    tabPhone.classList.toggle('active', mode === 'phone');
                    tabPhone.setAttribute('aria-selected', mode === 'phone' ? 'true' : 'false');
                }
                if (tabFile) {
                    tabFile.classList.toggle('active', mode === 'file');
                    tabFile.setAttribute('aria-selected', mode === 'file' ? 'true' : 'false');
                }
                if (secPhone) secPhone.style.display = mode === 'phone' ? '' : 'none';
                if (secFile)  secFile.style.display  = mode === 'file'  ? '' : 'none';

                if (resetFileState) {
                    // Сброс состояния файло��ой зоны при переключении
                    sellState.sessionFile = null;
                    const fileInput = document.getElementById('sellFileInput');
                    if (fileInput) fileInput.value = '';
                    const fileZone = document.getElementById('sellFileZone');
                    if (fileZone) {
                        fileZone.classList.remove('drag');
                        const nameEl = document.getElementById('sellFileName');
                        if (nameEl) { nameEl.textContent = ''; nameEl.className = 'file-name'; }
                    }
                    const pubBtn = document.getElementById('sellFilePublishBtn');
                    if (pubBtn) pubBtn.disabled = true;
                }

                showStep2Error('');
            }

            /* ===== Обработка выбора/drag .session файла ===== */
            function onSessionFileSelected(file) {
                const nameEl = document.getElementById('sellFileName');
                const pubBtn = document.getElementById('sellFilePublishBtn');

                if (!file) {
                    sellState.sessionFile = null;
                    if (nameEl) { nameEl.textContent = ''; nameEl.className = 'file-name'; }
                    if (pubBtn) pubBtn.disabled = true;
                    return;
                }

                if (!file.name.endsWith('.session')) {
                    sellState.sessionFile = null;
                    if (nameEl) { nameEl.textContent = 'Неверное расширение — нужен .session файл'; nameEl.className = 'file-name bad'; }
                    if (pubBtn) pubBtn.disabled = true;
                    showStep2Error('Файл должен иметь расширение .session');
                    return;
                }

                sellState.sessionFile = file;
                if (nameEl) { nameEl.textContent = file.name; nameEl.className = 'file-name ok'; }
                if (pubBtn) pubBtn.disabled = false;
                showStep2Error('');
            }

            /* ===== Отправка .session файла на сервер ===== */
            async function submitSellFile() {
                if (sellState.busy) return;
                if (!sellState.sessionFile) {
                    showStep2Error('Выберите .session файл');
                    return;
                }

                const { title, description, price, regMonth, regYear } = sellReadForm();
                showStep2Error('');

                sellState.busy = true;
                const btn = document.getElementById('sellFilePublishBtn');
                const btnText = document.getElementById('sellFilePublishBtnText');
                if (btn) btn.disabled = true;
                const oldText = btnText ? btnText.textContent : '';
                if (btnText) btnText.innerHTML = '<span class="sell-loader"></span>Проверяем…';

                try {
                    const fd = new FormData();
                    fd.append('session_file', sellState.sessionFile);
                    fd.append('title', title);
                    fd.append('description', description);
                    fd.append('price', String(price));
                    fd.append('origin', sellState.origin || '');
                    if (regMonth !== null) fd.append('reg_month', String(regMonth));
                    if (regYear  !== null) fd.append('reg_year',  String(regYear));

                    const headers = {};
                    if (state.initData) headers['X-Init-Data'] = state.initData;

                    const resp = await fetch('/api/sell_account/session_file', {
                        method: 'POST',
                        headers,
                        body: fd,
                    });
                    const data = await resp.json().catch(() => ({ ok: false, error: 'bad_json' }));
                    if (!resp.ok || !data.ok) {
                        showStep2Error('Ошибка: ' + (data.detail || data.error || 'Неизвестная ошибка'));
                        return;
                    }
                    onListingPublished(data);
                } catch (e) {
                    showStep2Error('Сеть: ' + e.message);
                } finally {
                    sellState.busy = false;
                    if (btn) btn.disabled = !sellState.sessionFile;
                    if (btnText) btnText.textContent = oldText;
                }
            }

            function onListingPublished(data) {
                showToast(
                    '✅ Объявление #' + data.listing_id + ' опубликовано! ' +
                    'Страна: ' + (data.country || '—') + '. ' +
                    'Вам поступит: ' + data.net + '₽',
                    'success'
                );
                resetSellForm();
                // Показываем прогресс-строку и запускаем проверку спама
                loadMyListings().then(() => {
                    if (data.account_id) {
                        _startSpamCheck(data.account_id, data.listing_id);
                    }
                });
                // Переключимся на страницу продажи, чтобы продавец увидел статус
                setTimeout(() => switchPage('pageSell'), 400);
            }

            // Запускает проверку спама в фоне и обновляет карточку объявления
            async function _startSpamCheck(accountId, listingId) {
                // Показываем «проверяется» в карточке
                _setSpamBadgeInCard(listingId, 'checking', '⏳ Проверяем спам...');

                try {
                    const headers = { 'Content-Type': 'application/json' };
                    if (state.initData) headers['X-Init-Data'] = state.initData;

                    const resp = await fetch('/api/spam_check', {
                        method: 'POST',
                        headers,
                        body: JSON.stringify({ account_id: accountId }),
                    });
                    const result = await resp.json().catch(() => null);

                    if (result && result.ok) {
                        if (result.spam_status === 'clean') {
                            _setSpamBadgeInCard(listingId, 'clean', '✅ Без спамблока');
                        } else if (result.spam_status === 'spam') {
                            _setSpamBadgeInCard(listingId, 'spam', '🔴 Спамблок!');
                        }
                    } else {
                        // Не удалось проверить — убираем бейдж
                        _setSpamBadgeInCard(listingId, '', '');
                    }
                } catch (e) {
                    _setSpamBadgeInCard(listingId, '', '');
                }
                // Обновляем весь список после получения результата
                loadMyListings();
            }

            // Обновляет spam-бейдж в конкретной карточке объявления
            function _setSpamBadgeInCard(listingId, status, label) {
                const card = document.querySelector('[data-listing-id="' + listingId + '"]');
                if (!card) return;
                let badge = card.querySelector('.spam-badge');
                if (!status) {
                    if (badge) badge.remove();
                    return;
                }
                if (!badge) {
                    badge = document.createElement('span');
                    badge.className = 'spam-badge ' + status;
                    const miSub = card.querySelector('.mi-sub');
                    if (miSub) miSub.appendChild(badge);
                } else {
                    badge.className = 'spam-badge ' + status;
                }
                badge.innerHTML = '<span class="spam-badge-dot"></span>' + label;
            }

            async function cancelSellFlow() {
                // Отменим pending phone-flow, если он в процессе
                try {
                    await fetch('/api/sell_account/phone/cancel', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ initData: state.initData || '' }),
                    });
                } catch (e) { /* noop */ }
                resetSellForm();
                showToast('Форма очищена', 'success');
            }

            function resetSellForm() {
                sellState.origin = null;
                sellState.phoneSent = false;
                sellState.needs2fa = false;
                sellState.busy = false;
                sellState.step = 1;
                sellState.mode = 'phone';
                sellState.sessionFile = null;

                document.getElementById('sellTitle').value = '';
                document.getElementById('sellDescription').value = '';
                document.getElementById('sellPrice').value = '';
                document.getElementById('sellPhone').value = '';
                document.getElementById('sellCode').value = '';
                document.getElementById('sell2fa').value = '';

                // Сброс файлового input
                const fileInput = document.getElementById('sellFileInput');
                if (fileInput) fileInput.value = '';
                const fileZone = document.getElementById('sellFileZone');
                if (fileZone) fileZone.classList.remove('drag');
                const fileNameEl = document.getElementById('sellFileName');
                if (fileNameEl) { fileNameEl.textContent = ''; fileNameEl.className = 'file-name'; }
                const filePublishBtn = document.getElementById('sellFilePublishBtn');
                if (filePublishBtn) filePublishBtn.disabled = true;

                // Уберём подсветку invalid
                ['sellTitle', 'sellDescription', 'sellPrice'].forEach((id) => {
                    document.getElementById(id)?.classList.remove('invalid');
                });

                const codeWrap = document.getElementById('sellCodeWrap');
                const twofaWrap = document.getElementById('sell2faWrap');
                const summaryCard = document.getElementById('sellSummaryCard');
                if (codeWrap) codeWrap.style.display = 'none';
                if (twofaWrap) twofaWrap.style.display = 'none';
                if (summaryCard) summaryCard.style.display = 'none';

                document.querySelectorAll('#sellOriginGrid .sell-origin-btn').forEach((b) => {
                    b.classList.toggle('active', false);
                });

                // Сброс вкладок режима
                const tabPhone = document.getElementById('sellModeTabPhone');
                const tabFile  = document.getElementById('sellModeTabFile');
                const secPhone = document.getElementById('sellPhoneSection');
                const secFile  = document.getElementById('sellFileSection');
                if (tabPhone) { tabPhone.classList.add('active'); tabPhone.setAttribute('aria-selected', 'true'); }
                if (tabFile)  { tabFile.classList.remove('active'); tabFile.setAttribute('aria-selected', 'false'); }
                if (secPhone) secPhone.style.display = '';
                if (secFile)  secFile.style.display  = 'none';

                // Возвращаемся к шагу 1
                document.getElementById('sellStep1').style.display = '';
                document.getElementById('sellStep2').style.display = 'none';
                showStep1Error('');
                showStep2Error('');
            }

            async function loadMyListings() {
                const wrap = document.getElementById('sellMyListings');
                if (!wrap) return;
                try {
                    const r = await api('/api/my_listings');
                    if (!r.ok) {
                        wrap.innerHTML = '<div class="empty-state" style="padding: 12px;">Не удалось загрузить</div>';
                        return;
                    }
                    const items = r.data?.items || [];
                    if (!items.length) {
                        wrap.innerHTML = '<div class="empty-state" style="padding: 12px;">' +
                            '<div class="empty-emoji">📭</div>' +
                            '<p>У вас пока нет объявлений</p></div>';
                        return;
                    }
                    const statusLabels = {
                        active: '🟢 Активно',
                        sold: '🔴 Продано',
                        cancelled: '⚪️ Снято',
                    };
                    // Вспомогательная функция для spam-бейджа
                    function _spamBadgeHtml(spamStatus) {
                        if (!spamStatus || spamStatus === 'checking') {
                            const lbl = spamStatus === 'checking' ? '⏳ Проверяем...' : '';
                            const cls = spamStatus === 'checking' ? 'checking' : '';
                            return cls ? '<span class="spam-badge ' + cls + '"><span class="spam-badge-dot"></span>' + lbl + '</span>' : '';
                        }
                        if (spamStatus === 'clean') return '<span class="spam-badge clean"><span class="spam-badge-dot"></span>✅ Без спамблока</span>';
                        if (spamStatus === 'spam')  return '<span class="spam-badge spam"><span class="spam-badge-dot"></span>🔴 Спамблок!</span>';
                        return '';
                    }
                    wrap.innerHTML = items.map((it) => {
                        const status = (it.status || 'active');
                        const date = it.created_at ? it.created_at.slice(0, 10) : '';
                        return (
                            '<div class="sell-my-item" data-listing-id="' + it.id + '" data-account-id="' + (it.account_id || '') + '">' +
                                '<div class="mi-row">' +
                                    '<div class="mi-title">' + escapeHtml(it.title) + '</div>' +
                                    '<div class="mi-price">' + Math.round(it.price) + '₽</div>' +
                                '</div>' +
                                '<div class="mi-sub">' +
                                    '<span class="sell-status-pill ' + status + '">' + (statusLabels[status] || status) + '</span>' +
                                    '<span>' + escapeHtml(it.country || '—') + '</span>' +
                                    (it.origin ? '<span>· ' + escapeHtml(it.origin) + '</span>' : '') +
                                    (date ? '<span>· ' + date + '</span>' : '') +
                                    _spamBadgeHtml(it.spam_status) +
                                '</div>' +
                            '</div>'
                        );
                    }).join('');
                } catch (e) {
                    wrap.innerHTML = '<div class="empty-state" style="padding: 12px;">Ошибка сети</div>';
                }
            }

            function bindSellEvents() {
                // Поля формы → обновляем сводку + убираем подсветку invalid
                ['sellTitle', 'sellDescription', 'sellPrice'].forEach((id) => {
                    const el = document.getElementById(id);
                    if (!el) return;
                    el.addEventListener('input', () => {
                        el.classList.remove('invalid');
                        updateSellSummary();
                    });
                });

                // Селекты месяца/года регистрации — обновляют сводку
                // и подсвечиваются, когда что-то выбрано.
                ['sellRegMonth', 'sellRegYear'].forEach((id) => {
                    const el = document.getElementById(id);
                    if (!el) return;
                    el.addEventListener('change', () => {
                        el.classList.toggle('has-value', !!el.value);
                        updateSellSummary();
                    });
                });

                // Происхождение (radio-style)
                document.querySelectorAll('#sellOriginGrid .sell-origin-btn').forEach((btn) => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('#sellOriginGrid .sell-origin-btn').forEach((b) => {
                            b.classList.toggle('active', false);
                        });
                        btn.classList.add('active');
                        sellState.origin = btn.dataset.origin;
                        updateSellSummary();
                    });
                });

                // Шаг 1 → Шаг 2
                const nextBtn = document.getElementById('sellNextBtn');
                if (nextBtn) nextBtn.addEventListener('click', goToStep2);

                // Возврат со шага 2 на шаг 1
                const backBtn = document.getElementById('sellBackBtn');
                if (backBtn) backBtn.addEventListener('click', goBackToStep1);

                // Шаг 2: отправить код на телефон
                const sendBtn = document.getElementById('sellPhoneSendBtn');
                if (sendBtn) sendBtn.addEventListener('click', sellPhoneSendCode);

                // Шаг 2: подтвердить код (ручная отправка, НЕ авто-submit)
                const pubBtn = document.getElementById('sellPublishBtn');
                if (pubBtn) pubBtn.addEventListener('click', submitSellCode);

                // Шаг 2: подтвердить 2FA
                const pub2faBtn = document.getElementById('sellPublish2faBtn');
                if (pub2faBtn) pub2faBtn.addEventListener('click', submitSell2FA);

                // Enter в поле кода — НЕ авто-submit, но удобно: пусть курсор прыгает
                // в кнопку «Подтвердить» для тех, кто привык к Enter. Никакого авто-отправления.
                const codeInput = document.getElementById('sellCode');
                if (codeInput) {
                    // Режем любые не-цифры прямо на вводе, чтобы пользователь
                    // физически не мог набрать 6-й символ или букву.
                    codeInput.addEventListener('input', (e) => {
                        const cleaned = (codeInput.value || '').replace(/\D/g, '').slice(0, 5);
                        if (cleaned !== codeInput.value) codeInput.value = cleaned;
                    });
                    codeInput.addEventListener('keydown', (e) => {
                        if (e.key === 'Enter') {
                            e.preventDefault();
                            // Строгая проверка: ровно 5 цифр
                            if (/^\d{5}$/.test((codeInput.value || '').trim())) {
                                submitSellCode();
                            } else {
                                showStep2Error('Код должен состоять ровно из 5 цифр');
                            }
                        }
                    });
                }

                // Cancel / reset
                const cancelBtn = document.getElementById('sellCancelBtn');
                if (cancelBtn) cancelBtn.addEventListener('click', cancelSellFlow);

                // ---- Переключатель режима (вкладки) ----
                document.querySelectorAll('.sell-mode-tab').forEach((tab) => {
                    tab.addEventListener('click', () => switchSellMode(tab.dataset.mode));
                });

                // ---- Файловая зона: клик → открыть file picker ----
                const fileZone = document.getElementById('sellFileZone');
                const fileInput = document.getElementById('sellFileInput');
                if (fileZone && fileInput) {
                    fileZone.addEventListener('click', () => fileInput.click());
                    fileZone.addEventListener('keydown', (e) => {
                        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
                    });

                    // Drag & drop
                    fileZone.addEventListener('dragover', (e) => {
                        e.preventDefault();
                        fileZone.classList.add('drag');
                    });
                    fileZone.addEventListener('dragleave', () => fileZone.classList.remove('drag'));
                    fileZone.addEventListener('drop', (e) => {
                        e.preventDefault();
                        fileZone.classList.remove('drag');
                        const file = e.dataTransfer?.files?.[0] || null;
                        onSessionFileSelected(file);
                    });

                    // Выбор через диалог
                    fileInput.addEventListener('change', () => {
                        onSessionFileSelected(fileInput.files?.[0] || null);
                    });
                }

                // ---- Кнопка «Проверить и опубликовать» (.session режим) ----
                const filePublishBtn = document.getElementById('sellFilePublishBtn');
                if (filePublishBtn) filePublishBtn.addEventListener('click', submitSellFile);
            }

            /* ===== API ===== */
            async function api(path, options = {}) {
                const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
                if (state.initData) headers['X-Init-Data'] = state.initData;
                const controller = new AbortController();
                const timeoutMs = Number(options.timeoutMs) || 10000;
                const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
                const externalSignal = options.signal;
                if (externalSignal) externalSignal.addEventListener('abort', () => controller.abort(), { once: true });
                try {
                    const fetchOptions = { ...options, headers, signal: controller.signal };
                    delete fetchOptions.timeoutMs;
                    const resp = await fetch(path, fetchOptions);
                    const data = await resp.json().catch(() => ({ ok: false, error: 'bad_json' }));
                    return { ok: resp.ok && data.ok, status: resp.status, data };
                } catch (err) {
                    return { ok: false, status: 0, error: err.name === 'AbortError' ? 'timeout' : (err.message || 'network_error') };
                } finally {
                    window.clearTimeout(timeoutId);
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
                'Бразилия': '🇧🇷', 'Индонезия': '🇮���', 'Казахстан': '🇰🇿',
                'Украина': '🇺🇦', 'Беларусь': '🇧🇾', 'Вьетнам': '🇻🇳',
                'Филиппины': '🇵🇭', 'Мьянма': '🇲🇲', 'Мексика': '🇲🇽',
                'Турция': '🇹🇷', 'Польша': '🇵🇱', 'Великобритания': '🇬🇧',
                'Аргентина': '🇦🇷',
                // новые страны (синхронизировано с bot.py / COUNTRY_FLAGS)
                'Бангладеш': '🇧🇩', 'Пакистан': '🇵🇰', 'Египет': '🇪🇬',
                'Нигерия': '🇳🇬', 'Кения': '🇰🇪', 'Иран': '🇮🇷',
                'Саудовская Аравия': '🇸🇦', 'ОАЭ': '🇦🇪',
                'Таиланд': '🇹🇭', 'Малайзия': '🇲🇾', 'Сингапур': '🇸🇬',
                'Южная Корея': '🇰🇷', 'Япония': '🇯🇵', 'Китай': '🇨🇳',
                'Австралия': '🇦🇺', 'Канада': '🇨🇦',
                'Франция': '🇫🇷', 'Италия': '🇮🇹', 'Испания': '🇪🇸',
            };
            const COUNTRY_LIST = [
                'США','Россия','Индия','Германия','Бразилия','Индонезия',
                'Казахстан','Украина','Беларусь','Вьетнам','Филиппины','Мьянма',
                'Мексика','Турци��','Польша','Великобритания','Аргентина',
                'Бангладеш','Пакистан','Египет','Нигерия','Кения','Иран',
                'Саудовская Аравия','ОАЭ','Таиланд','Малайзия','Сингапур',
                'Южная Корея','Япония','Китай','Австралия','Канада',
                'Франция','Италия','Испания',
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
            // Месяцы и годы для фильтра по дате создания аккаунта.
            // Годы — от 2013 (появление Telegram) до 2026 (текущий).
            const MONTH_LABELS = [
                'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
            ];
            const YEAR_MIN = 2013;
            const YEAR_MAX = 2026;
            const YEAR_OPTIONS = [];
            for (let y = YEAR_MAX; y >= YEAR_MIN; y--) YEAR_OPTIONS.push(y);

            // Заполняет <select> опц��ями месяцев/годов (если ещё не заполнен).
            function populateDateSelects() {
                // Месяцы: первые 13 опций (1..12 + 'all')
                const monthSels = [dom.filterFromMonth, dom.filterToMonth];
                monthSels.forEach((sel) => {
                    if (!sel) return;
                    if (sel.options.length > 1) return; // уже заполнен
                    MONTH_LABELS.forEach((label, i) => {
                        const opt = document.createElement('option');
                        opt.value = String(i + 1);
                        opt.textContent = label;
                        sel.appendChild(opt);
                    });
                });
                // Годы
                const yearSels = [dom.filterFromYear, dom.filterToYear];
                yearSels.forEach((sel) => {
                    if (!sel) return;
                    if (sel.options.length > 1) return;
                    YEAR_OPTIONS.forEach((y) => {
                        const opt = document.createElement('option');
                        opt.value = String(y);
                        opt.textContent = String(y);
                        sel.appendChild(opt);
                    });
                });
            }
            // Синхронизирует value <select>'ов с state.createdFrom* / createdTo*
            // и подсвечивает активные (си��ий фон) если выбран конкретный м/г.
            function syncDateSelects() {
                const pairs = [
                    { sel: dom.filterFromMonth, value: state.createdFromMonth },
                    { sel: dom.filterFromYear,  value: state.createdFromYear },
                    { sel: dom.filterToMonth,   value: state.createdToMonth },
                    { sel: dom.filterToYear,    value: state.createdToYear },
                ];
                pairs.forEach(({ sel, value }) => {
                    if (!sel) return;
                    const v = value == null ? 'all' : String(value);
                    sel.value = v;
                    if (v !== 'all') sel.classList.add('has-value');
                    else sel.classList.remove('has-value');
                });
            }

            async function loadCategories() {
                const r = await api('/api/categories');
                if (!r.ok) return;
                state.categories = r.data.categories || [];
                renderFilterModal();
            }
            function renderLoadFailure(message) {
                showEmpty(false);
                dom.catalog.innerHTML = `<div class="load-failure" role="alert"><strong>Не удалось загрузить каталог</strong><p>${escapeHtml(message || 'Проверьте соединение и попробуйте ещё раз.')}</p><button type="button" id="retryCatalog">Повторить</button></div>`;
                const retry = document.getElementById('retryCatalog');
                if (retry) retry.addEventListener('click', loadCatalog, { once: true });
            }

            async function loadCatalog() {
                showLoader(true);
                const params = new URLSearchParams();
                if (state.country && state.country !== 'all') params.set('country', state.country);
                if (state.origin && state.origin !== 'all') params.set('origin', state.origin);
                if (state.priceSort && state.priceSort !== 'default') params.set('sort', state.priceSort);
                // Фильтр по дате создания аккаунта (от и до)
                if (state.createdFromMonth && state.createdFromMonth !== 'all') params.set('from_month', state.createdFromMonth);
                if (state.createdFromYear  && state.createdFromYear  !== 'all') params.set('from_year',  state.createdFromYear);
                if (state.createdToMonth   && state.createdToMonth   !== 'all') params.set('to_month',   state.createdToMonth);
                if (state.createdToYear    && state.createdToYear    !== 'all') params.set('to_year',    state.createdToYear);
                params.set('limit', '100');

                try {
                    const r = await api('/api/catalog?' + params.toString());
                    if (!r.ok) {
                        renderLoadFailure(r.error === 'timeout' ? 'Сервер отвечает слишком долго.' : 'Проверьте соединение и попробуйте ещё раз.');
                        return;
                    }
                    state.catalog = r.data.items || [];
                    state.catalogSig = state.catalog.map((it) => `${it.id}:${it.price}:${it.country}`).join('|');
                    renderCatalog();
                    updateFilterSummary();
                } catch (error) {
                    renderLoadFailure('Произошла непредвиденная ошибка.');
                } finally {
                    showLoader(false);
                }
            }

            /* Рендер сетки фильтров в модалке — всё видно без скролла */
            function renderFilterModal() {
                // Сначала наполним <select>'ы месяцами/годами и подтянем значения из state
                populateDateSelects();
                syncDateSelects();
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
                // Подпись диапазона даты создания (если задан).
                const dateLbl = formatCreatedAtRangeLabel();
                const datePart = dateLbl ? ` · ${dateLbl}` : '';
                const isDefault = state.country === 'all'
                    && state.origin === 'all'
                    && state.priceSort === 'default'
                    && !dateLbl;
                if (isDefault) {
                    dom.filterSummary.textContent = 'Все страны · Любое · По умолчанию';
                } else {
                    dom.filterSummary.textContent = `${country} · ${origin} · ${priceLbl}${datePart}`;
                }
                // Бейдж со счётчиком активных фильтров
                if (dom.filterBadge) {
                    const active = (state.country !== 'all' ? 1 : 0)
                        + (state.origin !== 'all' ? 1 : 0)
                        + (state.priceSort !== 'default' ? 1 : 0)
                        + (dateLbl ? 1 : 0);
                    if (active > 0) {
                        dom.filterBadge.textContent = String(active);
                        dom.filterBadge.hidden = false;
                    } else {
                        dom.filterBadge.hidden = true;
                    }
                }
            }

            // Возвращает короткую подпись диапазона даты создания
            // (например "01.2013 → 03.2020") или пустую строку, если фильтр не задан.
            function formatCreatedAtRangeLabel() {
                const fy = state.createdFromYear, fm = state.createdFromMonth;
                const ty = state.createdToYear,   tm = state.createdToMonth;
                const hasFrom = fy !== 'all' || fm !== 'all';
                const hasTo   = ty !== 'all' || tm !== 'all';
                if (!hasFrom && !hasTo) return '';
                const fromTxt = hasFrom ? formatCreatedAtPart(fm, fy) : '...';
                const toTxt   = hasTo   ? formatCreatedAtPart(tm, ty) : '...';
                return `${fromTxt} → ${toTxt}`;
            }
            function formatCreatedAtPart(month, year) {
                const mm = (month && month !== 'all') ? String(month).padStart(2, '0') : '..';
                const yy = (year  && year  !== 'all') ? String(year) : '....';
                return `${mm}.${yy}`;
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
                    // ====== Полное имя продавца ======
                    // Полное имя приходит как single field `seller_full_name`
                    // с бэка (first_name + last_name). Если его не�� — фоллбек
                    // на username либо «id XXXX». Рендерим в ОДНУ СТРОКУ,
                    // как просили — не стек из имени + хендла.
                    const fullName = (it.seller_full_name || '').trim();
                    const sellerHandle = it.seller_username
                        ? '@' + it.seller_username
                        : (it.seller_id ? 'id ' + it.seller_id : 'Платформа');
                    const sellerName = fullName || sellerHandle;
                    const initialSrc = fullName || sellerHandle;
                    const sellerInitial = (initialSrc.replace('@', '').replace('id ', '') || '?').charAt(0).toUpperCase();
                    const sRating = Number(it.seller_rating) || 0;
                    const sReviews = Number(it.seller_reviews) || 0;
                    // ====== Аватарка продавца ======
                    // Если пришла seller_photo_url — рендерим <img>, иначе
                    // фоллбек с первой буквой имени.
                    const avatarHtml = it.seller_photo_url
                        ? `<img src="${escapeHtml(it.seller_photo_url)}" alt="" loading="lazy" onerror="this.parentNode.textContent='${sellerInitial.replace(/'/g, "\\'")}'">`
                        : sellerInitial;
                    // ====== Бейдж «Premium» (правый верхний угол) ======
                    const premiumBadgeHtml = it.has_premium
                        ? `<div class="card-premium-badge" title="На аккаунте активна подписка Telegram Premium"><span class="card-premium-badge-text">Телеграмм премиум</span></div>`
                        : '';
                    // ====== Строка метаданных (теперь отдельными бейджами,
                    // не вшитыми в описание) ======
                    // В карточке показываем два маленьких тега: сессия
                    // верифицирована + дата регистрации (если есть).
                    // Флаг страны убран из шапки; страна теперь — отдельный бей��ж.
                    // По просьбе: дата регистрации — как отдельная плашка
                    // рядом с «Сессия проверена», НЕ в описании.
                    const metaBadges = [];
                    if (it.is_verified) {
                        metaBadges.push(
                            `<span class="card-meta-badge verified" title="Сессия прошла верификацию">` +
                            `<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">` +
                            `<path fill="currentColor" d="M8 0 1 3v5c0 4 3 7 7 8 4-1 7-4 7-8V3L8 0zm3.7 5.3-4.2 4.2L5.7 7.7l-1 1 2.8 2.8L12.7 6.3l-1-1z"/>` +
                            `</svg>Сессия проверена</span>`
                        );
                    }
                    if (it.reg_text) {
                        metaBadges.push(
                            `<span class="card-meta-badge reg" title="Дата регистрации Telegram-аккаунта">` +
                            `<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">` +
                            `<path fill="currentColor" d="M5 1v1H3a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1h-2V1h-1v1H6V1H5zm-2 4h10v9H3V5zm1 1v2h3V6H4zm5 0v2h3V6H9z"/>` +
                            `</svg>${escapeHtml(it.reg_text.replace('Регистрация: ', ''))}</span>`
                        );
                    }
                    // Страна теперь НЕ в строке meta-бейджей, а вынесена на
                    // отдельную строку в шапке карточки с мини-флагом (см. ниже
                    // .card-country-line). Раньше она была бейджем с иконкой
                    // глобуса, теперь — самостоятельная строка вида "🇺🇸 США".
                    const metaHtml = metaBadges.length
                        ? `<div class="card-meta">${metaBadges.join('')}</div>`
                        : '';

                    // ====== Строка продавца: одна строка ======
                    // Имя + @username, в одну горизонтальную линию.
                    // Никогда не переносим на две строки — ровно как
                    // был «номер телефона» в моноширинном стиле.
                    //
                    // data-seller-id нужен, чтобы обработчик клика по
                    // аватару/нику открыл публичный пр��филь этого продавца
                    // (а не карточку товара, как делает клик по самой карточке).
                    const sellerIdForProfile = it.seller_id ? Number(it.seller_id) : 0;
                    const sellerLineHtml =
                        `<div class="card-seller-line">` +
                            `<div class="card-seller-avatar" data-seller-id="${sellerIdForProfile}">${avatarHtml}</div>` +
                            `<div class="card-seller-text" data-seller-id="${sellerIdForProfile}">` +
                                `<span class="card-seller-name">${escapeHtml(sellerName)}</span>` +
                                (it.seller_username
                                    ? `<span class="card-seller-handle-inline">@${escapeHtml(it.seller_username)}</span>`
                                    : '') +
                            `</div>` +
                            (sRating > 0
                                ? `<span class="card-seller-rating"><span class="star">★</span>${sRating.toFixed(1)}<span class="reviews">(${sReviews})</span></span>`
                                : `<span class="card-seller-rating no-rating">·</span>`) +
                        `</div>`;

                    // ====== Название лота (lot title) ======
                    // Это `listing.title` с бэка. По новому дизайну именно
                    // название лота идёт �� шапку карточки вместо страны.
                    // Фоллбек — "Telegram-аккаунт" если продавец не задал
                    // своё название (защита от пустой шапки).
                    const lotTitle = (it.title && String(it.title).trim())
                        ? String(it.title).trim()
                        : 'Telegram-аккаунт';
                    // ====== Страна отдельной строкой с мини-флагом ======
                    // it.flag — эмодзи-флаг страны (приходит с бэка,
                    // маппинг COUNTRY_FLAGS). Страна вынесена из строки
                    // meta-бейджей в отдельную строку шапки карточки, чтобы
                    // не смешивать с датой регистрации и сессией.
                    const countryLine = it.country
                        ? `<div class="card-country-line"><span class="card-country-flag">${escapeHtml(it.flag || '🌍')}</span><span class="card-country-name">${escapeHtml(it.country)}</span></div>`
                        : '';
                    card.innerHTML = `
                        ${premiumBadgeHtml}
                        <div class="card-top">
                            <div class="card-title-block">
                                <div class="card-lot-title">${escapeHtml(lotTitle)}</div>
                                ${countryLine}
                                <span class="card-origin">${it.origin_icon} ${escapeHtml(it.origin_label)}</span>
                            </div>
                        </div>
                        ${metaHtml}
                        <div class="card-price">${formatPrice(it.price)}<span class="rub">₽</span></div>
                        ${sellerLineHtml}
                    `;
                    card.addEventListener('click', () => openItem(it));
                    // ====== Клик по аватару/нику продавца в карточке ======
                    // Открывает публичный профиль, а не карточку товара.
                    // Если продавец неизвестен (платформенный лот) — игнорируем.
                    if (sellerIdForProfile) {
                        const sellerAvatar = card.querySelector('.card-seller-avatar');
                        const sellerText   = card.querySelector('.card-seller-text');
                        const openSeller = (ev) => {
                            ev.preventDefault();
                            ev.stopPropagation();
                            // лёгкий визуальный фидбек
                            if (ev.currentTarget) ev.currentTarget.classList.add('is-clicked');
                            setTimeout(() => {
                                if (ev.currentTarget) ev.currentTarget.classList.remove('is-clicked');
                            }, 180);
                            openUserProfile(sellerIdForProfile, {
                                preload: {
                                    name: sellerName,
                                    username: it.seller_username || '',
                                    photo_url: it.seller_photo_url || null,
                                },
                            });
                        };
                        if (sellerAvatar) sellerAvatar.addEventListener('click', openSeller);
                        if (sellerText)   sellerText.addEventListener('click', openSeller);
                    }
                    frag.appendChild(card);
                });
                dom.catalog.appendChild(frag);
            }

            function openFiltersModal() {
                renderFilterModal();
                openModal('filtersModal');
            }

            function applyFiltersFromModal() {
                // Считываем выбор из DOM (могли покли��ать до нажатия «Применить»)
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
                // Дата создания: читаем напрямую из <select>'ов.
                // Если задана только одна граница (от или до) — вторую оставляем 'all'.
                state.createdFromMonth = dom.filterFromMonth ? dom.filterFromMonth.value : 'all';
                state.createdFromYear  = dom.filterFromYear  ? dom.filterFromYear.value  : 'all';
                state.createdToMonth   = dom.filterToMonth   ? dom.filterToMonth.value   : 'all';
                state.createdToYear    = dom.filterToYear    ? dom.filterToYear.value    : 'all';
                // Подсветка активных селектов сразу после применения
                syncDateSelects();
                // Простая валидация: если «от» позже «до» — сбрасываем диапазон и сообщаем.
                if (isCreatedRangeInverted()) {
                    showToast('«От» не может быть позже «До» — фильтр по дате сброшен');
                    state.createdFromMonth = 'all';
                    state.createdFromYear  = 'all';
                    state.createdToMonth   = 'all';
                    state.createdToYear    = 'all';
                    syncDateSelects();
                }
                closeModal('filtersModal');
                loadCatalog();
            }

            // true, если заданы обе границы (от и до) и from > to по (год, месяц).
            function isCreatedRangeInverted() {
                const fy = state.createdFromYear, fm = state.createdFromMonth;
                const ty = state.createdToYear,   tm = state.createdToMonth;
                if (fy === 'all' || ty === 'all') return false;
                const fNum = Number(fy) * 12 + (fm === 'all' ? 0 : Number(fm) - 1);
                const tNum = Number(ty) * 12 + (tm === 'all' ? 11 : Number(tm) - 1);
                return fNum > tNum;
            }

            function resetFilters() {
                state.country = 'all';
                state.origin = 'all';
                state.priceSort = 'default';
                state.createdFromMonth = 'all';
                state.createdFromYear  = 'all';
                state.createdToMonth   = 'all';
                state.createdToYear    = 'all';
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
            /* Состояние валидации: null | 'checking' | 'valid' | 'invalid' | 'error' */
            const validateState = { status: null };

            // Сбрасывает UI валидации — вызывается при открытии новой модалки
            function resetValidateUI() {
                validateState.status = null;
                if (dom.validateStatus) dom.validateStatus.className = 'validate-status hidden';
                if (dom.validateStatusIcon) dom.validateStatusIcon.textContent = '';
                if (dom.validateStatusText) dom.validateStatusText.textContent = '';
                if (dom.buyConfirmBtn) dom.buyConfirmBtn.classList.add('hidden');
                if (dom.buyBtn) {
                    dom.buyBtn.disabled = false;
                    dom.buyBtn.classList.remove('loading');
                    dom.buyBtn.textContent = 'Купить';
                }
            }

            function showValidateStatus(state, text) {
                if (!dom.validateStatus) return;
                dom.validateStatus.className = 'validate-status ' + state;
                const icon = dom.validateStatusIcon;
                const spinner = dom.validateStatus.querySelector('.validate-status-spinner');

                if (state === 'checking') {
                    // Спиннер вместо иконки
                    if (icon) icon.innerHTML = '<span class="validate-status-spinner"></span>';
                } else if (state === 'valid') {
                    if (icon) icon.textContent = '';
                } else if (state === 'invalid') {
                    if (icon) icon.textContent = '';
                } else {
                    if (icon) icon.textContent = '';
                }
                if (dom.validateStatusText) dom.validateStatusText.textContent = text;
            }

            function openItem(it) {
                buyState.item = it;
                // Сбрасываем UI валидации при каждом открытии нового товара
                resetValidateUI();
                // ====== Шапк�� модалки: название лота (как в карточке) ======
                // Раньше в шапке модалки дублировалась страна с флагом.
                // Теперь показываем на��вание лота (listing.title), флаг убран.
                if (dom.itemFlag) dom.itemFlag.style.display = 'none';
                const lotTitle = (it.title && String(it.title).trim())
                    ? String(it.title).trim()
                    : 'Telegram-аккаунт';
                if (dom.itemCountry) dom.itemCountry.textContent = lotTitle;
                // Под шапкой — тип аккаунта (Авторег/Саморег/…). Страна
                // уже показана в характеристиках ниже, дублировать не нужно.
                dom.itemOrigin.textContent = `${it.origin_icon} ${it.origin_label}`;
                // ====== TELEGRAM PREMIUM ======
                // В детальной модалке тоже показываем бейдж, чтобы покупатель
                // видел Premium-статус крупно и мог принять решение. Элемент
                // создаём лениво (один раз), а потом просто показываем/скрываем.
                let premiumBadge = document.getElementById('itemPremiumBadge');
                if (!premiumBadge) {
                    // Вставляем после #itemOrigin — он уже есть в DOM по HTML-шаблону.
                    const anchor = dom.itemOrigin && dom.itemOrigin.parentNode;
                    if (anchor) {
                        premiumBadge = document.createElement('div');
                        premiumBadge.id = 'itemPremiumBadge';
                        premiumBadge.className = 'item-premium-badge';
                        premiumBadge.textContent = 'Телеграмм премиум';
                        premiumBadge.style.display = 'none';
                        anchor.appendChild(premiumBadge);
                    }
                }
                if (premiumBadge) {
                    premiumBadge.style.display = it.has_premium ? '' : 'none';
                }

                // Цена + никнейм продавца + рейтинг
                const priceNum = Number(it.price) || 0;
                dom.itemPrice.textContent = formatPrice(priceNum) + ' ₽';

                // ====== Полное имя продавца в модалке ======
                // ОДНОЙ СТРОКОЙ: «Имя Фамилия @username» (ник не переносится
                // на отдельную строку, ка�� было раньше). Бэк уже склеил имя
                // в `seller_full_name`, при необходимости fallback на handle.
                const fullName = (it.seller_full_name || '').trim();
                const sellerHandle = it.seller_username
                    ? '@' + it.seller_username
                    : (it.seller_id ? 'id ' + it.seller_id : 'Платформа');
                const sellerName = fullName || sellerHandle;
                const initialSrc = fullName || sellerHandle;
                const sellerInitial = (initialSrc.replace('@', '').replace('id ', '') || '?').charAt(0).toUpperCase();
                // ====== Аватарка продавца в модалке ======
                // Если backend вернул seller_photo_url — рисуем <img>.
                // Иначе — фоллбек с буквой.
                const avatarEl = document.getElementById('itemSellerAvatar');
                if (avatarEl) {
                    if (it.seller_photo_url) {
                        avatarEl.innerHTML =
                            `<img src="${escapeHtml(it.seller_photo_url)}" alt="" ` +
                            `onerror="this.parentNode.textContent='${sellerInitial.replace(/'/g, "\\'")}'">`;
                    } else {
                        avatarEl.textContent = sellerInitial;
                    }
                    // data-seller-id нужен, чтобы обработчик клика открыл
                    // публичный профиль. Если прода��ец не известен (платформенный
                    // лот) — клик игнорируется.
                    const itemSellerId = it.seller_id ? Number(it.seller_id) : 0;
                    if (itemSellerId) {
                        avatarEl.dataset.sellerId = String(itemSellerId);
                    } else {
                        delete avatarEl.dataset.sellerId;
                    }
                }
                // Полное имя и @handle рисуем в одну строку с разделителем.
                if (dom.itemSeller) {
                    dom.itemSeller.innerHTML = it.seller_username
                        ? `${escapeHtml(sellerName)} <span class="item-seller-handle-inline">@${escapeHtml(it.seller_username)}</span>`
                        : escapeHtml(sellerName);
                    // Тот же data-seller-id на блоке с именем — чтобы клик
                    // по «Иван @ivan» тоже открывал профиль.
                    const itemSellerId2 = it.seller_id ? Number(it.seller_id) : 0;
                    if (itemSellerId2) {
                        dom.itemSeller.dataset.sellerId = String(itemSellerId2);
                    } else {
                        delete dom.itemSeller.dataset.sellerId;
                    }
                }

                const openSellerProfileFromItem = (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    const sellerId = Number(it.seller_id || 0);
                    if (!sellerId) return;
                    closeModal('itemModal');
                    openUserProfile(sellerId, {
                        preload: {
                            name: sellerName,
                            username: it.seller_username || '',
                            photo_url: it.seller_photo_url || null,
                        },
                    });
                };
                if (avatarEl) avatarEl.onclick = openSellerProfileFromItem;
                if (dom.itemSeller) dom.itemSeller.onclick = openSellerProfileFromItem;

                const rating = Number(it.seller_rating);
                const reviews = Number(it.seller_reviews) || 0;
                if (dom.itemRating) {
                    if (rating && rating > 0) {
                        dom.itemRating.textContent = `${rating.toFixed(1)} ★ (${reviews} отзывов)`;
                    } else {
                        dom.itemRating.textContent = 'Новый продавец';
                    }
                }

                // ====== Описание аккаунта в модалке — улучшенная версия ======
                // Логика:
                //   1. Берём текст продавца (`description`) и название лота
                //      (`title`) — они приоритетны.
                //   2. Если своего текста нет — собираем живой дефолт на
                //      основе country / origin / verified / premium / reg.
                //   3. Плюс к этому — структурированный список характеристик
                //      аккаунта (страна, тип, дата регистрации, бонусы,
                //      условия выдачи), который строится всегда.
                if (dom.itemDescription) {
                    const rawDesc = (it.description && String(it.description).trim())
                        ? String(it.description).trim()
                        : '';
                    const lotTitle = (it.title && String(it.title).trim())
                        ? String(it.title).trim()
                        : '';
                    const country = (it.country || '').toString().trim();
                    const origin = (it.origin || '').toString().trim();
                    const verified = !!it.is_verified;
                    const premium = !!it.has_premium;
                    const regText = (it.reg_text || '').toString().trim();
                    const sessionAgo = regText ? regText.replace(/^Регистрация:\s*/i, '') : '';

                    // Тип аккаунта для человекочитаемой подписи
                    const originLabel = (() => {
                        const l = (it.origin_label || '').toString().trim();
                        return l || (origin || '');
                    })();
                    const ageLabel = (() => {
                        // Грубая оценка «возраста» аккаунта по году регистрации.
                        if (!it.reg_year) return '';
                        try {
                            const yearsOld = Math.max(0, new Date().getFullYear() - Number(it.reg_year));
                            if (yearsOld === 0) return 'свежая регистрация';
                            if (yearsOld === 1) return '≈ 1 год';
                            if (yearsOld <= 4) return `≈ ${yearsOld} года`;
                            return `≈ ${yearsOld} лет`;
                        } catch (e) { return ''; }
                    })();

                    // Характеристики-строки (карточки плюсов)
                    const rows = [];
                    if (country) {
                        rows.push(
                            `<div class="desc-row"><span class="desc-emoji">🌍</span>` +
                            `<span class="desc-text">Страна выпуска: <strong>${escapeHtml(country)}</strong></span></div>`
                        );
                    }
                    if (originLabel) {
                        rows.push(
                            `<div class="desc-row"><span class="desc-emoji">${escapeHtml(it.origin_icon || '✨')}</span>` +
                            `<span class="desc-text">Тип аккаунта: <strong>${escapeHtml(originLabel)}</strong></span></div>`
                        );
                    }
                    if (sessionAgo) {
                        const agePart = ageLabel ? ` <span class="desc-sub">(${escapeHtml(ageLabel)})</span>` : '';
                        rows.push(
                            `<div class="desc-row"><span class="desc-emoji">📅</span>` +
                            `<span class="desc-text">Регистрация: <strong>${escapeHtml(sessionAgo)}</strong>${agePart}</span></div>`
                        );
                    }
                    rows.push(
                        `<div class="desc-row"><span class="desc-emoji">${verified ? '✅' : '⚠️'}</span>` +
                        `<span class="desc-text">${verified
                            ? 'Сессия <strong>прошла проверку</strong> — вход без сброса и блокировок'
                            : 'Сессия <strong>не верифицирована</strong> — рекомендуем уточнить у продавца'}</span></div>`
                    );
                    if (premium) {
                        rows.push(
                            `<div class="desc-row"><span class="desc-emoji">⭐</span>` +
                            `<span class="desc-text">На аккаунте активен <strong>Telegram Premium</strong></span></div>`
                        );
                    }
                    rows.push(
                        `<div class="desc-row"><span class="desc-emoji">⚡</span>` +
                        `<span class="desc-text">Выдача <strong>сразу после оплаты</strong> — логин и данные приходят в этот чат</span></div>`
                    );
                    rows.push(
                        `<div class="desc-row"><span class="desc-emoji">🔒</span>` +
                        `<span class="desc-text">Без действующих банов и ограничений на момент продажи</span></div>`
                    );
                    rows.push(
                        `<div class="desc-row"><span class="desc-emoji">💬</span>` +
                        `<span class="desc-text">Если остались вопросы — напишите продавцу до оплаты</span></div>`
                    );

                    // Лид-абзац: приоритет — текст продавца, иначе собираем
                    // ж��вой дефолт. Используем название лота, чтобы текст
                    // звучал конкретно, а не шаблонно.
                    let leadHtml;
                    if (rawDesc) {
                        // Текст продавца — рендерим как HTML, потому что фронт
                        // уже отдаёт безопасную строку (escapeHtml), а авторы
                        // могут ставить переносы строк, которые превратим в <br>.
                        leadHtml = escapeHtml(rawDesc).replace(/\n/g, '<br>');
                    } else {
                        // Дефолт — собираем из фактов, не шаблонно.
                        const parts = [];
                        if (lotTitle) {
                            parts.push(`<strong>${escapeHtml(lotTitle)}</strong>`);
                        } else if (country) {
                            parts.push(`Аккаунт <strong>${escapeHtml(country)}</strong>`);
                        } else {
                            parts.push('Аккаунт');
                        }
                        if (originLabel) {
                            parts.push(`типа <strong>${escapeHtml(originLabel)}</strong>`);
                        }
                        if (sessionAgo) {
                            parts.push(`(регистрация — <strong>${escapeHtml(sessionAgo)}</strong>)`);
                        }
                        const head = parts.join(' ') + '.';
                        if (verified) {
                            leadHtml = `${head} Сессия прошла полную проверку, а��каунт готов к выдаче сразу после оплаты. ${premium ? 'Бонусом идёт активный Telegram Premium. ' : ''}Можно заходить и пользоваться без ограничений.`;
                        } else {
                            leadHtml = `${head} Готов к выдаче после оплаты. По любым вопросам пишите продавцу — он на связи.`;
                        }
                    }

                    dom.itemDescription.innerHTML =
                        `<div class="item-description-title"><span class="item-description-icon">📋</span>О ЛОТЕ</div>` +
                        `<div class="item-description-body"><p>${leadHtml}</p></div>` +
                        `<div class="item-description-grid">${rows.join('')}</div>`;
                }

                // ====== Метаданные в списке .item-features ======
                // По просьбе: дата регистрации — не в описании, а строкой
                // рядом с «Сессия верифицирована». Реализовано через
                // CSS-классы .hidden на <li data-feature="...">.
                const featuresRoot = document.getElementById('itemFeatures');
                if (featuresRoot) {
                    const regLi = featuresRoot.querySelector('[data-feature="reg"]');
                    const regTextEl = document.getElementById('itemRegText');
                    if (it.reg_text && regLi && regTextEl) {
                        regTextEl.textContent = it.reg_text.replace(/^Регистрация:\s*/i, '');
                        regLi.classList.remove('hidden');
                    } else if (regLi) {
                        regLi.classList.add('hidden');
                    }
                    const premiumLi = featuresRoot.querySelector('[data-feature="premium"]');
                    if (premiumLi) {
                        premiumLi.classList.toggle('hidden', !it.has_premium);
                    }
                    const verifiedLi = featuresRoot.querySelector('[data-feature="verified"]');
                    if (verifiedLi) {
                        verifiedLi.classList.toggle('hidden', !it.is_verified);
                    }
                }

                // Кнопка «Купить» — показываем актуальную цену
                if (dom.buyBtn) {
                    dom.buyBtn.textContent = `Купить за ${formatPrice(priceNum)} ₽`;
                    dom.buyBtn.disabled = false;
                }

                // Кнопка «Написать продавцу» — видна только если это P2P объявление с известным seller_id.
                if (dom.itemChatBtn) {
                    const sid = it.seller_id ? Number(it.seller_id) : 0;
                    const myId = (state.tgUser && state.tgUser.id) || 0;
                    if (sid && sid !== myId) {
                        dom.itemChatBtn.classList.remove('hidden');
                        dom.itemChatBtn.dataset.sellerId = String(sid);
                        dom.itemChatBtn.dataset.sellerUsername = it.seller_username || '';
                    } else {
                        dom.itemChatBtn.classList.add('hidden');
                        dom.itemChatBtn.dataset.sellerId = '';
                        dom.itemChatBtn.dataset.sellerUsername = '';
                    }
                }

                openModal('itemModal');
            }

            // ===== ЭТАП 1: Валидация аккаунта =====
            // Вызывается при нажатии "Купить". Проверяет аккаунт через Telethon.
            // После успеха показывает кнопку «Купить (подтвердить)».
            async function buyCurrentItem() {
                if (buyState.busy || !buyState.item) return;

                // Если валидация уже прошла успешно — не запускаем заново
                if (validateState.status === 'valid') {
                    confirmBuyCurrentItem();
                    return;
                }

                buyState.busy = true;
                if (dom.buyBtn) {
                    dom.buyBtn.disabled = true;
                    dom.buyBtn.classList.add('loading');
                    dom.buyBtn.textContent = 'Проверяем...';
                }
                if (dom.buyConfirmBtn) dom.buyConfirmBtn.classList.add('hidden');

                validateState.status = 'checking';
                showValidateStatus('checking', 'Проверяем на валид, это может занять до минуты...');

                try {
                    const r = await api('/api/validate_account', {
                        method: 'POST',
                        body: JSON.stringify({ account_id: buyState.item.id }),
                    });

                    if (!r.ok) {
                        // Ошибка соединения или сервера — не снимаем, даём повторить
                        const err = (r.data && r.data.error) || 'unknown';
                        validateState.status = 'error';
                        showValidateStatus('invalid', 'Не удалось проверить аккаунт. Попробуйте ещё раз.');
                        if (dom.buyBtn) {
                            dom.buyBtn.disabled = false;
                            dom.buyBtn.classList.remove('loading');
                            dom.buyBtn.textContent = 'Купить';
                        }
                        return;
                    }

                    if (r.data.valid) {
                        // Аккаунт валидный — показываем кнопку подтверждения
                        validateState.status = 'valid';
                        showValidateStatus('valid', 'Аккаунт проверен — всё в порядке. Подтверд��те покупку.');
                        if (dom.buyBtn) {
                            dom.buyBtn.disabled = true;
                            dom.buyBtn.classList.remove('loading');
                            dom.buyBtn.textContent = 'Купить';
                        }
                        if (dom.buyConfirmBtn) {
                            dom.buyConfirmBtn.classList.remove('hidden');
                            dom.buyConfirmBtn.disabled = false;
                        }
                    } else {
                        // Аккаунт невалиден — объявление уже снято сервером
                        validateState.status = 'invalid';
                        showValidateStatus('invalid', 'Аккаунт не валиден — объявление снято. Выберите другой аккаунт.');
                        if (dom.buyBtn) {
                            dom.buyBtn.disabled = true;
                            dom.buyBtn.classList.remove('loading');
                            dom.buyBtn.textContent = 'Недоступен';
                        }
                        // Обновляем каталог — убираем снятое объявление
                        setTimeout(async () => {
                            try { await loadCatalog(); } catch (e) { /* noop */ }
                        }, 800);
                    }
                } catch (e) {
                    validateState.status = 'error';
                    showValidateStatus('invalid', 'Ошибка сети при проверке. Попробуйте ещё раз.');
                    if (dom.buyBtn) {
                        dom.buyBtn.disabled = false;
                        dom.buyBtn.classList.remove('loading');
                        dom.buyBtn.textContent = 'Купить';
                    }
                } finally {
                    buyState.busy = false;
                }
            }

            // ===== ЭТАП 2: Реальная покупка (после успешной валидации) =====
            async function confirmBuyCurrentItem() {
                if (buyState.busy || !buyState.item) return;
                if (validateState.status !== 'valid') return;

                buyState.busy = true;
                if (dom.buyConfirmBtn) {
                    dom.buyConfirmBtn.disabled = true;
                    dom.buyConfirmBtn.classList.add('loading');
                    dom.buyConfirmBtn.textContent = 'Покупаем...';
                }

                try {
                    const r = await api('/api/buy', {
                        method: 'POST',
                        body: JSON.stringify({ account_id: buyState.item.id }),
                    });

                    if (r.ok) {
                        showToast('Покупка успешна!', 'success');
                        if (typeof r.data.balance !== 'undefined') {
                            setBalanceUI(r.data.balance, {
                                hold: r.data.hold_balance,
                                syncedAt: r.data.synced_at,
                            });
                        } else {
                            refreshBalance({ silent: true });
                        }
                        // flash-эффект перед закрытием
                        const sheet = document.querySelector('#itemModal .modal-sheet');
                        if (sheet) {
                            sheet.style.transition = 'transform 0.18s, opacity 0.18s';
                            sheet.style.transform = 'scale(0.96)';
                            sheet.style.opacity = '0.0';
                            setTimeout(() => {
                                sheet.style.transform = '';
                                sheet.style.opacity = '';
                            }, 220);
                        }
                        setTimeout(() => {
                            closeModal('itemModal');
                            loadCatalog();
                        }, 200);
                        // Авто-открытие чата с продавцом
                        const sellerId = r.data && r.data.seller_id;
                        const threadId = r.data && r.data.chat_thread_id;
                        if (sellerId && threadId && typeof openChatByPeerId === 'function') {
                            setTimeout(async () => {
                                try { await pollChats(); } catch (e) { /* noop */ }
                                openChatByPeerId(sellerId, null);
                            }, 280);
                        }
                    } else {
                        const err = (r.data && r.data.error) || 'unknown';
                        showToast(translateBuyError(err), 'error');
                        // shake-эффект при ошибке
                        const sheet = document.querySelector('#itemModal .modal-sheet');
                        if (sheet) {
                            sheet.classList.remove('shake');
                            void sheet.offsetWidth;
                            sheet.classList.add('shake');
                            setTimeout(() => sheet.classList.remove('shake'), 450);
                        }
                        if (dom.buyConfirmBtn) {
                            dom.buyConfirmBtn.disabled = false;
                            dom.buyConfirmBtn.classList.remove('loading');
                            dom.buyConfirmBtn.textContent = 'Купить (подтвердить)';
                        }
                        if (err === 'already_sold' || err === 'not_found') {
                            try { await loadCatalog(); } catch (e) { /* noop */ }
                            try { await loadCategories(); } catch (e) { /* noop */ }
                            setTimeout(() => {
                                try { closeModal('itemModal'); } catch (e) { /* noop */ }
                            }, 320);
                        }
                    }
                } catch (e) {
                    showToast('Ошибка сети', 'error');
                    const sheet = document.querySelector('#itemModal .modal-sheet');
                    if (sheet) {
                        sheet.classList.remove('shake');
                        void sheet.offsetWidth;
                        sheet.classList.add('shake');
                        setTimeout(() => sheet.classList.remove('shake'), 450);
                    }
                    if (dom.buyConfirmBtn) {
                        dom.buyConfirmBtn.disabled = false;
                        dom.buyConfirmBtn.classList.remove('loading');
                        dom.buyConfirmBtn.textContent = 'Купить (подтвердить)';
                    }
                } finally {
                    buyState.busy = false;
                }
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
                        // flash-эффект на модалке перед закрытием
                        const sheet = document.querySelector('#itemModal .modal-sheet');
                        if (sheet) {
                            sheet.style.transition = 'transform 0.18s, opacity 0.18s';
                            sheet.style.transform = 'scale(0.96)';
                            sheet.style.opacity = '0.0';
                            setTimeout(() => {
                                sheet.style.transform = '';
                                sheet.style.opacity = '';
                            }, 220);
                        }
                        setTimeout(() => {
                            closeModal('itemModal');
                            loadCatalog();
                        }, 200);
                        // Авто-открытие чата с продавцом (FunPay-стиль).
                        // После покупки бот/апп создал чат и положил туда
                        // системное сообщение о сделке — сразу покажем его.
                        const sellerId = r.data && r.data.seller_id;
                        const threadId = r.data && r.data.chat_thread_id;
                        if (sellerId && threadId && typeof openChatByPeerId === 'function') {
                            // По��тянем свежий список чатов, чтобы получить
                            // имя/username продавца (а потом сразу откроем чат).
                            setTimeout(async () => {
                                try { await pollChats(); } catch (e) { /* noop */ }
                                openChatByPeerId(sellerId, null);
                            }, 280);
                        }
                    } else {
                        const err = (r.data && r.data.error) || 'unknown';
                        showToast(translateBuyError(err), 'error');
                        // shake-эффект для модалки при ошибке
                        const sheet = document.querySelector('#itemModal .modal-sheet');
                        if (sheet) {
                            sheet.classList.remove('shake');
                            // force reflow для рестарта анимации
                            void sheet.offsetWidth;
                            sheet.classList.add('shake');
                            setTimeout(() => sheet.classList.remove('shake'), 450);
                        }
                        if (dom.buyBtn) {
                            dom.buyBtn.disabled = false;
                            dom.buyBtn.classList.remove('loading');
                            dom.buyBtn.textContent = oldText || 'Купить';
                        }
                        // ⚠️ FIX «ложный already_sold»:
                        // Если аккаунт реально куплен (already_sold или
                        // not_found) — карточка в каталоге уже мертва.
                        // Сразу обновляем каталог и закрываем модалку,
                        // чтобы юзер не кликал по устаревшей кнопке
                        // «Купить» и не пол��чал ту же ошибку повторно.
                        if (err === 'already_sold' || err === 'not_found') {
                            try { await loadCatalog(); } catch (e) { /* noop */ }
                            try { await loadCategories(); } catch (e) { /* noop */ }
                            // Чуть позже — чтобы shake успел отыграть
                            setTimeout(() => {
                                try { closeModal('itemModal'); } catch (e) { /* noop */ }
                            }, 320);
                        }
                    }
                } catch (e) {
                    showToast('Ошибка сети', 'error');
                    const sheet = document.querySelector('#itemModal .modal-sheet');
                    if (sheet) {
                        sheet.classList.remove('shake');
                        void sheet.offsetWidth;
                        sheet.classList.add('shake');
                        setTimeout(() => sheet.classList.remove('shake'), 450);
                    }
                    if (dom.buyBtn) {
                        dom.buyBtn.disabled = false;
                        dom.buyBtn.classList.remove('loading');
                        dom.buyBtn.textContent = oldText || 'Купить';
                    }
                } finally {
                    buyState.busy = false;
                }
            }

            function translateBuyError(err) {
                const map = {
                    'unauthorized': 'Не авторизовано',
                    'not_found': 'Аккаунт уже купили',
                    'already_sold': 'Аккаунт уже продан',
                    'has_active_listing': 'Аккаунт выставлен на маркетплейсе в боте',
                    'low_balance': 'Недостаточно средств — пополните баланс',
                    'self_buy': 'Нельзя купить свой аккаунт',
                    'integrity_error': 'Ошибка БД, попробуйте ещё раз',
                    'server_error': 'Ошибка сервера, попробуйте позже',
                    'invalid_account': 'Аккаунт не прошёл проверку',
                };
                return map[err] || err;
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

            /* ===== Публичный профиль чужого юзера (открывается по клику на ник/аватар) =====
               — Показывает модалку с публичной инфой о юзере:
                 имя, @username, аватар, рейтинг, число отзывов, дата регистрации.
               — Не показывает баланс / холд / total_spent / total_earned (это приватно).
               — Если кликнули по себе — предлагает открыть свой полный профиль.
               — Если кликнули по чужому — предлагает написать ему сообщение.
               Не блокирует UI: при долгом запросе показывается спиннер в модалке.
            */
            let _userProfileLoading = false;
            let _userProfileCurrentId = 0;
            async function openUserProfileModalLegacy(telegramId, opts) {
                telegramId = Number(telegramId || 0);
                if (!telegramId) return;
                opts = opts || {};
                // Защита от двойного открытия одной и той же модалки
                if (_userProfileLoading && _userProfileCurrentId === telegramId) {
                    openModal('userModal');
                    return;
                }
                _userProfileLoading = true;
                _userProfileCurrentId = telegramId;

                // Сначала показываем модалку со спиннером, чтобы UI отреагировал
                // МГНОВЕННО (без задержки на сеть). Контент подставится после fetch.
                const myId = (state.tgUser && state.tgUser.id) || 0;
                const isSelf = (telegramId === myId);

                // Прелоад-данные: если передали (из каталога / чата) —
                // покажем сразу, пока грузится свежий профиль.
                const pre = opts.preload || null;
                const initialSrc = (pre && pre.name)
                    ? pre.name
                    : (pre && pre.username ? '@' + pre.username : (isSelf ? 'Вы' : 'id ' + telegramId));
                const initialChar = (String(initialSrc).replace(/^[@\s]+/, '').charAt(0) || '?').toUpperCase();
                if ($('userModalInitial')) $('userModalInitial').textContent = initialChar;
                if ($('userModalName')) $('userModalName').textContent = initialSrc;
                if ($('userModalUsername')) {
                    $('userModalUsername').textContent = (pre && pre.username)
                        ? '@' + pre.username
                        : (isSelf ? 'это ��ы' : 'telegram');
                }
                if ($('userModalAvatar')) {
                    // Если в прелоаде есть фото — сразу покажем
                    if (pre && pre.photo_url) {
                        $('userModalAvatar').innerHTML = '<img src="' + escapeHtml(pre.photo_url) + '" alt="" onerror="this.parentNode.textContent=\'' + initialChar + '\'">';
                    } else {
                        $('userModalAvatar').innerHTML = '<span class="up-initial">' + initialChar + '</span>';
                    }
                }
                // Спрячем кнопки до загрузки (чтобы не мигали лишний раз)
                if ($('userModalActions')) $('userModalActions').classList.add('hidden');
                if ($('userModalMessageBtn')) $('userModalMessageBtn').classList.add('hidden');
                if ($('userModalSelfBtn')) $('userModalSelfBtn').classList.add('hidden');
                if ($('userModalStats')) $('userModalStats').classList.add('hidden');
                if ($('userModalJoined')) $('userModalJoined').classList.add('hidden');
                if ($('userModalLoader')) $('userModalLoader').classList.remove('hidden');

                openModal('userModal');

                try {
                    const r = await api('/api/user/' + encodeURIComponent(telegramId) + '/public');
                    if (!r.ok) {
                        // Юзер не найден в нашей БД (например, бота/админа без
                        // записи в users) — всё равно покажем что-то осмысленное.
                        if (r.status === 404 || (r.data && r.data.error === 'user_not_found')) {
                            if ($('userModalName')) $('userModalName').textContent = initialSrc;
                            if ($('userModalUsername')) $('userModalUsername').textContent = 'Профиль недоступен';
                            if ($('userModalLoader')) {
                                $('userModalLoader').innerHTML = '<span>Профиль этого пользователя пока недоступен.</span>';
                            }
                        } else {
                            if ($('userModalLoader')) {
                                $('userModalLoader').innerHTML = '<span>Не удалось загрузить профиль.</span>';
                            }
                        }
                        return;
                    }
                    const u = r.data.user || {};
                    const fullName = ((u.first_name || '') + (u.last_name ? (' ' + u.last_name) : '')).trim();
                    const displayName = fullName || (u.username ? '@' + u.username : ('id ' + telegramId));
                    const freshInitial = (String(displayName).replace(/^[@\s]+/, '').charAt(0) || '?').toUpperCase();
                    if ($('userModalInitial')) $('userModalInitial').textContent = freshInitial;
                    if ($('userModalName')) {
                        $('userModalName').innerHTML = escapeHtml(displayName) +
                            (u.is_admin ? ' <span class="user-profile-admin-badge">admin</span>' : '');
                    }
                    if ($('userModalUsername')) {
                        $('userModalUsername').textContent = u.username
                            ? '@' + u.username
                            : (u.is_self ? 'это вы' : 'telegram id ' + telegramId);
                    }
                    if ($('userModalAvatar')) {
                        if (u.photo_url) {
                            $('userModalAvatar').innerHTML = '<img src="' + escapeHtml(u.photo_url) + '" alt="" onerror="this.parentNode.textContent=\'' + freshInitial + '\'">';
                        } else {
                            $('userModalAvatar').innerHTML = '<span class="up-initial">' + freshInitial + '</span>';
                        }
                    }
                    if ($('userModalRating')) $('userModalRating').innerHTML = (Number(u.rating) || 0).toFixed(1) + '<span class="small">★</span>';
                    if ($('userModalReviews')) $('userModalReviews').textContent = String(u.reviews_count || 0);
                    if ($('userModalStats')) $('userModalStats').classList.remove('hidden');
                    if ($('userModalJoined')) {
                        if (u.created_at) {
                            $('userModalJoined').textContent = 'На платформе с ' + formatRegDate(u.created_at);
                            $('userModalJoined').classList.remove('hidden');
                        } else {
                            $('userModalJoined').classList.add('hidden');
                        }
                    }
                    if ($('userModalLoader')) $('userModalLoader').classList.add('hidden');

                    // Кнопки действий: «Написать» для чужих, «Открыть свой профиль» для себя.
                    if ($('userModalActions')) $('userModalActions').classList.remove('hidden');
                    if (u.is_self) {
                        if ($('userModalSelfBtn')) $('userModalSelfBtn').classList.remove('hidden');
                        if ($('userModalMessageBtn')) $('userModalMessageBtn').classList.add('hidden');
                    } else {
                        if ($('userModalMessageBtn')) {
                            $('userModalMessageBtn').classList.remove('hidden');
                            $('userModalMessageBtn').dataset.peerId = String(telegramId);
                            $('userModalMessageBtn').dataset.peerUsername = u.username || '';
                        }
                        if ($('userModalSelfBtn')) $('userModalSelfBtn').classList.add('hidden');
                    }
                } catch (e) {
                    if ($('userModalLoader')) {
                        $('userModalLoader').innerHTML = '<span>Сеть: ' + escapeHtml(e.message || 'ошибка') + '</span>';
                    }
                } finally {
                    _userProfileLoading = false;
                }
            }

            function renderProfileListingCards(container, items, showStatus) {
                if (!container) return;
                if (!items.length) {
                    container.innerHTML = '<div class="profile-listings-state">У пользователя пока нет объявлений</div>';
                    return;
                }
                const labels = { active: 'Активно', sold: 'Продано', cancelled: 'Снято' };
                container.innerHTML = '';
                items.forEach((it) => {
                    const status = it.status || 'active';
                    const interactive = status === 'active';
                    const card = document.createElement(interactive ? 'button' : 'article');
                    card.className = 'profile-listing-card';
                    if (interactive) card.type = 'button';
                    const meta = [it.country, it.origin_label || it.origin].filter(Boolean);
                    card.innerHTML =
                        '<div class="profile-listing-row">' +
                            '<div class="profile-listing-name">' + escapeHtml(it.title || 'Telegram-аккаунт') + '</div>' +
                            '<div class="profile-listing-price">' + formatPrice(it.price || 0) + ' ₽</div>' +
                        '</div>' +
                        '<div class="profile-listing-meta">' +
                            (showStatus ? '<span class="profile-listing-status ' + escapeHtml(status) + '">' + escapeHtml(labels[status] || status) + '</span>' : '') +
                            meta.map((value) => '<span>' + escapeHtml(value) + '</span>').join('') +
                        '</div>';
                    if (interactive) card.addEventListener('click', () => openItem(it));
                    container.appendChild(card);
                });
            }

            async function loadProfileListings() {
                const wrap = $('profileListings');
                const count = $('profileListingsCount');
                if (!wrap) return;
                wrap.innerHTML = '<div class="profile-listings-state">Загружаем о��ъявления…</div>';
                try {
                    const r = await api('/api/my_listings');
                    if (!r.ok) throw new Error('load_failed');
                    const me = state.tgUser || {};
                    const originIcons = { 'Авторег': '🤖', 'Саморег': '👤', 'Фишинг': '🎣', 'Стиллер': '🕵️' };
                    const items = (r.data.items || []).map((it) => ({
                        ...it,
                        origin_label: it.origin || 'Не указано',
                        origin_icon: originIcons[it.origin] || '•',
                        seller_id: Number(me.id || 0),
                        seller_username: me.username || '',
                        seller_full_name: [me.first_name, me.last_name].filter(Boolean).join(' '),
                    }));
                    if (count) count.textContent = String(items.length);
                    renderProfileListingCards(wrap, items, true);
                } catch (e) {
                    if (count) count.textContent = '0';
                    wrap.innerHTML = '<div class="profile-listings-state">Не удалось загрузить объявления</div>';
                }
            }

            let _userProfileReturnPage = 'pageCatalog';
            async function openUserProfile(telegramId, opts) {
                telegramId = Number(telegramId || 0);
                if (!telegramId) return;
                const myId = Number((state.tgUser && state.tgUser.id) || 0);
                if (telegramId === myId) {
                    openProfile();
                    return;
                }
                opts = opts || {};
                _userProfileReturnPage = state.currentPage && state.currentPage !== 'pageUserProfile'
                    ? state.currentPage : 'pageCatalog';
                const pre = opts.preload || {};
                const initialName = pre.name || (pre.username ? '@' + pre.username : 'Профиль');
                const initial = (String(initialName).replace(/^[@\s]+/, '').charAt(0) || '?').toUpperCase();
                $('userProfileName').textContent = initialName;
                $('userProfileUsername').textContent = pre.username ? '@' + pre.username : 'Загрузка…';
                $('userProfileAvatar').innerHTML = pre.photo_url
                    ? '<img src="' + escapeHtml(pre.photo_url) + '" alt="Аватар пользователя">'
                    : '<span>' + escapeHtml(initial) + '</span>';
                $('userProfileJoined').classList.add('hidden');
                $('userProfileRating').textContent = '—';
                $('userProfileReviews').textContent = '—';
                $('userProfileMessageBtn').classList.add('hidden');
                $('userProfileListingsCount').textContent = '0';
                $('userProfileListings').innerHTML = '<div class="profile-listings-state">Загружаем профиль…</div>';
                switchPage('pageUserProfile');
                try {
                    const r = await api('/api/user/' + encodeURIComponent(telegramId) + '/public');
                    if (!r.ok) throw new Error((r.data && r.data.error) || 'load_failed');
                    const u = r.data.user || {};
                    if (u.is_self) { openProfile(); return; }
                    const fullName = ((u.first_name || '') + (u.last_name ? ' ' + u.last_name : '')).trim();
                    const displayName = fullName || (u.username ? '@' + u.username : 'Пользователь');
                    const freshInitial = (displayName.replace(/^[@\s]+/, '').charAt(0) || '?').toUpperCase();
                    $('userProfileName').textContent = displayName;
                    $('userProfileUsername').textContent = u.username ? '@' + u.username : 'telegram id ' + telegramId;
                    $('userProfileAvatar').innerHTML = u.photo_url
                        ? '<img src="' + escapeHtml(u.photo_url) + '" alt="Аватар ' + escapeHtml(displayName) + '">'
                        : '<span>' + escapeHtml(freshInitial) + '</span>';
                    $('userProfileRating').textContent = (Number(u.rating) || 0).toFixed(1) + ' ★';
                    $('userProfileReviews').textContent = String(u.reviews_count || 0);
                    if (u.created_at) {
                        $('userProfileJoined').textContent = 'На платформе с ' + formatRegDate(u.created_at);
                        $('userProfileJoined').classList.remove('hidden');
                    }
                    const messageBtn = $('userProfileMessageBtn');
                    messageBtn.dataset.peerId = String(telegramId);
                    messageBtn.dataset.peerUsername = u.username || '';
                    messageBtn.classList.remove('hidden');
                    const listings = r.data.listings || [];
                    $('userProfileListingsCount').textContent = String(listings.length);
                    renderProfileListingCards($('userProfileListings'), listings, false);
                } catch (e) {
                    $('userProfileUsername').textContent = 'Профиль недоступен';
                    $('userProfileListings').innerHTML = '<div class="profile-listings-state">Не удалось загрузить профиль</div>';
                }
            }

            // Вспомогательное: «март 2024» из ISO-даты.
            function formatRegDate(iso) {
                try {
                    const months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
                                    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
                    const d = new Date(iso);
                    if (isNaN(d.getTime())) return '';
                    return months[d.getMonth()] + ' ' + d.getFullYear();
                } catch (e) { return ''; }
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
                // Параметр start для deeplink — бот получит deeplink-аргумент
                // и сам откроет нужный раздел пополнения.
                const startArg = 'deposit_' + method;
                let url;
                if (state.botUsername) {
                    url = 'https://t.me/' + state.botUsername + '?start=' + startArg;
                } else {
                    // fallback — прямая ссылка на бота (см. state.BOT_URL)
                    url = state.BOT_URL + '?start=' + startArg;
                }

                if (tg) {
                    if (tg.openTelegramLink) {
                        tg.openTelegramLink(url);
                    } else {
                        window.open(url, '_blank');
                    }
                    // Закрываем мини-апп, чтобы юзер оказался в боте, а не возвращался в апп
                    try { tg.close(); } catch (e) {}
                } else {
                    window.open(url, '_blank');
                }
                showToast('Открываем бота…', 'success');
            }

            // Прямой переход в бота по ссылке (для кнопок «Пополнить» и «Открыть бота»).
            // Закрываем мини-апп ср��зу после открытия deep-link, чтобы юзер
            // оказался в боте на экране пополнения, а не возвращался назад в апп.
            function openBotDirect(startArg) {
                const u = state.botUsername || 'testvestaccs_bot';
                const base = state.botUsername ? ('https://t.me/' + state.botUsername) : state.BOT_URL;
                const url = startArg ? (base + '?start=' + startArg) : base;

                if (tg) {
                    if (tg.openTelegramLink) {
                        tg.openTelegramLink(url);
                    } else {
                        window.open(url, '_blank');
                    }
                    try { tg.close(); } catch (e) {}
                } else {
                    window.open(url, '_blank');
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

            /* ===== Активация промокод�� прямо из профиля (общая БД с ботом) ===== */
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
                    showProfilePromoMsg('Ошибка сети. Попр��буйте ещё раз.', 'error');
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
                // Прямая ссылка на бота (см. state.BOT_URL) — не динамический getMe.
                openBotDirect();
            }

            /* ===== Отзыв (review modal) =====
               Состояние модалки живёт в reviewState. purchase_id приходит
               из кнопки чата (через [[BTN:open_review:<id>|...]]). Если id
               не передан — берём первую покупку, по которой ещё можно
               оставить отзыв (вызов /api/purchases/<id>/review). */
            const reviewState = {
                purchaseId: 0,
                submitting: false,
            };

            // Текстовая подсказка «N из 5» под звёздами.
            const REVIEW_RATING_LABELS = {
                1: '1 из 5 — ужасно',
                2: '2 из 5 — плохо',
                3: '3 из 5 — нормально',
                4: '4 из 5 — хорошо',
                5: '5 из 5 — отлично',
            };

            function paintReviewStars(value) {
                // value — 1..5. Красим «до N» жёлт��м.
                const wrap = dom.reviewStars;
                if (!wrap) return;
                const stars = wrap.querySelectorAll('.review-star');
                stars.forEach((el) => {
                    const v = parseInt(el.getAttribute('data-value'), 10);
                    if (v <= value) el.classList.add('filled');
                    else el.classList.remove('filled');
                });
                wrap.classList.add('has-filled');
                if (dom.reviewStarsLabel) {
                    dom.reviewStarsLabel.textContent = REVIEW_RATING_LABELS[value] || '';
                }
            }

            function openReviewModal(purchaseId) {
                reviewState.purchaseId = parseInt(purchaseId, 10) || 0;
                reviewState.submitting = false;
                if (dom.reviewMsg) {
                    dom.reviewMsg.textContent = '';
                    dom.reviewMsg.className = 'review-msg';
                }
                if (dom.reviewComment) dom.reviewComment.value = '';
                updateReviewCounter();
                // Дефолт — 5★ (последний radio помечен checked)
                paintReviewStars(5);
                openModal('reviewModal');
            }

            function updateReviewCounter() {
                if (!dom.reviewComment || !dom.reviewCounter) return;
                const len = (dom.reviewComment.value || '').length;
                dom.reviewCounter.textContent = len + ' / 1000';
            }

            async function submitReview() {
                if (reviewState.submitting) return;
                if (!dom.reviewStars) return;

                // Считываем выбранную звезду
                const checked = dom.reviewStars.querySelector(
                    'input[name="review-rating"]:checked'
                );
                const rating = checked ? parseInt(checked.value, 10) : 0;
                if (!(rating >= 1 && rating <= 5)) {
                    if (dom.reviewMsg) {
                        dom.reviewMsg.textContent = 'Поставьте оценку от 1 до 5';
                        dom.reviewMsg.className = 'review-msg error';
                    }
                    return;
                }

                if (!reviewState.purchaseId) {
                    // id не пришёл из чата — берём первую «отзывопригодную»
                    // покупку юзера. Подгружаем /api/purchases и перебираем.
                    if (dom.reviewMsg) {
                        dom.reviewMsg.textContent = 'Определяем покупку…';
                        dom.reviewMsg.className = 'review-msg';
                    }
                    try {
                        const r = await fetch('/api/purchases', {
                            headers: state.initData
                                ? { 'X-Init-Data': state.initData }
                                : {},
                        });
                        const j = await r.json();
                        if (j && j.ok && Array.isArray(j.items) && j.items.length) {
                            // Проверяем каждую покупку на can_review
                            for (const p of j.items) {
                                const rs = await fetch(
                                    '/api/purchases/' + p.id + '/review',
                                    { headers: state.initData
                                        ? { 'X-Init-Data': state.initData }
                                        : {} }
                                );
                                const rj = await rs.json();
                                if (rj && rj.ok && rj.can_review) {
                                    reviewState.purchaseId = p.id;
                                    break;
                                }
                            }
                        }
                    } catch (e) {
                        // сеть — оставим «не нашли»
                    }
                    if (!reviewState.purchaseId) {
                        if (dom.reviewMsg) {
                            dom.reviewMsg.textContent =
                                'Не нашли покупку для отзыва. Откройте «Мои покупки».';
                            dom.reviewMsg.className = 'review-msg error';
                        }
                        return;
                    }
                    if (dom.reviewMsg) {
                        dom.reviewMsg.textContent = '';
                        dom.reviewMsg.className = 'review-msg';
                    }
                }

                reviewState.submitting = true;
                if (dom.reviewSubmitBtn) {
                    dom.reviewSubmitBtn.disabled = true;
                    dom.reviewSubmitBtn.textContent = 'Отправляем…';
                }
                try {
                    const resp = await fetch('/api/reviews', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            ...(state.initData
                                ? { 'X-Init-Data': state.initData }
                                : {}),
                        },
                        body: JSON.stringify({
                            initData: state.initData || '',
                            purchase_id: reviewState.purchaseId,
                            rating: rating,
                            comment: (dom.reviewComment && dom.reviewComment.value) || '',
                        }),
                    });
                    const data = await resp.json().catch(() => ({}));
                    if (data && data.ok) {
                        if (dom.reviewMsg) {
                            dom.reviewMsg.textContent =
                                'Спасибо! Отзыв сохранён (' + data.rating + '/5)';
                            dom.reviewMsg.className = 'review-msg success';
                        }
                        // Через секунду закрываем модалку
                        setTimeout(() => {
                            try { closeModal('reviewModal'); } catch (e) {}
                            // Если открыт чат с продавцом — перезагрузим сообщения,
                            // чтобы юзер увидел благодарность от Vest Account.
                            if (
                                state.activeChatThreadId &&
                                typeof renderChatMessages === 'function'
                            ) {
                                renderChatMessages(false);
                            }
                            showToast('Отзыв сохранён', 'success');
                        }, 900);
                    } else {
                        const err = (data && (data.error || data.detail)) || 'unknown';
                        let human;
                        if (err === 'already_reviewed') {
                            human = 'Отзыв уже был оставлен';
                        } else if (err === 'review_window_closed') {
                            human = 'Срок для отзыва истёк (7 дней)';
                        } else if (err === 'not_your_purchase') {
                            human = 'Это не ваша покупка';
                        } else if (err === 'bad_rating') {
                            human = 'Поставьте оценку от 1 до 5';
                        } else if (err === 'purchase_not_found') {
                            human = 'Покупка не найдена';
                        } else {
                            human = 'Не удалось отправить отзыв';
                        }
                        if (dom.reviewMsg) {
                            dom.reviewMsg.textContent = human;
                            dom.reviewMsg.className = 'review-msg error';
                        }
                    }
                } catch (e) {
                    if (dom.reviewMsg) {
                        dom.reviewMsg.textContent = 'Ошибка сети';
                        dom.reviewMsg.className = 'review-msg error';
                    }
                } finally {
                    reviewState.submitting = false;
                    if (dom.reviewSubmitBtn) {
                        dom.reviewSubmitBtn.disabled = false;
                        dom.reviewSubmitBtn.textContent = 'Отправить отзыв';
                    }
                }
            }

            // «Пополнить» — сразу открывает бота (без страницы выбора метода),
            // так как пополнение делается в боте, а не в мини-аппе.
            function topupGoToBot() {
                openBotDirect('deposit');
                showToast('Открываем б��та для пополне��ия…', 'success');
            }

            /* ===== Bind events ===== */
            function bindEvents() {
                // Кнопка «Фильтры» — открывае�� модалку с гридами стран/происхождения/цены
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

                // Кнопка + рядом с балансом -> пополнение
                if (dom.headerTopupBtn) {
                    dom.headerTopupBtn.addEventListener('click', openTopup);
                }

                // Бургер: открыть/закрыть боковое меню
                dom.burgerBtn.addEventListener('click', () => {
                    const isOpen = dom.sideMenu.classList.contains('open');
                    if (isOpen) closeSideMenu();
                    else openSideMenu();
                });
                // Клик по заднему фону закрывает меню
                dom.sideMenuBackdrop.addEventListener('click', closeSideMenu);
                // Клик по пунктам бокового меню (страницы)
                document.querySelectorAll('.side-menu-item[data-page]').forEach((btn) => {
                    btn.addEventListener('click', () => {
                        const page = btn.dataset.page;
                        if (page === 'pageProfile') openProfile();
                        else switchPage(page);
                        closeSideMenu();
                    });
                });
                // Пункт «Чаты» из бокового меню
                dom.sideMenuChats.addEventListener('click', () => {
                    closeSideMenu();
                    switchPage('pageChats');
                });
                // Пункт «Помощь» из бокового меню
                dom.sideMenuSupport.addEventListener('click', () => {
                    closeSideMenu();
                    openSupport();
                });
                // Кнопка «Закрыть меню» внутри панели
                dom.sideMenuClose.addEventListener('click', closeSideMenu);

                // Profile back / topup
                dom.profileBack.addEventListener('click', () => switchPage('pageCatalog'));
                dom.topupBack.addEventListener('click', () => switchPage('pageProfile'));
                dom.purchasesBack.addEventListener('click', () => switchPage('pageCatalog'));
                const sellBack = document.getElementById('sellBack');
                if (sellBack) sellBack.addEventListener('click', () => switchPage('pageCatalog'));
                const chatsBack = document.getElementById('chatsBack');
                if (chatsBack) chatsBack.addEventListener('click', () => switchPage('pageCatalog'));
                bindSellEvents();
                bindChatEvents();
                startChatsPolling();
                refreshUnreadBadge();
                if (dom.codeModalRefresh) {
                    dom.codeModalRefresh.addEventListener('click', () => {
                        const pid = purchasesState.codePurchaseId;
                        if (pid) fetchPurchaseCode(pid);
                    });
                }
                dom.topupBtn.addEventListener('click', topupGoToBot);
                dom.openTopup2.addEventListener('click', topupGoToBot);
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
                // Кнопка на странице пополнения (если кто-то туда попал) — сразу в бота
                const topupGoBotBtn = document.getElementById('topupGoBotBtn');
                if (topupGoBotBtn) {
                    topupGoBotBtn.addEventListener('click', () => {
                        openBotDirect('deposit');
                        showToast('Открываем бота…', 'success');
                    });
                }

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
                    const url = state.SUPPORT_URL;
                    if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
                    else window.open(url, '_blank');
                });

                // ===== Review modal (отзыв) =====
                // Клик по «звезде» — выставляем radio + подсветка
                if (dom.reviewStars) {
                    dom.reviewStars.querySelectorAll('.review-star').forEach((el) => {
                        el.addEventListener('click', () => {
                            const v = parseInt(el.getAttribute('data-value'), 10);
                            if (!Number.isFinite(v) || v < 1 || v > 5) return;
                            // Программно «чекнем» соответствующий radio,
                            // чтобы change в DOM прошёл естественно.
                            const input = dom.reviewStars.querySelector(
                                'input[name="review-rating"][value="' + v + '"]'
                            );
                            if (input) input.checked = true;
                            paintReviewStars(v);
                        });
                    });
                }
                if (dom.reviewComment) {
                    dom.reviewComment.addEventListener('input', updateReviewCounter);
                }
                if (dom.reviewSubmitBtn) {
                    dom.reviewSubmitBtn.addEventListener('click', submitReview);
                }
                // При закрытии модалки — сбрасываем состояние, чтобы
                // повторное открытие стартовало с чистого листа.
                document.querySelectorAll('[data-close="reviewModal"]').forEach((b) => {
                    b.addEventListener('click', () => {
                        reviewState.purchaseId = 0;
                        reviewState.submitting = false;
                        if (dom.reviewMsg) {
                            dom.reviewMsg.textContent = '';
                            dom.reviewMsg.className = 'review-msg';
                        }
                        if (dom.reviewComment) dom.reviewComment.value = '';
                        updateReviewCounter();
                        paintReviewStars(5);
                    });
                });

                // Кнопка «Купить» (этап 1: валидация)
                if (dom.buyBtn) {
                    dom.buyBtn.addEventListener('click', buyCurrentItem);
                }
                // Кнопка «Купить (подтвердить)» (этап 2: реальная покупка)
                if (dom.buyConfirmBtn) {
                    dom.buyConfirmBtn.addEventListener('click', confirmBuyCurrentItem);
                }
                // Кнопка «Написать продавцу» в модалке товара
                if (dom.itemChatBtn) {
                    dom.itemChatBtn.addEventListener('click', () => {
                        const sid = Number(dom.itemChatBtn.dataset.sellerId || 0);
                        const uname = dom.itemChatBtn.dataset.sellerUsername || '';
                        if (!sid) return;
                        closeModal('itemModal');
                        openChatByPeerId(sid, { username: uname });
                    });
                }

                // ====== Клик по аватару/имени продавца В МОДАЛКЕ ТОВАРА ======
                // Открывает публичный профиль продавца. Элементы создаются
                // динамически в openItem(), поэтому вешаем обработчик один раз
                // через делегирование на самой модалке #itemModal.
                const itemModal = document.getElementById('itemModal');
                if (itemModal && !itemModal.__sellerProfileBound) {
                    itemModal.__sellerProfileBound = true;
                    itemModal.addEventListener('click', (e) => {
                        const target = e.target.closest(
                            '.item-seller-avatar[data-seller-id], ' +
                            '.item-seller-info[data-seller-id]'
                        );
                        if (!target) return;
                        e.preventDefault();
                        e.stopPropagation();
                        const sid = Number(target.dataset.sellerId || 0);
                        if (!sid) return;
                        target.classList.add('is-clicked');
                        setTimeout(() => target.classList.remove('is-clicked'), 180);
                        // Снимаем «текущий» лот, чтобы прелоад-данные продавца
                        // пришли из buyState.item (последнего открытого).
                        const it = (typeof buyState !== 'undefined' && buyState.item) || null;
                        const preload = it ? {
                            name: (it.seller_full_name || '').trim() || (it.seller_username ? '@' + it.seller_username : ''),
                            username: it.seller_username || '',
                            photo_url: it.seller_photo_url || null,
                        } : null;
                        openUserProfile(sid, { preload: preload });
                    });
                }

                // ====== Клик по аватару/имени в МОДАЛКЕ ЧАТА ======
                // Открывает публичный профиль собеседник��, не закрывая чат.
                // (Пользователь может вернуться кнопкой «Закрыть».)
                const chatModalEl = document.getElementById('chatModal');
                if (chatModalEl && !chatModalEl.__peerProfileBound) {
                    chatModalEl.__peerProfileBound = true;
                    chatModalEl.addEventListener('click', (e) => {
                        // Кнопка закрытия модалки должна работать, как и раньше
                        if (e.target.closest('[data-close]')) return;
                        const target = e.target.closest(
                            '#chatModalAvatar[data-peer-id], ' +
                            '#chatModalName[data-peer-id], ' +
                            '#chatModalSub[data-peer-id]'
                        );
                        if (!target) return;
                        e.preventDefault();
                        e.stopPropagation();
                        const pid = Number(target.dataset.peerId || 0);
                        if (!pid) return;
                        target.classList.add('is-clicked');
                        setTimeout(() => target.classList.remove('is-clicked'), 180);
                        const photoUrl = (chatsState && chatsState.peerPhotos && chatsState.peerPhotos[String(pid)]) || null;
                        // Берём прелоад прямо из data-* атрибутов (они
                        // проставляются в openChat), а username — с запасом
                        // из chatsState.activeMessages[0].from_user (если есть).
                        let firstName = target.dataset.peerFirstName || '';
                        let username   = target.dataset.peerUsername   || '';
                        if ((!firstName && !username) && chatsState && chatsState.items) {
                            const ci = chatsState.items.find((x) => Number(x.peer_id) === pid);
                            if (ci) {
                                firstName = ci.peer_first_name || '';
                                username  = ci.peer_username   || '';
                            }
                        }
                        const name = firstName || (username ? '@' + username : ('id ' + pid));
                        openUserProfile(pid, {
                            preload: {
                                name: name,
                                username: username,
                                photo_url: photoUrl,
                            },
                        });
                    });
                }

                const userProfileBack = document.getElementById('userProfileBack');
                if (userProfileBack) {
                    userProfileBack.addEventListener('click', () => switchPage(_userProfileReturnPage || 'pageCatalog'));
                }
                const userProfileMessageBtn = document.getElementById('userProfileMessageBtn');
                if (userProfileMessageBtn) {
                    userProfileMessageBtn.addEventListener('click', () => {
                        const pid = Number(userProfileMessageBtn.dataset.peerId || 0);
                        if (!pid) return;
                        openChatByPeerId(pid, { username: userProfileMessageBtn.dataset.peerUsername || '' });
                    });
                }

                // ====== Кнопки в модалке профиля чужого юзера ======
                //  — «Написать сообщение» — открывает/создаёт чат с этим юзером
                //  — «Это вы — открыть свой профиль» — переключает на pageProfile
                // Привязываем один раз, чтобы не плодить обработчики.
                const userModalEl = document.getElementById('userModal');
                if (userModalEl && !userModalEl.__buttonsBound) {
                    userModalEl.__buttonsBound = true;
                    const msgBtn = document.getElementById('userModalMessageBtn');
                    if (msgBtn) {
                        msgBtn.addEventListener('click', () => {
                            const pid = Number(msgBtn.dataset.peerId || 0);
                            if (!pid) return;
                            const uname = msgBtn.dataset.peerUsername || '';
                            closeModal('userModal');
                            // Используем уже существующий openChatByPeerId —
                            // он подтянет/создаст чат и откро��т модалку диалога.
                            openChatByPeerId(pid, { username: uname });
                        });
                    }
                    const selfBtn = document.getElementById('userModalSelfBtn');
                    if (selfBtn) {
                        selfBtn.addEventListener('click', () => {
                            closeModal('userModal');
                            openProfile();
                        });
                    }
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
                        else if (state.currentPage === 'pageUserProfile') switchPage(_userProfileReturnPage || 'pageCatalog');
                        else if (state.currentPage === 'pageProfile') switchPage('pageCatalog');
                        else if (state.currentPage === 'pageSell') switchPage('pageCatalog');
                        else if (state.currentPage === 'pagePurchases') switchPage('pageCatalog');
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

                // Рефреш каталога каждые 5 секунд пока апппа видима.
                // Нужен, чтобы в мини-аппе были видны изменения из бота:
                //  — кто-то только что купил аккаунт (is_sold=True),
                //  — продавец выставил новое объявление,
                //  — у существующего listing поменялись title/description.
                // Опрос идёт тихим fetch без лоадера, чтобы не моргал UI.
                setInterval(() => {
                    if (document.visibilityState !== 'visible') return;
                    if (state.currentPage !== 'pageCatalog') return;
                    // Не дёргаем каталог, если юзер прямо ��ейчас что-то покупает
                    if (sellState && sellState.busy) return;
                    silentRefreshCatalog();
                }, 5000);
            }

            // Тихий рефреш каталога — без лоадера, без мигания UI и без сброса скролла.
            // Сравниваем список id+price+sold-флагов с текущим: если ничего не поменялось,
            // вообще не трогаем DOM.
            async function silentRefreshCatalog() {
                const params = new URLSearchParams();
                if (state.country && state.country !== 'all') params.set('country', state.country);
                if (state.origin && state.origin !== 'all') params.set('origin', state.origin);
                if (state.priceSort && state.priceSort !== 'default') params.set('sort', state.priceSort);
                // Фильтр по дате создания аккаунта (от и до) — дублируем с loadCatalog(),
                // чтобы фоновый поллинг каталога тоже у��итывал выб��анный диапазон.
                if (state.createdFromMonth && state.createdFromMonth !== 'all') params.set('from_month', state.createdFromMonth);
                if (state.createdFromYear  && state.createdFromYear  !== 'all') params.set('from_year',  state.createdFromYear);
                if (state.createdToMonth   && state.createdToMonth   !== 'all') params.set('to_month',   state.createdToMonth);
                if (state.createdToYear    && state.createdToYear    !== 'all') params.set('to_year',    state.createdToYear);
                params.set('limit', '100');

                try {
                    const r = await api('/api/catalog?' + params.toString());
                    if (!r.ok || !r.data || !r.data.items) return;
                    const freshItems = r.data.items;

                    // Лёгкий fingerprint: id|price|country
                    const sig = freshItems.map((it) => `${it.id}:${it.price}:${it.country}`).join('|');
                    if (state.catalogSig === sig) return; // ничего не изменилось

                    state.catalog = freshItems;
                    state.catalogSig = sig;
                    if (dom.catListCount) dom.catListCount.textContent = freshItems.length;
                    renderCatalog();
                } catch (e) {
                    // Тихо игнорим — следующий тик через 5 сек
                }
            }

            async function bootstrap() {
                initTheme();
                bindThemeToggle();
                renderUser();
                bindEvents();
                try {
                    await auth();
                    // Ошибка одного запроса больше не оставляет ��есь интерфейс в loading.
                    await Promise.allSettled([loadCategories(), loadCatalog()]);
                    updateFilterSummary();
                    api('/api/bot-info').then((r) => {
                        if (r.ok && r.data.username) state.botUsername = r.data.username;
                    });
                } catch (error) {
                    showLoader(false);
                    renderLoadFailure('Приложение не смогло запуститься. Попробуйте ещё раз.');
                }
            }

            function startApp() {
                bootstrap().catch(() => {
                    showLoader(false);
                    renderLoadFailure('Приложение не смогло запуститься. Попроб��йте ещё раз.');
                });
            }

            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', startApp, { once: true });
            } else {
                startApp();
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
            # В схеме bot.py нет колонок last_name / photo_url,
            # поэтому last_name добавили миграцией. first_name и
            # last_name — чтобы карточка товара показывала ПОЛНОЕ имя
            # продавца, а не только first_name.
            db_user = User(
                telegram_id=tg_id,
                username=tg_user.get("username"),
                first_name=tg_user.get("first_name"),
                last_name=tg_user.get("last_name"),
            )
            session.add(db_user)
        else:
            # Обновляем только те поля, которые есть в реальной схеме.
            new_username = tg_user.get("username")
            if new_username and new_username != db_user.username:
                db_user.username = new_username
            # Обновим first_name, если он появился / поменялся
            new_first = tg_user.get("first_name")
            if new_first and new_first != db_user.first_name:
                db_user.first_name = new_first
            # Обновим last_name, если он появился / поменялся
            new_last = tg_user.get("last_name")
            if new_last and new_last != getattr(db_user, "last_name", None):
                db_user.last_name = new_last
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
            # Поля — строго по реальной схеме (см. bot.py). last_name —
            # через миграцию, чтобы можно было собрать полное имя в карточке.
            db_user = User(
                telegram_id=telegram_id,
                username=tg_user.get("username"),
                first_name=tg_user.get("first_name"),
                last_name=tg_user.get("last_name"),
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
# Ид��нтично логике из bot.py: список покупок юзера + выдача
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


@app.route("/api/purchases/<int:purchase_id>/review", methods=["GET"])
@require_auth
def api_purchase_review_status(telegram_id, tg_user, purchase_id):
    """Статус отзыва по конкретной ��окупке.

    Возвращает:
      - exists: True, если отзыв уже оставлен (вручную или авто)
      - rating, comment, created_at, is_auto — поля существующего отзыва
      - can_review: True, если покупатель ещё МОЖЕТ оставить отзыв
        (покупка его, отзыва ещё нет, прошло < REVIEW_AUTO_POST_DAYS дн��й)
      - days_left: сколько целых дней осталось до авто-отзыва
        (None, если отзыв уже есть или время вышло)
      - auto_in_review: True, если время вышло и авто-отзыв уже должен
        был быть выставлен (но ещё не выставлен — скоро подхватит планировщик)

    Используется фронтом, чтобы показать/скрыть кнопку «Оставить отзыв»
    в карточке покупки и в системном сообщении чата.
    """
    session = SessionLocal()
    try:
        purchase = session.execute(
            select(Purchase).where(Purchase.id == purchase_id)
        ).scalar_one_or_none()
        if not purchase:
            return jsonify({"ok": False, "error": "purchase_not_found"}), 404
        if purchase.user_id != telegram_id:
            return jsonify({"ok": False, "error": "not_your_purchase"}), 403

        existing = session.execute(
            select(Review).where(Review.purchase_id == purchase_id)
        ).scalar_one_or_none()

        if existing:
            return jsonify({
                "ok": True,
                "exists": True,
                "rating": int(existing.rating or 0),
                "comment": (existing.comment or ""),
                "created_at": existing.created_at.isoformat() if existing.created_at else None,
                "is_auto": bool(getattr(existing, "is_auto", False)),
                "can_review": False,
                "days_left": None,
                "auto_in_review": False,
            })

        # Отзыва ещё нет — считаем, сколько дней осталось до авто-выставления.
        from datetime import datetime as _dt
        now = _dt.utcnow()
        purchased_at = purchase.created_at or now
        deadline = purchased_at + timedelta(days=REVIEW_AUTO_POST_DAYS)
        seconds_left = (deadline - now).total_seconds()
        days_left = max(0, int(seconds_left // 86400))
        # «просрочено» = время вышло, но авто-отзыв ещё не появился
        # (планировщик подхватит на ближайшем тике). После дедлайна
        # ручной отзыв больше не принимается — чтобы не было гонки
        # с авто-планировщиком и «задним числом».
        auto_in_review = seconds_left <= 0

        return jsonify({
            "ok": True,
            "exists": False,
            "rating": None,
            "comment": None,
            "created_at": None,
            "is_auto": False,
            "can_review": not auto_in_review,
            "days_left": days_left,
            "auto_in_review": auto_in_review,
        })
    finally:
        session.close()


@app.route("/api/reviews", methods=["POST"])
@require_auth
def api_post_review(telegram_id, tg_user):
    """Оставить отзыв (1..5 звёзд, опционально комментарий) по покупке.

    Тело запроса:
      { "purchase_id": <int>, "rating": <1..5>, "comment": "<text>" }

    Правила:
      - покупка должна принадлежать текущему юзеру
      - отзыв можно оставить ТОЛЬКО пока не истёк REVIEW_AUTO_POST_DAYS
        (после дедлайна ручной отзыв больше не принимается — там уже
        отрабатывает авто-планировщик; гонок нет: даже если планировщик
        уже вставил авто-5, ручной POST вернёт 409 "already_reviewed")
      - один отзыв на покупку
      - при успехе пересчитываем рейтинг продавца (users.rating и
        users.reviews_count) и постим в чат бот-сообщение «Спасибо
        за отзыв!».
    """
    payload = request.get_json(silent=True) or {}
    try:
        purchase_id = int(payload.get("purchase_id") or 0)
    except (TypeError, ValueError):
        purchase_id = 0
    if purchase_id <= 0:
        return jsonify({"ok": False, "error": "bad_purchase_id"}), 400

    try:
        rating = int(payload.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    if rating < 1 or rating > 5:
        return jsonify({"ok": False, "error": "bad_rating"}), 400

    comment = (payload.get("comment") or "").strip()
    if len(comment) > 1000:
        # Защита от мусорных простыней в чате продавца
        comment = comment[:1000]

    session = SessionLocal()
    try:
        purchase = session.execute(
            select(Purchase).where(Purchase.id == purchase_id)
        ).scalar_one_or_none()
        if not purchase:
            return jsonify({"ok": False, "error": "purchase_not_found"}), 404
        if purchase.user_id != telegram_id:
            return jsonify({"ok": False, "error": "not_your_purchase"}), 403

        # Уже есть отзыв? (в т.ч. автоматический)
        existing = session.execute(
            select(Review).where(Review.purchase_id == purchase_id)
        ).scalar_one_or_none()
        if existing:
            return jsonify({"ok": False, "error": "already_reviewed"}), 409

        # Проверяем дедлайн — после REVIEW_AUTO_POST_DAYS ручной отзыв
        # больше не принимается (там уже отработал авто-планировщик).
        from datetime import datetime as _dt
        purchased_at = purchase.created_at or _dt.utcnow()
        deadline = purchased_at + timedelta(days=REVIEW_AUTO_POST_DAYS)
        if _dt.utcnow() > deadline:
            return jsonify({
                "ok": False,
                "error": "review_window_closed",
                "detail": "Срок для отзыва истёк",
            }), 403

        # Убеждаемся, что у покупки есть продавец (без продавца отзыв
        # оценивать нечего).
        account = session.execute(
            select(Account).where(Account.id == purchase.account_id)
        ).scalar_one_or_none()
        if not account or not account.seller_id:
            return jsonify({"ok": False, "error": "no_seller"}), 400

        # listing_id в Review — NOT NULL. Если у покупки нет listing_id,
        # ищ��м любой листинг по э��ому аккаунту (как в api_buy) — без
        # этого INSERT упадёт с NOT NULL violation.
        listing_id = purchase.listing_id
        if not listing_id:
            linked_listing = session.execute(
                select(Listing)
                .where(Listing.account_id == purchase.account_id)
                .order_by(Listing.created_at.desc())
            ).scalars().first()
            if not linked_listing:
                # Крайний случай — создаём синтетический листинг-«sold»,
                # чтобы Review-строка могла сослаться. Безопасно: цена
                # реальная, бот та��ие тоже умеет читать.
                linked_listing = Listing(
                    seller_id=int(account.seller_id),
                    account_id=account.id,
                    title=f"Покупка #{purchase.id}",
                    description="",
                    price=float(purchase.amount or 0.0),
                    origin=account.origin,
                    country=account.country,
                    status="sold",
                    buyer_id=int(telegram_id),
                    created_at=purchased_at,
                    sold_at=purchased_at,
                )
                session.add(linked_listing)
                session.flush()
            listing_id = linked_listing.id

        # ===== Пишем отзыв =====
        review = Review(
            seller_id=int(account.seller_id),
            buyer_id=int(telegram_id),
            listing_id=int(listing_id),
            purchase_id=int(purchase_id),
            rating=int(rating),
            comment=comment,
            created_at=_dt.utcnow(),
        )
        # Если в таблице есть колонка is_auto (миграция ниже) — заполняем
        # False. Если колонки нет (старая БД) — этот атрибут просто
        # проигнорируется ORM-ом, потому что в модели Review он
        # определён как default=False.
        if hasattr(review, "is_auto"):
            review.is_auto = False
        session.add(review)

        # ===== Пересчитываем рейтинг продавца (честно, через COUNT/AVG) =====
        # Почему не инкрементально: default у User — rating=5.0,
        # reviews_count=0. Если у продавца ещё нет ни одного отзыва,
        # инкрементальный пересчёт (old_rating * old_count + new) даёт
        # мусор (5.0 * 0 + 4) / 1 = 9.0. Агрегация по таблице — всегда
        # точна и не зависит от того, насколько рассинхронизированы
        # счётчики. Дёшево — COUNT/AVG по индексу seller_id.
        seller = session.execute(
            select(User).where(User.telegram_id == account.seller_id)
            .with_for_update()
        ).scalar_one_or_none()
        if seller is not None:
            stats = session.execute(
                select(func.count(Review.id), func.avg(Review.rating))
                .where(Review.seller_id == account.seller_id)
            ).one()
            total_count = int(stats[0] or 0)
            avg_rating = float(stats[1] or 0.0)
            seller.reviews_count = total_count
            seller.rating = round(avg_rating, 2)

        # ===== Постим в чат бот-сообщение «Спасибо за отзыв» =====
        try:
            a_id, b_id = sorted([int(telegram_id), int(account.seller_id)])
            thread = session.execute(
                select(ChatThread).where(
                    ChatThread.user1_id == a_id,
                    ChatThread.user2_id == b_id,
                )
            ).scalar_one_or_none()
            if thread is not None:
                stars = "⭐" * int(rating)
                thanks_text = (
                    f"{stars}\n\n"
                    f"Спасибо за отзыв {rating}/5"
                    + (f"!\n💬 «{comment}»" if comment else "!")
                )
                _insert_bot_message(
                    session,
                    thread.id,
                    thanks_text,
                    purchase_id=purchase.id,
                )
        except Exception as _e_chat:
            # Чат — вспомогательная фича, не валим отзыв из-за чата.
            try:
                app.logger.warning("Failed to post review-thanks chat msg: %s", _e_chat)
            except Exception:
                pass

        session.commit()

        return jsonify({
            "ok": True,
            "review_id": review.id,
            "rating": int(rating),
            "comment": comment,
            "is_auto": False,
        })
    except IntegrityError as _ie:
        try:
            session.rollback()
        except Exception:
            pass
        return jsonify({
            "ok": False,
            "error": "integrity_error",
            "detail": str(_ie)[:200],
        }), 500
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "server_error", "detail": str(e)[:200]}), 500
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
                        "Либо скачайт�� .session файл и войдите вручную.",
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
                first_name=tg_user.get("first_name"),
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

    # Фильтр по дате создания аккаунта (от и до: месяц + год).
    # Принимаем только годы 2013..2026 и месяцы 1..12 — остальное игнорируем,
    # чтобы не делать кривых SQL-запросов.
    def _parse_int(name, lo, hi):
        v = request.args.get(name)
        if v in (None, "", "all"):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        if n < lo or n > hi:
            return None
        return n

    from_month = _parse_int("from_month", 1, 12)
    from_year  = _parse_int("from_year",  2013, 2026)
    to_month   = _parse_int("to_month",   1, 12)
    to_year    = _parse_int("to_year",    2013, 2026)

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

        # Границы диапазона даты создания.
        # from_date — пер��ое число выбранного месяца (или 1 января, если месяц не указан).
        # to_date   — первое число СЛЕДУЮЩЕГО месяца (исключительно), чтобы весь
        # последний выбранный месяц попал в выборку.
        from datetime import datetime as _dt
        if from_year is not None:
            fm = from_month or 1
            from_date = _dt(from_year, fm, 1)
            q = q.where(Account.created_at >= from_date)
        if to_year is not None:
            tm = to_month or 12
            if tm == 12:
                # следующий месяц — январь следующего года
                to_date_excl = _dt(to_year + 1, 1, 1)
            else:
                to_date_excl = _dt(to_year, tm + 1, 1)
            q = q.where(Account.created_at < to_date_excl)

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
            # Описание для фронта: «Регистрация: <месяц> <год>» + свободный
            # текст продавца. Покупатель видит «Регистрация: Июль 2021» в
            # карточке объявления. reg_month / reg_year лежат на Account
            # (там они и сохраняются).
            raw_desc = listing.description if listing and listing.description else None
            # Аватарка продавца — реальная, из Telegram (через Bot API),
            # с кешированием на 6 часов. Если продавца нет / нет фото —
            # фронт нарисует фоллбек с первой буквой имени.
            try:
                seller_photo_url = _get_telegram_photo_url(int(a.seller_id)) if a.seller_id else None
            except Exception:
                seller_photo_url = None
            items.append({
                "id": a.id,
                "country": a.country,
                "flag": COUNTRY_FLAGS.get(a.country, "🌍"),
                "price": float(a.price or 0),
                "origin": origin_key,
                "origin_icon": icon,
                "origin_label": label,
                # preview (с маской +***XXXX) больше НЕ отдаём в карточ��у —
                # последние 4 цифры номера в открытом виде не нужны покупателю.
                # Полный номер он получит после оплаты (страница покупок).
                "created_at": a.created_at.isoformat() if a.created_at else None,
                # продавец
                "seller_id": a.seller_id,
                "seller_username": seller.username if seller else None,
                "seller_first_name": (seller.first_name if seller else None),
                "seller_last_name": (getattr(seller, "last_name", None) if seller else None),
                "seller_full_name": _compose_full_name(
                    getattr(seller, "first_name", None) if seller else None,
                    getattr(seller, "last_name", None) if seller else None,
                ),
                "seller_photo_url": seller_photo_url,
                "seller_rating": float(seller.rating or 5.0) if seller else 5.0,
                "seller_reviews": int(seller.reviews_count or 0) if seller else 0,
                # описание — теперь без префикса даты регистрации
                "description": build_listing_description(
                    raw_desc, a.reg_month, a.reg_year
                ),
                "title": (listing.title if listing and listing.title else None),
                # Дата регистрации — отдельным полем (для фильтров и
                # для блока метаданных рядом с «Сессия верифицирована»).
                "reg_month": a.reg_month,
                "reg_year": a.reg_year,
                "reg_text": format_reg_info(a.reg_month, a.reg_year) or None,
                # Флаг верификации сессии. Каталог и так фильтрует только
                # верифицирован��ые аккаунты, но фронту удобно иметь явный
                # булеан для бейджа «Сессия верифицирована».
                "is_verified": bool(getattr(a, "is_verified", False)),
                # ====== TELEGRAM PREMIUM ======
                # True, если у аккаунта активна платная подписка. Фронт
                # рисует зелёный бейдж «Телеграмм премиум» на карточке.
                "has_premium": bool(getattr(a, "has_premium", False)),
            })
        return jsonify({"ok": True, "items": items, "count": len(items)})
    finally:
        session.close()


async def _validate_account_async(session_string: str) -> dict:
    """Проверяет валидность аккаунта: подключается к Telegram, проверяет
    авторизацию и делает простой запрос (getMe) чтобы убедиться что аккаунт
    живой и не имеет критических ошибок.

    Возвращает:
        {"ok": True, "valid": True}          — аккаунт живой
        {"ok": True, "valid": False, "reason": "..."} — аккаунт невалиден
        {"ok": False, "error": "..."}        — ошибка соединения
    """
    if not session_string or len(session_string) < 50:
        return {"ok": True, "valid": False, "reason": "empty_session"}

    client = None
    try:
        sess = StringSession(session_string)
        client = TelegramClient(sess, API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            return {"ok": True, "valid": False, "reason": "not_authorized"}

        # Делаем реальный запрос — getMe
        me = await client.get_me()
        if me is None:
            return {"ok": True, "valid": False, "reason": "get_me_failed"}

        return {"ok": True, "valid": True}

    except telethon_errors.AuthKeyUnregisteredError:
        return {"ok": True, "valid": False, "reason": "auth_key_unregistered"}
    except telethon_errors.UserDeactivatedError:
        return {"ok": True, "valid": False, "reason": "user_deactivated"}
    except telethon_errors.UserDeactivatedBanError:
        return {"ok": True, "valid": False, "reason": "account_banned"}
    except Exception as e:
        err_str = str(e)[:120]
        # Если явная ошибка авторизации — помечаем невалидным
        auth_err_keywords = ["auth", "unauthorized", "deactivated", "banned", "flood", "session"]
        if any(kw in err_str.lower() for kw in auth_err_keywords):
            return {"ok": True, "valid": False, "reason": f"auth_error: {err_str}"}
        return {"ok": False, "error": f"connection_error: {err_str}"}
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


def _validate_account(session_string: str) -> dict:
    """Sync-обёртка для _validate_account_async."""
    try:
        return _run_async(_validate_account_async(session_string), timeout=60)
    except Exception as e:
        return {"ok": False, "error": f"run_error: {str(e)[:100]}"}


@app.route("/api/validate_account", methods=["POST"])
@require_auth
def api_validate_account(telegram_id, tg_user):
    """Проверяет валидность аккаунта перед покупкой.

    Принимает JSON: {"account_id": <int>}
    Если аккаунт невалиден — снимает объявление с продажи (listing.status='cancelled').
    Возвращает: {"ok": True, "valid": True/False, "reason": "..."}
    """
    payload = request.get_json(silent=True) or {}
    try:
        account_id = int(payload.get("account_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad_account_id"}), 400

    if account_id <= 0:
        return jsonify({"ok": False, "error": "missing_account_id"}), 400

    db_sess = SessionLocal()
    try:
        account = db_sess.execute(
            select(Account).where(Account.id == account_id)
        ).scalar_one_or_none()

        if not account:
            return jsonify({"ok": False, "error": "not_found"}), 404

        if account.is_sold:
            return jsonify({"ok": False, "error": "already_sold"}), 409

        session_string = account.session_string
        if not session_string:
            # Нет сессии — снимаем объявление
            listing = db_sess.execute(
                select(Listing).where(
                    Listing.account_id == account_id,
                    Listing.status == "active"
                )
            ).scalar_one_or_none()
            if listing:
                listing.status = "cancelled"
                db_sess.commit()
            return jsonify({"ok": True, "valid": False, "reason": "no_session"}), 200
    finally:
        db_sess.close()

    # Запускаем валидацию (блокирующий вызов до 60 сек)
    result = _validate_account(session_string)

    if not result.get("ok"):
        # Ошибка соединения — не снимаем объявление, сообщаем о сбое
        return jsonify({
            "ok": False,
            "error": result.get("error", "connection_failed"),
        }), 200

    if not result.get("valid"):
        # Аккаунт невалиден — снимаем объявление
        db_sess2 = SessionLocal()
        try:
            listing = db_sess2.execute(
                select(Listing).where(
                    Listing.account_id == account_id,
                    Listing.status == "active"
                )
            ).scalar_one_or_none()
            if listing:
                listing.status = "cancelled"
                db_sess2.commit()
        finally:
            db_sess2.close()

    return jsonify({
        "ok": True,
        "valid": result.get("valid", False),
        "reason": result.get("reason", ""),
        "account_id": account_id,
    })


@app.route("/api/buy", methods=["POST"])
@require_auth
def api_buy(telegram_id, tg_user):
    """
    Реальная покупка аккаунта.
    Схема покупки — как в bot.py: списываем с баланса, помечаем аккаунт
    проданным, создаём Purchase. Продавцу зачисляем в hold (как в боте).

    ⚠️ FIX «ложный already_sold»:
    Раньше блок `except IntegrityError → "already_sold"` ловил ЛЮБОЙ
    IntegrityError и возвращал "уже продан". Это вводило юзера в
    заблуждение: реальная причина могла быть в гонке при INSERT users
    (UNIQUE telegram_id), или в сбое создания Hold/ChatThread — а юзер
    видел «аккаунт уже продан», хотя аккаунт был свободен.

    Теперь:
      1) User создаём через INSERT ... ON CONFLICT DO NOTHING — без гонок
         на UNIQUE telegram_id.
      2) Любой IntegrityError логируем с деталями, а перед ответом делаем
         повторный SELECT Account.is_sold: если в БД аккаунт реально
         свободен (is_sold=False) — возвращаем "server_error" с типом
         "integrity_error" (НЕ "already_sold"). Если в БД уже продан
         (is_sold=True) — это и есть легитимный already_sold.
    """
    payload = request.get_json(silent=True) or {}
    try:
        account_id = int(payload.get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    if account_id <= 0:
        return jsonify({"ok": False, "error": "bad_account_id"}), 400

    session = SessionLocal()
    # Флаг для блока except — была ли IntegrityError
    integrity_failure = {"detail": None}
    try:
        # Блокируем аккаунт (FOR UPDATE), чтобы при ��онках один и тот же
        # аккаунт не купили дважды. Блокировка живёт до COMMIT/ROLLBACK.
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

        # ===== Безопасное создание/получение User =====
        # Раньше тут был `select ... with_for_update()` + `if not buyer:
        # INSERT`. Но FOR UPDATE блокирует только СУЩЕСТВУЮЩИЕ строки, а
        # если User ещё нет — блокировки нет, и при гонке двух запросов
        # (например, юзер быстро жмёт «Купить» дважды, или фронт
        # ретраит) обе транзакции проходят select→None и обе пытаются
        # INSERT. Одна из них получает UNIQUE (users.telegram_id) →
        # IntegrityError → ложный "already_sold".
        #
        # Решение: UPSERT через ON CONFLICT DO NOTHING. Если User уже
        # есть — ничего не делаем; если нет — создаём со всеми дефолтами
        # схемы. Дефолты подобраны так, чтобы совпадали с моделью User
        # и не сломать bot.py (он читает те же поля).
        try:
            session.execute(
                text(
                    """
                    INSERT INTO users (
                        telegram_id, username, balance, hold_balance,
                        total_spent, total_earned, is_admin, rating,
                        reviews_count, created_at
                    )
                    VALUES (
                        :tid, :un, 0, 0, 0, 0, false, 5.0, 0, NOW()
                    )
                    ON CONFLICT (telegram_id) DO NOTHING
                    """
                ),
                {"tid": int(telegram_id), "un": tg_user.get("username")},
            )
        except Exception as _e_user:
            # Если вдруг ON CONFLICT не сработал (старая версия PG без
            # поддержки, или схема отличается) — откатываем и возвращаем
            # явную ошибку, НЕ маскируем её под "already_sold".
            integrity_failure["detail"] = f"user_upsert_failed: {_e_user}"
            raise IntegrityError("user_upsert_failed", {}, _e_user)

        # Теперь User точно существует — читаем с FOR UPDATE для блокировки баланса
        buyer = session.execute(
            select(User).where(User.telegram_id == telegram_id)
            .with_for_update()
        ).scalar_one_or_none()
        if not buyer:
            # Теоретически не должно случиться после UPSERT, но на всякий
            # случай — отдаём server_error, а не already_sold.
            return jsonify({
                "ok": False,
                "error": "buyer_missing",
                "detail": "User-запись не найдена после upsert",
            }), 500

        if float(buyer.balance or 0.0) < price:
            return jsonify({"ok": False, "error": "low_balance"}), 402

        # Списываем с баланса покупателя
        buyer.balance = float(buyer.balance or 0.0) - price
        buyer.total_spent = float(buyer.total_spent or 0.0) + price

        # Помечаем аккаунт проданным
        account.is_sold = True

        # На всякий случай — гасим ВСЕ listings по этому аккаунту (вдруг
        # гонка с ботом успела создать listing после нашей проверки выше).
        # FOR UPDATE на listing не нужен — мы уже держим блокировку аккаунта.
        for L in session.execute(
            select(Listing).where(
                Listing.account_id == account.id,
                Listing.status == "active",
            )
        ).scalars().all():
            L.status = "cancelled"

        # Создаём Purchase (схема как в bot.py)
        purchase = Purchase(
            user_id=telegram_id,
            account_id=account.id,
            listing_id=None,  # покупка напрямую из каталога (не из P2P-объявления)
            amount=price,
            payment_method="balance",
        )
        session.add(purchase)
        # ⚠️ FIX «integrity_error при покупке»:
        # Раньше дальше создавался Hold с listing_id=None и purchase_id=None,
        # и только ПОТОМ делался session.flush(). В PostgreSQL Hold.listing_id
        # и Hold.purchase_id — NOT NULL, и без ForeignKey в SQLAlchemy ORM не
        # знает, что нужно сначала зафлашить Purchase, чтобы получить id.
        # В итоге INSERT Hold падал с NOT NULL violation → IntegrityError,
        # который раньше маскировался под «already_sold», а после прошлого
        # фикса стал честно возвращаться как «integrity_error».
        # Теперь сначала флашим Purchase, получаем purchase.id, и Hold
        # создаём с реальным purchase_id.
        session.flush()  # получаем purchase.id

        # Если есть продавец — кладём выручку в его hold (по аналогии с ботом).
        # Применяем 7%-комиссию как в bot.py: в hold кладём net_amount (93%),
        # а разница (commission) фиксируется в Hold-записи и удерживается при
        # релизе. Через HOLD_PERIOD_HOURS фоновый scheduler переведёт net_amount
        # на основной баланс и напишет в чат «Деньги зачислены продавцу».
        commission = round(price * COMMISSION_PERCENT / 100.0, 2)
        net_amount = round(price - commission, 2)
        new_hold = None
        if account.seller_id:
            seller = session.execute(
                select(User).where(User.telegram_id == account.seller_id)
                .with_for_update()
            ).scalar_one_or_none()
            if seller:
                seller.hold_balance = float(seller.hold_balance or 0.0) + net_amount
                seller.total_earned = float(seller.total_earned or 0.0) + net_amount
                # Находим любой Listing для этого аккаунта — даже cancelled
                # или sold. Hold.listing_id — NOT NULL, поэтому нужно
                # передать реальный id существующего листинга.
                # Если по какой-то причине у аккаунта вообще нет листингов
                # (например, аккаунт попал в БД мимо бота), создаём
                # синтетический «sold»-листинг прямо сейчас — это
                # безопасно, бот умеет с такими работать.
                linked_listing = session.execute(
                    select(Listing)
                    .where(Listing.account_id == account.id)
                    .order_by(Listing.created_at.desc())
                ).scalars().first()
                if not linked_listing:
                    linked_listing = Listing(
                        seller_id=int(account.seller_id),
                        account_id=account.id,
                        title=f"Покупка #{purchase.id}",
                        description="",
                        price=float(price),
                        origin=account.origin,
                        country=account.country,
                        status="sold",
                        buyer_id=int(telegram_id),
                        created_at=datetime.utcnow(),
                        sold_at=datetime.utcnow(),
                    )
                    session.add(linked_listing)
                    session.flush()  # нужен linked_listing.id

                # Создаём Hold-запись — её подхватит фоновый scheduler
                # ровно через HOLD_PERIOD_HOURS и переведёт деньги продавцу.
                new_hold = Hold(
                    seller_id=int(account.seller_id),
                    listing_id=int(linked_listing.id),
                    purchase_id=int(purchase.id),
                    gross_amount=float(price),
                    commission=float(commission),
                    net_amount=float(net_amount),
                    status="hold",
                    created_at=datetime.utcnow(),
                    release_at=datetime.utcnow() + timedelta(hours=HOLD_PERIOD_HOURS),
                )
                session.add(new_hold)

        # ===== Авто-создание чата с продавцом (FunPay-стиль) =====
        # Если у аккаунта есть продавец и покупатель ≠ продавец — создаём
        # (или переиспользуем) диалог между ними и кладём туда системное
        # сообщение о покупке ОТ ЛИЦА ВЕСТ АККАУНТ БОТА (sender_id = 0).
        # Это «отдельное лицо»: фронт рисует такие сообщения с аватаркой
        # из репозитория и именем «Vest Account», а не как сообщение от
        # покупателя или продавца. Под сообщением — кнопка «Открыть спор»,
        # которая ведёт в поддержку. Через 24 часа бот допишет сюда же
        # «Деньги зачислены продавцу».
        chat_thread_id = None
        if account.seller_id and int(account.seller_id) != int(telegram_id):
            try:
                a_id, b_id = sorted([int(telegram_id), int(account.seller_id)])
                thread = session.execute(
                    select(ChatThread).where(
                        ChatThread.user1_id == a_id,
                        ChatThread.user2_id == b_id,
                    )
                ).scalar_one_or_none()
                if not thread:
                    thread = ChatThread(
                        user1_id=a_id,
                        user2_id=b_id,
                        last_message_at=datetime.utcnow(),
                    )
                    session.add(thread)
                    session.flush()  # нужен thread.id
                chat_thread_id = thread.id

                # Текст системного сообщения — как карточка заказа на FunPay.
                # В конце — маркеры кнопок:
                #   [[BTN:open_dispute|⚠️ Открыть спор]]     — открыть спор
                #   [[BTN:open_review:<purchase_id>|⭐ Оставить отзыв]] — оставить
                #     отзыв (1..5 ★ + комментарий). Если за REVIEW_AUTO_POST_DAYS
                #     дней отзыв не оставлен — фоновый планировщик выставит
                #     5★ автоматически.
                # Фронт вырежет маркеры из текста и отрендерит настоящие кнопки
                # под пузырьком. sender_id = BOT_SENDER_ID — это «Vest Account».
                # Тег <b> снят — текст рендерится чистым, без жирных выделений.
                sys_text = (
                    f"🛒 Покупка #{purchase.id}\n\n"
                    f"📦 Аккаунт: {account.phone}\n"
                    f"🌍 Страна: {account.country or '—'}\n"
                    f"💰 Сумма: {price:.0f}₽\n"
                    f"💳 Оплата: с баланса\n"
                    f"🕓 Холд продавца: {HOLD_PERIOD_HOURS} ч "
                    f"(зачисление после проверки)\n\n"
                    f"Откройте «Мои покупки», чтобы получить данные аккаунта. "
                    f"Если что-то не так — откройте спор. "
                    f"Вам доступен отзыв (1–5★) — {REVIEW_AUTO_POST_DAYS} дней.\n\n"
                    f"[[BTN:open_dispute|⚠️ Открыть спор]]\n"
                    f"[[BTN:open_review:{int(purchase.id)}|⭐ Оставить отзыв]]"
                )
                _insert_bot_message(
                    session,
                    thread.id,
                    sys_text,
                    purchase_id=purchase.id,
                )
            except Exception as e:
                # Чат — вспомогательная фича. Если что-то пошло не так
                # (гонка, нехватка таблицы и т.п.) — НЕ вал��м покупку,
                # только логируем.
                app.logger.warning("Failed to create post-purchase chat: %s", e)

        session.commit()

        return jsonify({
            "ok": True,
            "purchase_id": purchase.id,
            "account_id": account.id,
            "amount": price,
            "balance": float(buyer.balance or 0.0),
            "hold_balance": float(buyer.hold_balance or 0.0),
            "chat_thread_id": chat_thread_id,
            "seller_id": int(account.seller_id) if account.seller_id else None,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })
    except IntegrityError as _ie:
        # ⚠️ FIX: Раньше здесь возвращался "already_sold" для ЛЮБОЙ
        # IntegrityError. Теперь — корректная диагностика:
        #   1) Откатываем транзакцию.
        #   2) Перечитываем Account в НОВОЙ сессии (старая убита rollback'ом).
        #   3) Если аккаунт реально продан (is_sold=True) — честно
        #      возвращаем "already_sold" (это легитимный случай: между
        #      нашими проверками кто-то купил аккаунт через бот ил��
        #      параллельную сессию).
        #   4) Если аккаунт НЕ продан — это была другая IntegrityError
        #      (например, гонка на UNIQUE chat_threads или сбой Hold),
        #      возвращаем "server_error" с типом "integrity_error" и
        #      деталями. Юзер НЕ видит ложного «уже продан».
        try:
            session.rollback()
        except Exception:
            pass
        # Проверяем реальное состояние аккаунта после rollback
        try:
            verify_session = SessionLocal()
            try:
                actual = verify_session.execute(
                    select(Account).where(Account.id == account_id)
                ).scalar_one_or_none()
                if actual and actual.is_sold:
                    return jsonify({"ok": False, "error": "already_sold"}), 409
                # Аккаунт свободен — значит IntegrityError был НЕ из-за продажи
                detail_msg = str(_ie)[:200] if str(_ie) else "unknown"
                app.logger.error(
                    "api_buy IntegrityError (NOT is_sold): account_id=%s, telegram_id=%s, detail=%s",
                    account_id, telegram_id, detail_msg,
                )
                return jsonify({
                    "ok": False,
                    "error": "integrity_error",
                    "detail": detail_msg,
                }), 500
            finally:
                verify_session.close()
        except Exception as _verify_err:
            app.logger.error("api_buy post-rollback verify failed: %s", _verify_err)
            return jsonify({
                "ok": False,
                "error": "server_error",
                "detail": str(_verify_err)[:200],
            }), 500
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


@app.route("/api/chats")
@require_auth
def api_chats_list(telegram_id, tg_user):
    """Список чатов текущего пользователя.

    Возвращает диалоги, где он — у��астник (user1_id ИЛИ user2_id),
    с превью последнего сообщения, ником собеседника и счётчиком непрочитанных.
    Сортировка — свежие сверху (по last_message_at).
    """
    session = SessionLocal()
    try:
        # 1) все thread-ы, где я участник
        threads = session.execute(
            select(ChatThread).where(
                (ChatThread.user1_id == telegram_id) | (ChatThread.user2_id == telegram_id)
            ).order_by(ChatThread.last_message_at.desc())
        ).scalars().all()

        if not threads:
            return jsonify({"ok": True, "chats": [], "unread_total": 0})

        thread_ids = [t.id for t in threads]
        # 2) последние сообщения (по одному на thread — берём MAX(id))
        last_msg_subq = (
            select(
                ChatMessage.thread_id.label("tid"),
                func.max(ChatMessage.id).label("max_id"),
            )
            .where(ChatMessage.thread_id.in_(thread_ids))
            .group_by(ChatMessage.thread_id)
            .subquery()
        )
        last_msg_rows = session.execute(
            select(ChatMessage).join(
                last_msg_subq, ChatMessage.id == last_msg_subq.c.max_id
            )
        ).scalars().all()
        last_msg_by_tid = {m.thread_id: m for m in last_msg_rows}

        # 3) непрочитанные: sender_id != me AND read_at IS NULL
        unread_counts = {}
        unread_rows = session.execute(
            select(
                ChatMessage.thread_id,
                func.count(ChatMessage.id),
            )
            .where(
                ChatMessage.thread_id.in_(thread_ids),
                ChatMessage.sender_id != telegram_id,
                ChatMessage.read_at.is_(None),
            )
            .group_by(ChatMessage.thread_id)
        ).all()
        for tid, cnt in unread_rows:
            unread_counts[tid] = int(cnt or 0)

        # 4) пользователи-собеседники одним за��росом
        peer_ids = set()
        for t in threads:
            peer_ids.add(t.user1_id if t.user2_id == telegram_id else t.user2_id)
        peer_users = {}
        if peer_ids:
            rows = session.execute(
                select(User).where(User.telegram_id.in_(peer_ids))
            ).scalars().all()
            peer_users = {u.telegram_id: u for u in rows}

        chats = []
        for t in threads:
            peer_id = t.user1_id if t.user2_id == telegram_id else t.user2_id
            peer = peer_users.get(peer_id)
            lm = last_msg_by_tid.get(t.id)
            chats.append({
                "thread_id": t.id,
                "peer_id": peer_id,
                "peer_username": peer.username if peer else None,
                "peer_first_name": (peer.first_name if peer else None),
                "last_message": (lm.text[:140] if lm else None),
                "last_message_at": (lm.created_at.isoformat() if lm and lm.created_at
                                    else t.last_message_at.isoformat()),
                "last_message_sender_id": lm.sender_id if lm else None,
                "unread": unread_counts.get(t.id, 0),
            })

        unread_total = sum(unread_counts.values())
        return jsonify({"ok": True, "chats": chats, "unread_total": unread_total})
    finally:
        session.close()


@app.route("/api/chats/unread_count")
@require_auth
def api_chats_unread_count(telegram_id, tg_user):
    """Только число непрочитанных — для бейджа в боковом меню."""
    session = SessionLocal()
    try:
        thread_subq = (
            select(ChatThread.id)
            .where((ChatThread.user1_id == telegram_id) | (ChatThread.user2_id == telegram_id))
            .subquery()
        )
        cnt = session.execute(
            select(func.count(ChatMessage.id))
            .where(
                ChatMessage.thread_id.in_(select(thread_subq.c.id)),
                ChatMessage.sender_id != telegram_id,
                ChatMessage.read_at.is_(None),
            )
        ).scalar_one_or_none()
        return jsonify({"ok": True, "unread": int(cnt or 0)})
    finally:
        session.close()


@app.route("/api/user/<int:user_telegram_id>/avatar")
@require_auth
def api_user_avatar(user_telegram_id, telegram_id, tg_user):
    """Возвращает {photo_url: <абсолютный url>|null} для аватарки
    пользователя. Кешируется в памяти процесса (см. _TG_PHOTO_CACHE).

    Используется фронтом, чтобы подтягивать реальные фото собеседников
    в списке чатов и в модалке диалога, не устраивая N запросов к Bot API
    при каждом открытии списка.
    """
    try:
        url = _get_telegram_photo_url(user_telegram_id)
    except Exception:
        url = None
    return jsonify({"ok": True, "photo_url": url})


@app.route("/api/user/<int:user_telegram_id>/public")
@require_auth
def api_user_public(user_telegram_id, telegram_id, tg_user):
    """Публичный профиль пользователя (для открытия по клику на ник/аватар).

    Возвращает только ту информацию, которую можно показывать другим
    пользователям: имя, username, аватар, рейтинг, число отзывов,
    флаг админа и дату регистрации. Баланс / hold / total_spent /
    total_earned / email / phone сюда НЕ попадают — это личные данные,
    и доступ к ним имеет только сам пользователь через /api/me.

    Параметры:
      user_telegram_id  — telegram_id запрашиваемого поль��ователя.

    Поля ответа:
      ok                — bool
      user.telegram_id  — int
      user.username     — str | null
      user.first_name   — str | null
      user.last_name    — str | null
      user.photo_url    — str | null  (абсолютный URL из Telegram Bot API)
      user.rating       — float
      user.reviews_count — int
      user.is_admin     — bool
      user.created_at   — iso8601 | null
      user.is_self      — bool (True, если это сам запрашивающий)
    """
    session = SessionLocal()
    try:
        u = session.execute(
            select(User).where(User.telegram_id == user_telegram_id)
        ).scalar_one_or_none()
        if not u:
            return jsonify({"ok": False, "error": "user_not_found"}), 404
        # Аватарку тянем через ту же функцию, что и /api/user/.../avatar,
        # чтобы задействовать общий _TG_PHOTO_CACHE (не дублируем запросы).
        try:
            photo_url = _get_telegram_photo_url(int(u.telegram_id))
        except Exception:
            photo_url = None
        public_listings = session.execute(
            select(Listing, Account)
            .join(Account, Account.id == Listing.account_id)
            .where(
                Listing.seller_id == user_telegram_id,
                Listing.status == "active",
                Account.is_sold.is_(False),
            )
            .order_by(Listing.created_at.desc())
            .limit(50)
        ).all()
        seller_full_name = " ".join(filter(None, [
            getattr(u, "first_name", None),
            getattr(u, "last_name", None),
        ])).strip()
        listings_payload = []
        month_names = {
            1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
            5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
            9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
        }
        for listing, account in public_listings:
            reg_parts = []
            if account.reg_month in month_names:
                reg_parts.append(month_names[account.reg_month])
            if account.reg_year:
                reg_parts.append(str(account.reg_year))
            reg_text = ("Регистрация: " + " ".join(reg_parts)) if reg_parts else ""
            origin_key = listing.origin or account.origin or "Авторег"
            origin_icon, origin_label = ORIGIN_LABELS.get(origin_key, ("•", origin_key))
            listings_payload.append({
                "id": int(listing.id),
                "title": listing.title or "Telegram-аккаунт",
                "description": build_listing_description(
                    listing.description or "", account.reg_month, account.reg_year
                ) or "",
                "price": float(listing.price or 0),
                "origin": origin_key,
                "origin_label": origin_label,
                "origin_icon": origin_icon,
                "country": listing.country or account.country or "",
                "flag": COUNTRY_FLAGS.get(listing.country or account.country or "", ""),
                "status": "active",
                "created_at": listing.created_at.isoformat() if listing.created_at else None,
                "is_verified": bool(account.is_verified),
                "has_premium": bool(account.has_premium),
                "reg_month": account.reg_month,
                "reg_year": account.reg_year,
                "reg_text": reg_text or "",
                "seller_id": int(u.telegram_id),
                "seller_username": u.username,
                "seller_full_name": seller_full_name,
                "seller_photo_url": photo_url,
                "seller_rating": float(u.rating or 5.0),
                "seller_reviews": int(u.reviews_count or 0),
            })
        return jsonify({
            "ok": True,
            "user": {
                "telegram_id": int(u.telegram_id),
                "username": u.username,
                "first_name": getattr(u, "first_name", None),
                "last_name": getattr(u, "last_name", None),
                "photo_url": photo_url,
                "rating": float(u.rating or 5.0),
                "reviews_count": int(u.reviews_count or 0),
                "is_admin": bool(u.is_admin),
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "is_self": int(u.telegram_id) == int(telegram_id),
            },
            "listings": listings_payload,
        })
    finally:
        session.close()


@app.route("/api/chats/start", methods=["POST"])
@require_auth
def api_chats_start(telegram_id, tg_user):
    """Создать (если ещё нет) диалог с пользователем peer_id.

    Тело: {"peer_id": <telegram_id>}. Возвращает thread_id.
    Нельзя создать чат с самим собой.
    """
    payload = request.get_json(silent=True) or {}
    try:
        peer_id = int(payload.get("peer_id") or 0)
    except (TypeError, ValueError):
        peer_id = 0
    if peer_id <= 0:
        return jsonify({"ok": False, "error": "peer_id_required"}), 400
    if peer_id == telegram_id:
        return jsonify({"ok": False, "error": "cannot_chat_with_self"}), 400

    session = SessionLocal()
    try:
        a, b = sorted([int(telegram_id), int(peer_id)])
        existing = session.execute(
            select(ChatThread).where(
                ChatThread.user1_id == a, ChatThread.user2_id == b
            )
        ).scalar_one_or_none()
        if existing:
            return jsonify({"ok": True, "thread_id": existing.id, "created": False})
        t = ChatThread(user1_id=a, user2_id=b, last_message_at=datetime.utcnow())
        session.add(t)
        session.commit()
        return jsonify({"ok": True, "thread_id": t.id, "created": True})
    finally:
        session.close()


def _resolve_thread(session, telegram_id: int, peer_id: int):
    """Находит thread между (telegram_id, peer_id) или возвращает None."""
    a, b = sorted([int(telegram_id), int(peer_id)])
    return session.execute(
        select(ChatThread).where(
            ChatThread.user1_id == a, ChatThread.user2_id == b
        )
    ).scalar_one_or_none()


@app.route("/api/chats/<int:peer_id>/messages", methods=["GET"])
@require_auth
def api_chats_get_messages(peer_id, telegram_id, tg_user):
    """Список сообщений в диалоге с peer_id (старые → новые)."""
    if peer_id == telegram_id:
        return jsonify({"ok": False, "error": "cannot_chat_with_self"}), 400
    session = SessionLocal()
    try:
        thread = _resolve_thread(session, telegram_id, peer_id)
        if not thread:
            return jsonify({"ok": True, "thread_id": None, "messages": [], "peer_id": peer_id})
        msgs = session.execute(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread.id)
            .order_by(ChatMessage.id.asc())
        ).scalars().all()
        out = [{
            "id": m.id,
            "sender_id": m.sender_id,
            "text": m.text,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "read_at": m.read_at.isoformat() if m.read_at else None,
            "mine": m.sender_id == telegram_id,
        } for m in msgs]
        return jsonify({
            "ok": True,
            "thread_id": thread.id,
            "peer_id": peer_id,
            "messages": out,
        })
    finally:
        session.close()


@app.route("/api/chats/<int:peer_id>/messages", methods=["POST"])
@require_auth
def api_chats_send_message(peer_id, telegram_id, tg_user):
    """Отправить сообщение в диалог с peer_id.

    Если диалога ещё нет — создаём автоматически.
    Тело: {"text": "..."}.
    """
    if peer_id == telegram_id:
        return jsonify({"ok": False, "error": "cannot_chat_with_self"}), 400
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty_message"}), 400
    if len(text) > 4000:
        text = text[:4000]

    session = SessionLocal()
    try:
        thread = _resolve_thread(session, telegram_id, peer_id)
        if not thread:
            a, b = sorted([int(telegram_id), int(peer_id)])
            thread = ChatThread(user1_id=a, user2_id=b, last_message_at=datetime.utcnow())
            session.add(thread)
            session.flush()  # нужен thread.id
        now = datetime.utcnow()
        msg = ChatMessage(
            thread_id=thread.id,
            sender_id=telegram_id,
            text=text,
            created_at=now,
            read_at=None,
        )
        session.add(msg)
        thread.last_message_at = now
        session.commit()
        return jsonify({
            "ok": True,
            "message": {
                "id": msg.id,
                "sender_id": msg.sender_id,
                "text": msg.text,
                "created_at": msg.created_at.isoformat(),
                "read_at": None,
                "mine": True,
            },
            "thread_id": thread.id,
        })
    finally:
        session.close()


@app.route("/api/chats/<int:peer_id>/read", methods=["POST"])
@require_auth
def api_chats_mark_read(peer_id, telegram_id, tg_user):
    """Пометить ВСЕ входящие сообщения от peer_id как прочитанные."""
    if peer_id == telegram_id:
        return jsonify({"ok": False, "error": "cannot_chat_with_self"}), 400
    session = SessionLocal()
    try:
        thread = _resolve_thread(session, telegram_id, peer_id)
        if not thread:
            return jsonify({"ok": True, "marked": 0})
        now = datetime.utcnow()
        # UPDATE ... WHERE thread_id=? AND sender_id=peer AND read_at IS NULL
        rows = session.execute(
            select(ChatMessage).where(
                ChatMessage.thread_id == thread.id,
                ChatMessage.sender_id == peer_id,
                ChatMessage.read_at.is_(None),
            )
        ).scalars().all()
        for m in rows:
            m.read_at = now
        session.commit()
        return jsonify({"ok": True, "marked": len(rows)})
    finally:
        session.close()


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "ts": datetime.utcnow().isoformat(),
        "bot_username": get_bot_username(),
    })


# ===== АВАТАРКА VEST ACCOUNT BOT =====
@app.route("/api/bot_avatar")
def api_bot_avatar():
    """Отдаёт PNG-аватарку бота из репозитория.

    Файл лежит рядом с app.py (Gemini_Generated_Image_w0v6n4w0v6n4w0v6.png).
    Кешируем на сутки — аватар меняться не должен, а на каждый чат-рендер
    браузер ломиться в сеть не должен.
    """
    try:
        if not _BOT_AVATAR_PATH.exists():
            abort(404)
        resp = send_file(
            str(_BOT_AVATAR_PATH),
            mimetype="image/png",
            as_attachment=False,
            download_name="vest_bot_avatar.png",
        )
        # 1 день кеша — аватар статичный
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["Content-Type"] = "image/png"
        return resp
    except Exception:
        abort(404)


# ===== ХЕЛПЕР: ВСТАВИТЬ СООБЩЕНИЕ ОТ «VEST ACCOUNT» В ЧАТ =====
def _insert_bot_message(session, thread_id: int, text: str, purchase_id: int = None) -> int:
    """Создаёт запись ChatMessage от имени бота (sender_id = BOT_SENDER_ID)
    в ука��анном thread-е и обновляет last_message_at потока.

    Возвращает id созданного сообщения.
    """
    now = datetime.utcnow()
    msg = ChatMessage(
        thread_id=thread_id,
        sender_id=BOT_SENDER_ID,
        text=text,
        created_at=now,
        read_at=None,
    )
    session.add(msg)
    session.flush()  # нужен msg.id для thread-а
    thread = session.get(ChatThread, thread_id)
    if thread is not None:
        thread.last_message_at = now
    return msg.id


# ===== ФОНОВЫЙ РЕЛИЗ ХОЛДОВ (24 ЧАСА) =====
#
# По правилам P2P-маркетплейса деньг�� продавца лежат в hold_balance 24 часа
# после продажи. Через 24 часа бот (здесь — фоновый поток мини-аппа)
# переводит net_amount на основной баланс продавца и пишет в чат между
# покупателем и продавцом сообщение «Деньги зачислены продавцу» от
# лица Vest Account. Атомарность обеспечивается условием status='hold'
# в UPDATE — если bot.py уже отрелизил, мы не отработаем повторно.
_HOLD_LOOP_STARTED = False
_HOLD_LOOP_LOCK = threading.Lock()


def _release_due_holds_sync() -> int:
    """Синхронная часть релиза — ровно то, что раньше делал bot.py
    (release_due_holds), но через общую SessionLocal и без asyncio.
    Возвращает количество отрелизенных холдов за этот тик.
    """
    now = datetime.utcnow()
    released = 0
    session = SessionLocal()
    try:
        # Берём все due-холды под блокировкой (SELECT ... FOR UPDATE SKIP LOCKED),
        # чтобы при гонке с bot.py второй воркер не отрабатывал повторно.
        try:
            due_holds = session.execute(
                select(Hold)
                .where(
                    Hold.status == "hold",
                    Hold.release_at <= now,
                )
                .with_for_update(skip_locked=True)
            ).scalars().all()
        except Exception:
            # SQLite / старые версии SQLAlchemy не поддерживают SKIP LOCKED —
            # в этом случае лочим обычным FOR UPDATE, гонка редкая.
            due_holds = session.execute(
                select(Hold)
                .where(
                    Hold.status == "hold",
                    Hold.release_at <= now,
                )
                .with_for_update()
            ).scalars().all()

        for hold in due_holds:
            try:
                seller = session.execute(
                    select(User).where(User.telegram_id == hold.seller_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if not seller:
                    # Продавец пропал — просто закрываем холд, чтобы не крутилось вечно
                    hold.status = "cancelled"
                    hold.released_at = now
                    continue

                # Двигаем деньги: hold_balance -> balance
                if (seller.hold_balance or 0) < hold.net_amount:
                    seller.hold_balance = max(0.0, float(seller.hold_balance or 0.0))
                else:
                    seller.hold_balance = float(seller.hold_balance or 0.0) - float(hold.net_amount or 0.0)
                seller.balance = float(seller.balance or 0.0) + float(hold.net_amount or 0.0)
                seller.total_earned = float(seller.total_earned or 0.0) + float(hold.net_amount or 0.0)

                hold.status = "released"
                hold.released_at = now
                released += 1

                # Ищем чат между покупателем и продавцом. Thread создаётся в api_buy,
                # но на случай рассинхрона ищем заново по паре id.
                buyer_id = None
                purchase = session.get(Purchase, hold.purchase_id) if hold.purchase_id else None
                if purchase is not None:
                    buyer_id = purchase.user_id
                if buyer_id and int(buyer_id) != int(hold.seller_id):
                    a, b = sorted([int(buyer_id), int(hold.seller_id)])
                    thread = session.execute(
                        select(ChatThread).where(
                            ChatThread.user1_id == a,
                            ChatThread.user2_id == b,
                        )
                    ).scalar_one_or_none()
                    if thread is not None:
                        # Без <b> — чистый текст, как просили в ТЗ.
                        bot_text = (
                            f"💸 Деньги зачислены продавцу!\n\n"
                            f"Холд 24 часа истёк. "
                            f"Сумма {float(hold.net_amount or 0):.0f}₽ "
                            f"(комиссия {float(hold.commission or 0):.0f}₽) переведена продавцу.\n\n"
                            f"Если у вас остались вопросы по сделке — откройте спор."
                        )
                        _insert_bot_message(session, thread.id, bot_text, purchase_id=hold.purchase_id)
            except Exception as e_hold:
                # Один холд не должен валить всю пачку
                try:
                    app.logger.warning("release hold %s failed: %s", getattr(hold, "id", "?"), e_hold)
                except Exception:
                    pass

        session.commit()
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        try:
            app.logger.error("hold_releaser error: %s", e)
        except Exception:
            pass
    finally:
        session.close()
    return released


def _hold_releaser_loop():
    """Фоновый поток: раз в HOLD_RELEASE_CHECK_INTERVAL секунд зовёт
    _release_due_holds_sync(). Daemon=True — не блокируем выключение."""
    while True:
        try:
            _release_due_holds_sync()
        except Exception as e:
            try:
                app.logger.warning("hold_releaser tick failed: %s", e)
            except Exception:
                pass
        time.sleep(HOLD_RELEASE_CHECK_INTERVAL)


def _start_hold_releaser_once():
    """Запускает фоновый цикл релиза холдов ровно один раз за процесс."""
    global _HOLD_LOOP_STARTED
    with _HOLD_LOOP_LOCK:
        if _HOLD_LOOP_STARTED:
            return
        t = threading.Thread(target=_hold_releaser_loop, name="hold-releaser", daemon=True)
        t.start()
        _HOLD_LOOP_STARTED = True


# На Vercel фоновые потоки нельзя запускать при импорте: serverless-инстанс
# может быть заморожен сразу после ответа. Релиз холдов должен вызываться
# планировщиком/ботом, а не блокировать жизненный цикл web-приложения.


# ===== ФОНОВЫЙ АВТО-ОТЗЫВ (через REVIEW_AUTO_POST_DAYS дней) =====
#
# Зачем: покупатель получил аккаунт, всё ок — но отзыв не оставил.
# Через REVIEW_AUTO_POST_DAYS дней фоновый планировщик выставляет
# автоматический 5★-отзыв от его лица, пересчитывает рейтинг продавца
# �� пишет в чат системное сообщение «Отзыв выставлен автоматически».
#
# Безопасность:
#   - Берём только Purchase �� которых created_at <= NOW() - REVIEW_AUTO_POST_DAYS
#   - и для которых ещё НЕТ Review (LEFT JOIN ... WHERE reviews.id IS NULL)
#   - SELECT ... FOR UPDATE SKIP LOCKED, чтобы при гонке с bot.py
#     (если бот когда-то тоже научится) не было дублей.
#   - Если listing_id у покупки NULL — подбираем существующий или
#     создаём синтетический, чтобы Review-строка вписалась без NOT NULL
#     violation.
#   - Один неудачный отзыв НЕ валит всю пачку.
_AUTO_REVIEW_LOOP_STARTED = False
_AUTO_REVIEW_LOCK = threading.Lock()


def _post_auto_reviews_sync() -> int:
    """Синхронный проход: выставляет 5★-отзывы по просроченным покупкам.

    Возвращает количество выставленных авто-отзывов за этот тик.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=REVIEW_AUTO_POST_DAYS)
    posted = 0
    session = SessionLocal()
    try:
        # Кандидаты — покупки старше REVIEW_AUTO_POST_DAYS, у которых
        # ещё нет отзыва. Делаем это одним SQL-запросом с NOT EXISTS,
        # а не двумя (список + проверка) — меньше гонок.
        try:
            candidates = session.execute(
                select(Purchase)
                .where(
                    Purchase.created_at <= cutoff,
                    ~select(Review.id).where(Review.purchase_id == Purchase.id).exists(),
                )
                .with_for_update(skip_locked=True)
                .limit(50)  # за тик — не больше 50, чтобы не залипнуть
            ).scalars().all()
        except Exception:
            # SQLite / старые версии SQLAlchemy не умеют SKIP LOCKED.
            candidates = session.execute(
                select(Purchase)
                .where(
                    Purchase.created_at <= cutoff,
                    ~select(Review.id).where(Review.purchase_id == Purchase.id).exists(),
                )
                .with_for_update()
                .limit(50)
            ).scalars().all()

        for purchase in candidates:
            try:
                account = session.execute(
                    select(Account).where(Account.id == purchase.account_id)
                ).scalar_one_or_none()
                if not account or not account.seller_id:
                    # Покупка без продавца — отзыв не к кому писать, пропускаем
                    continue
                # Покупатель ≠ продавец — логично, но всё равно проверим
                if int(account.seller_id) == int(purchase.user_id):
                    continue

                # listing_id — обязательный в Review. Подбираем так же,
                # как в api_post_review.
                listing_id = purchase.listing_id
                if not listing_id:
                    linked_listing = session.execute(
                        select(Listing)
                        .where(Listing.account_id == purchase.account_id)
                        .order_by(Listing.created_at.desc())
                    ).scalars().first()
                    if not linked_listing:
                        linked_listing = Listing(
                            seller_id=int(account.seller_id),
                            account_id=account.id,
                            title=f"Покупка #{purchase.id}",
                            description="",
                            price=float(purchase.amount or 0.0),
                            origin=account.origin,
                            country=account.country,
                            status="sold",
                            buyer_id=int(purchase.user_id),
                            created_at=purchase.created_at or now,
                            sold_at=purchase.created_at or now,
                        )
                        session.add(linked_listing)
                        session.flush()
                    listing_id = linked_listing.id

                # Пишем авто-5★
                review = Review(
                    seller_id=int(account.seller_id),
                    buyer_id=int(purchase.user_id),
                    listing_id=int(listing_id),
                    purchase_id=int(purchase.id),
                    rating=5,
                    comment="",
                    created_at=now,
                    is_auto=True,
                )
                session.add(review)

                # Пересчитываем рейтинг продавца (честно, через COUNT/AVG).
                # Та же логика, что и в api_post_review — инкрементальный
                # пересчёт ломается на default-ах (rating=5.0, count=0).
                seller = session.execute(
                    select(User).where(User.telegram_id == account.seller_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if seller is not None:
                    stats = session.execute(
                        select(func.count(Review.id), func.avg(Review.rating))
                        .where(Review.seller_id == account.seller_id)
                    ).one()
                    total_count = int(stats[0] or 0)
                    avg_rating = float(stats[1] or 0.0)
                    seller.reviews_count = total_count
                    seller.rating = round(avg_rating, 2)

                # Постим в чат бот-сообщение «Отзыв выставлен автоматически».
                try:
                    a_id, b_id = sorted(
                        [int(purchase.user_id), int(account.seller_id)]
                    )
                    thread = session.execute(
                        select(ChatThread).where(
                            ChatThread.user1_id == a_id,
                            ChatThread.user2_id == b_id,
                        )
                    ).scalar_one_or_none()
                    if thread is not None:
                        auto_text = (
                            f"⭐⭐⭐⭐⭐\n\n"
                            f"Покупатель не оставил отзыв в течение "
                            f"{REVIEW_AUTO_POST_DAYS} дней — отзыв выставлен "
                            f"автоматически (5/5)."
                        )
                        _insert_bot_message(
                            session,
                            thread.id,
                            auto_text,
                            purchase_id=purchase.id,
                        )
                except Exception as _e_chat:
                    try:
                        app.logger.warning(
                            "auto-review chat msg failed: %s", _e_chat
                        )
                    except Exception:
                        pass

                posted += 1
            except Exception as e_one:
                # ��дин неудачный отзыв не должен валить всю пачку.
                try:
                    app.logger.warning(
                        "auto-review for purchase %s failed: %s",
                        getattr(purchase, "id", "?"), e_one,
                    )
                except Exception:
                    pass

        session.commit()
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        try:
            app.logger.error("auto_reviewer error: %s", e)
        except Exception:
            pass
    finally:
        session.close()
    return posted


def _auto_reviewer_loop():
    """Фоновый поток: раз в REVIEW_AUTO_CHECK_INTERVAL секунд зовёт
    _post_auto_reviews_sync(). Daemon=True — не блокируем выключение."""
    while True:
        try:
            _post_auto_reviews_sync()
        except Exception as e:
            try:
                app.logger.warning("auto_reviewer tick failed: %s", e)
            except Exception:
                pass
        time.sleep(REVIEW_AUTO_CHECK_INTERVAL)


def _start_auto_reviewer_once():
    """Запускает фоновый цикл авто-отзывов ровно один раз за процесс."""
    global _AUTO_REVIEW_LOOP_STARTED
    with _AUTO_REVIEW_LOCK:
        if _AUTO_REVIEW_LOOP_STARTED:
            return
        t = threading.Thread(
            target=_auto_reviewer_loop, name="auto-reviewer", daemon=True
        )
        t.start()
        _AUTO_REVIEW_LOOP_STARTED = True


# Авто-отзывы также запускаются внешним планировщиком/ботом, не web worker.


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not found"}), 404
    abort(404)


if __name__ == "__main__":
    # Запускаем ленивые миграции БД при старте dev-сервера
    _ensure_account_spam_status_column()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
