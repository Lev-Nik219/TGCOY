#!/usr/bin/env python3
"""
FINANCE BOT - Инвестиционная система
"""

import asyncio
import logging
import sqlite3
import hashlib
import requests
import json
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from contextlib import closing
from collections import defaultdict

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = "8983461211:AAFvT-AVltZNKQLEPy3TD0BsMHeEol3IRcE"
ADMIN_IDS = [5432126918]

# CryptoBot API (для реальных выплат)
CRYPTOBOT_API_KEY = "588567:AAef3E1a3WIHR1FQaY3OJhzs1T30kwWRRxp"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

REFERRAL_BONUS_LEVELS = [0.07, 0.03, 0.01]
MIN_DEPOSIT = 10.0
MAX_DEPOSIT = 10000.0
WITHDRAW_FEE = 0.02
MIN_WITHDRAW = 5.0

INVEST_PACKAGES = {
    7: {"percent": 1.2, "bonus": 5},
    14: {"percent": 1.5, "bonus": 15},
    30: {"percent": 2.0, "bonus": 50}
}

DB_NAME = "finance_bot.db"

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                surname TEXT,
                age INTEGER,
                balance REAL DEFAULT 0,
                referrer_id INTEGER DEFAULT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_daily TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                device_fingerprint TEXT UNIQUE,
                total_deposits REAL DEFAULT 0,
                total_withdraws REAL DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS investments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                package_days INTEGER,
                daily_percent REAL,
                start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_date TIMESTAMP,
                status TEXT DEFAULT 'active'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS withdraws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                wallet TEXT,
                crypto_type TEXT DEFAULT 'USDT',
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER,
                level INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                fingerprint TEXT PRIMARY KEY,
                user_id INTEGER,
                blocked BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    print("✅ База данных инициализирована")

def user_exists(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, surname, age, balance, referrer_id, last_daily FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return {
                "name": row[0],
                "surname": row[1],
                "age": row[2],
                "balance": row[3],
                "referrer_id": row[4],
                "last_daily": row[5]
            }
        return None

def add_user(user_id: int, name: str, surname: str, age: int, referrer_id: int = None, fingerprint: str = ""):
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (user_id, name, surname, age, referrer_id, device_fingerprint)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, name, surname, age, referrer_id, fingerprint))
        conn.commit()

def update_balance(user_id: int, amount: float, tx_type: str = "manual"):
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        cursor.execute("""
            INSERT INTO transactions (user_id, amount, type, status)
            VALUES (?, ?, ?, 'completed')
        """, (user_id, amount, tx_type))
        conn.commit()

def get_all_users() -> list:
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, name, balance FROM users")
        return cursor.fetchall()

def get_device_fingerprint(message: Message) -> str:
    data = f"{message.from_user.id}_{message.from_user.username}_{message.from_user.language_code}"
    return hashlib.sha256(data.encode()).hexdigest()

def is_multiacccount(fingerprint: str, user_id: int) -> bool:
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, blocked FROM devices WHERE fingerprint = ?", (fingerprint,))
        result = cursor.fetchone()
        if result:
            existing_user_id, blocked = result
            if existing_user_id != user_id:
                if not blocked:
                    cursor.execute("UPDATE devices SET blocked = TRUE WHERE fingerprint = ?", (fingerprint,))
                    conn.commit()
                return True
        else:
            cursor.execute("INSERT INTO devices (fingerprint, user_id) VALUES (?, ?)", (fingerprint, user_id))
            conn.commit()
    return False

def process_referral_chain(user_id: int, amount: float):
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return
        referrer_id = row[0]
        level = 1
        while referrer_id and level <= 3:
            bonus = amount * REFERRAL_BONUS_LEVELS[level - 1]
            if bonus > 0:
                cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (bonus, referrer_id))
                cursor.execute("INSERT INTO transactions (user_id, amount, type, status) VALUES (?, ?, 'referral_bonus', 'completed')", (referrer_id, bonus))
            cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (referrer_id,))
            row2 = cursor.fetchone()
            referrer_id = row2[0] if row2 else None
            level += 1
        conn.commit()

