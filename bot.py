import os
import json
import logging
import asyncio
from datetime import datetime
from random import sample

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from dotenv import load_dotenv
import aiosqlite

from keep_alive import keep_alive
keep_alive()

# -----------------------
# Загрузка токена
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set in .env")

# -----------------------
# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------
# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot)

DB_PATH = "serpukhov_quiz.db"
QUESTIONS_FILE = "questions.json"
NUM_QUESTIONS_PER_TEST = 12
PASS_SCORE = 10  # Минимум для допуска к поездке

# -----------------------
# Память для индивидуальных сессий пользователей
user_sessions = {}  # user_id -> {"attempt_id": int, "questions": [..], "current_q": int}

# -----------------------
# Загрузка вопросов
with open(QUESTIONS_FILE, encoding="utf-8") as f:
    ALL_QUESTIONS = json.load(f)

# -----------------------
# Инициализация БД
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen TEXT
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            started_at TEXT,
            completed INTEGER DEFAULT 0,
            score INTEGER DEFAULT 0
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER,
            q_index INTEGER,
            chosen INTEGER,
            correct INTEGER
        );
        """)
        await db.commit()
    logger.info("DB initialized")

# -----------------------
# Получить или создать пользователя
async def get_or_create_user(tg_user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_user.id,))
        row = await cur.fetchone()
        if row:
            return row[0]
        now = datetime.utcnow().isoformat()
        await db.execute("""
            INSERT INTO users (tg_id, username, first_name, last_name, first_seen)
            VALUES (?, ?, ?, ?, ?)
        """, (tg_user.id, tg_user.username, tg_user.first_name, tg_user.last_name, now))
        await db.commit()
        cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_user.id,))
        row = await cur.fetchone()
        return row[0]

# -----------------------
# Команда /start
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    await get_or_create_user(message.from_user)
    text = (
        "Привет! Я бот-тест по истории и культуре города Серпухова 🦚\n\n"
        "Команды:\n"
        "/test — начать тест\n"
        "/score — посмотреть свои результаты\n"
        "/leaderboard — топ пользователей по лучшему результату\n"
        "/reset — удалить свои данные"
    )
    await message.answer(text)

# -----------------------
# Команда /test
@dp.message_handler(commands=["test"])
async def cmd_test(message: types.Message):
    user = message.from_user
    user_db_id = await get_or_create_user(user)
    started_at = datetime.utcnow().isoformat()

    # Формируем новую попытку
    questions = sample(ALL_QUESTIONS, NUM_QUESTIONS_PER_TEST)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO attempts (user_id, started_at) VALUES (?, ?)",
            (user_db_id, started_at)
        )
        await db.commit()
        attempt_id = cur.lastrowid
        # Создаем заготовки ответов
        for i in range(NUM_QUESTIONS_PER_TEST):
            await db.execute(
                "INSERT INTO answers (attempt_id, q_index, chosen, correct) VALUES (?, ?, ?, ?)",
                (attempt_id, i, -1, -1)
            )
        await db.commit()

    # Сохраняем состояние пользователя
    user_sessions[user.id] = {
        "attempt_id": attempt_id,
        "questions": questions,
        "current_q": 0
    }

    await send_question(user.id, attempt_id, 0)

# -----------------------
async def send_question(user_id: int, attempt_id: int, q_index: int):
    session = user_sessions.get(user_id)
    if not session:
        return

    questions = session["questions"]
    if q_index >= len(questions):
        await finalize_attempt(user_id, attempt_id)
        return

    q = questions[q_index]
    kb = InlineKeyboardMarkup(row_width=1)
    for i, option in enumerate(q["options"]):
        kb.add(InlineKeyboardButton(text=option, callback_data=f"answer|{attempt_id}|{q_index}|{i}"))

    await bot.send_message(user_id, f"Вопрос {q_index + 1}:\n{q['question']}", reply_markup=kb)

# -----------------------
@dp.callback_query_handler(lambda c: c.data.startswith("answer|"))
async def process_answer(callback_query: types.CallbackQuery):
    if not hasattr(dp, "current_attempts") or attempt_id not in dp.current_attempts:
        await callback_query.answer("⏳ Сессия теста устарела. Начни заново — /test", show_alert=True)
        return
    try:
        _, attempt_id_s, q_index_s, choice_s = callback_query.data.split("|")
        attempt_id = int(attempt_id_s)
        q_index = int(q_index_s)
        choice = int(choice_s)
    except Exception:
        await callback_query.answer("Ошибка обработки ответа.")
        return

    user_id = callback_query.from_user.id
    session = user_sessions.get(user_id)
    if not session:
        await callback_query.answer("Сессия не найдена. Начни тест заново: /test")
        return

    questions = session["questions"]
    q = questions[q_index]
    correct_index = q["options"].index(q["correct_answer"])
    is_correct = 1 if choice == correct_index else 0

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE answers SET chosen=?, correct=? WHERE attempt_id=? AND q_index=?",
            (choice, is_correct, attempt_id, q_index)
        )
        if is_correct:
            await db.execute("UPDATE attempts SET score = score + 1 WHERE id=?", (attempt_id,))
        await db.commit()

    # Ответ и пояснение
    explanation = q.get("explanation", "")
    await bot.send_message(
        user_id,
        f"Вы выбрали: {q['options'][choice]}\nПравильный ответ: {q['correct_answer']}\n{explanation}"
    )

    # Текущий счёт
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT score FROM attempts WHERE id=?", (attempt_id,))
        row = await cur.fetchone()
        score = row[0] if row else 0
    await bot.send_message(user_id, f"Текущий счёт: {score} из {NUM_QUESTIONS_PER_TEST}")

    session["current_q"] += 1
    await send_question(user_id, attempt_id, q_index + 1)

# -----------------------
async def finalize_attempt(user_id: int, attempt_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE attempts SET completed=1 WHERE id=?", (attempt_id,))
        await db.commit()
        cur = await db.execute("SELECT score FROM attempts WHERE id=?", (attempt_id,))
        row = await cur.fetchone()
        score = row[0] if row else 0

    if score >= PASS_SCORE:
        text = f"🎉 Поздравляем! Ты набрал {score}/{NUM_QUESTIONS_PER_TEST} и допущен к поездке в Серпухов!"
    else:
        text = f"Тест завершён. Ты набрал {score}/{NUM_QUESTIONS_PER_TEST}. Попробуй ещё раз, чтобы набрать минимум {PASS_SCORE}."

    await bot.send_message(user_id, text)

    # Показываем таблицу лидеров
    await show_leaderboard(user_id)

    # Удаляем сессию
    if user_id in user_sessions:
        del user_sessions[user_id]

# -----------------------
@dp.message_handler(commands=["score"])
async def cmd_score(message: types.Message):
    user_db_id = await get_or_create_user(message.from_user)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, started_at, score FROM attempts
            WHERE user_id=? ORDER BY id DESC LIMIT 10
        """, (user_db_id,))
        rows = await cur.fetchall()
    if not rows:
        await message.reply("У тебя ещё нет результатов. Нажми /test, чтобы начать.")
        return

    text = "📊 Твои последние попытки:\n"
    for i, (aid, started, score) in enumerate(rows, start=1):
        text += f"{i}. {score}/{NUM_QUESTIONS_PER_TEST} — {started}\n"
    await message.reply(text)