def create_investment(user_id: int, amount: float, package_days: int):
    package = INVEST_PACKAGES[package_days]
    end_date = datetime.now() + timedelta(days=package_days)
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO investments (user_id, amount, package_days, daily_percent, end_date)
            VALUES (?,


?, ?, ?, ?)
        """, (user_id, amount, package_days, package["percent"], end_date))
        if package["bonus"] > 0:
            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (package["bonus"], user_id))
            cursor.execute("INSERT INTO transactions (user_id, amount, type, status) VALUES (?, ?, 'package_bonus', 'completed')", (user_id, package["bonus"]))
        conn.commit()
    process_referral_chain(user_id, amount)

def calculate_daily_interest():
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id, amount, daily_percent FROM investments WHERE status = 'active' AND end_date > datetime('now')")
        investments = cursor.fetchall()
        for inv_id, user_id, amount, daily_percent in investments:
            interest = amount * (daily_percent / 100)
            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (interest, user_id))
            cursor.execute("INSERT INTO transactions (user_id, amount, type, status) VALUES (?, ?, 'daily_interest', 'completed')", (user_id, interest))
        cursor.execute("UPDATE investments SET status = 'completed' WHERE status = 'active' AND end_date <= datetime('now')")
        conn.commit()

# ==================== FSM СОСТОЯНИЯ ====================
class RegisterStates(StatesGroup):
    name = State()
    surname = State()
    age = State()

class DepositStates(StatesGroup):
    amount = State()

class WithdrawStates(StatesGroup):
    amount = State()
    wallet = State()

class AdminStates(StatesGroup):
    broadcast = State()

# ==================== КЛАВИАТУРЫ ====================
def main_kb(user_id: int):
    buttons = [
        [KeyboardButton(text="📊 Мой профиль")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📈 Инвестировать")],
        [KeyboardButton(text="💸 Пополнить"), KeyboardButton(text="💳 Вывести")],
        [KeyboardButton(text="👥 Рефералы"), KeyboardButton(text="📜 История")],
        [KeyboardButton(text="❓ Помощь")]
    ]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

# ==================== ОБРАБОТЧИКИ ====================
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    fingerprint = get_device_fingerprint(message)
    if is_multiacccount(fingerprint, user_id):
        await message.answer("⛔ Обнаружена попытка создания мультиаккаунта. Доступ заблокирован.")
        return
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() and int(args[1]) != user_id else None
    if user_exists(user_id):
        user = get_user(user_id)
        await message.answer(f"С возвращением, {user['name']}!", reply_markup=main_kb(user_id))
    else:
        await state.update_data(referrer_id=referrer_id, fingerprint=fingerprint)
        await message.answer("Введите ваше имя:", reply_markup=cancel_kb)
        await state.set_state(RegisterStates.name)

@router.message(RegisterStates.name, F.text)
async def process_name(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Регистрация отменена. /start")
        return
    await state.update_data(name=message.text.strip())
    await message.answer("Введите фамилию:")
    await state.set_state(RegisterStates.surname)

@router.message(RegisterStates.surname, F.text)
async def process_surname(message: Message, state: FSMContext):
    await state.update_data(surname=message.text.strip())
    await message.answer("Введите возраст (от 14 до 100):")
    await state.set_state(RegisterStates.age)

@router.message(RegisterStates.age, F.text)
async def process_age(message: Message, state: FSMContext):
    try:
        age = int(message.text.strip())
        if age < 14 or age > 100:
            await message.answer("Возраст от 14 до 100")
            return
    except:
        await message.answer("Введите число")
        return
    data = await state.get_data()
    user_id = message.from_user.id
    add_user(user_id, data["name"], data["surname"], age, data.get("referrer_id"), data.get("fingerprint", ""))
    if data.get("referrer_id"):
        with closing(sqlite3.connect(DB_NAME)) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO referrals (referrer_id, referred_id, level) VALUES (?, ?, 1)", (data["referrer_id"], user_id))
            conn.commit()
    await message.answer(f"Регистрация завершена! Добро пожаловать, {data['name']}!", reply_markup=main_kb(user_id))
    await state.clear()

@router.message(F.text == "💰 Баланс")
async def show_balance(message: Message):
    user_id = message.from_user.id
    if not user_exists(user_id):
        await message.answer("/start для регистрации")
        return
    user = get_user(user_id)
    await message.answer(f"💰 Баланс: {user['balance']:.2f}$")

@router.message(F.text == "📈 Инвестировать")
async def show_packages(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней (1.2% в день +5$)", callback_data="invest_7")],
        [InlineKeyboardButton(text="14 дней (1.5% в день +15$)", callback_data="invest_14")],
        [InlineKeyboardButton(text="30 дней (2.0% в день +50$)", callback_data="invest_30")]
    ])
    await message.answer("Выберите пакет:", reply_markup=keyboard)

@router.callback_query(F.data.startswith("invest_"))
async def process_package(callback: CallbackQuery, state: FSMContext):
    days = int(callback.data.split("_")[1])
    await state.update_data(package_days=days)
    await callback.message.answer(f"Введите сумму (от {MIN_DEPOSIT}$ до {MAX_DEPOSIT}$):")
    await state.set_state(DepositStates.amount)
    await callback.answer()

@router.message(DepositStates.amount)
async def process_investment(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < MIN_DEPOSIT or amount > MAX_DEPOSIT:
            await message.answer(f"Сумма от {MIN_DEPOSIT}$ до {MAX_DEPOSIT}$")
            return
        user = get_user(message.from_user.id)
        if user['balance'] < amount:
            await message.answer(f"Недостаточно средств. Баланс: {user['balance']:.2f}$")
            return
        update_balance(message.from_user.id, -amount, "investment")
        data = await state.get_data()
        create_investment(message.from_user.id, amount, data["package_days"])
        await message.answer(f"Инвестиция {amount}$ активирована на {data['package_days']} дней!")
        await state.clear()
    except:
        await message.answer("Введите число")

@router.message(F.text == "💸 Пополнить")
async def deposit_start(message: Message):
    await message.answer("💸 Пополнение через CryptoBot пока в разработке. Для теста напишите админу.")

@router.message(F.text == "💳 Вывести")
async def withdraw_start(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user['balance'] < MIN_WITHDRAW:
        await message.answer(f"Минимальная сумма вывода: {MIN_WITHDRAW}$")
        return
    await message.answer(f"Введите сумму вывода (мин. {MIN_WITHDRAW}$, комиссия {WITHDRAW_FEE*100}%):")
    await state.set_state(WithdrawStates.amount)

@router.message(WithdrawStates.amount)
async def withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < MIN_WITHDRAW:
            await message.answer(f"Минимум {MIN_WITHDRAW}$")
            return
        user = get_user(message.from_user.id)
        if user['balance'] < amount:
            await message.answer("Недостаточно средств")
            return
        await state.update_data(amount=amount)
        await message.answer("Введите адрес USDT кошелька:")
        await state.set_state(WithdrawStates.wallet)
    except:
        await message.answer("Введите число")

@router.message(WithdrawStates.wallet)
async def withdraw_wallet(message: Message, state: FSMContext):
    wallet = message.text.strip()
    if len(wallet) < 10:
        await message.answer("Некорректный адрес")
        return
    data = await state.get_data()
    amount = data["amount"]
    fee = amount * WITHDRAW_FEE
    net_amount = amount - fee
    user_id = message.from_user.id
    update_balance(user_id, -amount, "withdraw")
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO withdraws (user_id, amount, wallet, status) VALUES (?, ?, ?, 'pending')", (user_id, net_amount, wallet))
        conn.commit()
    await message.answer(f"Заявка на вывод {net_amount:.2f}$ (комиссия {fee:.2f}$) отправлена!")
    await state.clear()

@router.message(F.text == "👥 Рефералы")
async def show_referrals(message: Message):
    user_id = message.from_user.id
    bot_username = (await message.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={user_id}"
    await message.answer(f"Ваша реферальная ссылка:\n{link}\n\nВы получаете 7% / 3% / 1% от депозитов приглашённых!")

@router.message(F.text == "📜 История")
async def show_history(message: Message):
    user_id = message.from_user.id
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT amount, type, created_at FROM transactions WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user_id,))
        rows = cursor.fetchall()
        if not rows:
            await message.answer("История пуста")
            return
        text = "📜 Последние транзакции:\n\n"
        for amount, tx_type, date in rows:
            text += f"{date[:16]} | {tx_type}: {amount:+.2f}$\n"
        await message.answer(text)

@router.message(F.text == "❓ Помощь")
async def show_help(message: Message):
    await message.answer("Доступные команды в меню.\n\nПо вопросам: @admin")

@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещён")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")]
    ])
    await message.answer("Админ-панель:", reply_markup=keyboard)

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), SUM(balance) FROM users")
        total_users, total_balance = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM withdraws WHERE status='pending'")
        pending = cursor.fetchone()[0]
    text = f"Пользователей: {total_users}\nБаланс: {total_balance:.2f}$\nЗаявок на вывод: {pending}"
    await callback.message.edit_text(text)
    await callback.answer()

# ==================== ЗАПУСК ====================
async def daily_task(bot: Bot):
    while True:
        await asyncio.sleep(3600)
        now = datetime.now()
        if now.hour == 0 and now.minute == 0:
            calculate_daily_interest()
            print(f"Начислены проценты за {now.date()}")

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(daily_task(bot))
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