# -----------------------
@dp.message_handler(commands=["leaderboard"])
async def cmd_leaderboard(message: types.Message):
    await show_leaderboard(message.chat.id)

async def show_leaderboard(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT u.username, u.first_name, MAX(a.score)
            FROM attempts a
            JOIN users u ON u.id = a.user_id
            GROUP BY a.user_id
            ORDER BY MAX(a.score) DESC
            LIMIT 10
        """)
        rows = await cur.fetchall()

    if not rows:
        await bot.send_message(chat_id, "Таблица лидеров пока пуста.")
        return

    text = "🏆 Топ пользователей:\n"
    for i, (username, fname, score) in enumerate(rows, start=1):
        name = username or fname or "Пользователь"
        text += f"{i}. {name} — {score}/{NUM_QUESTIONS_PER_TEST}\n"

    await bot.send_message(chat_id, text)

# -----------------------
@dp.message_handler(commands=["reset"])
async def cmd_reset(message: types.Message):
    user_db_id = await get_or_create_user(message.from_user)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM answers WHERE attempt_id IN (SELECT id FROM attempts WHERE user_id=?)", (user_db_id,))
        await db.execute("DELETE FROM attempts WHERE user_id=?", (user_db_id,))
        await db.execute("DELETE FROM users WHERE id=?", (user_db_id,))
        await db.commit()
    if message.from_user.id in user_sessions:
        del user_sessions[message.from_user.id]
    await message.reply("Все твои данные удалены.")

# -----------------------
@dp.message_handler()
async def fallback(message: types.Message):
    await message.reply("Напиши /test чтобы начать тест, или /help для справки.")

if __name__ == "__main__":
    asyncio.run(init_db())
    executor.start_polling(dp, skip_updates=True)
