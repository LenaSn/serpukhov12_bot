import os
import json
import logging
import asyncio
from datetime import datetime
from random import sample

from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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
# Загрузка вопросов из JSON
with open(QUESTIONS_FILE, encoding="utf-8") as f:
    ALL_QUESTIONS = json.load(f)

# -----------------------
# Инициализация базы данных
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
# Получение или создание пользователя
async def get_or_create_user(tg_user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM users WHERE tg_id = ?", (tg_user.id,))
        row = await cursor.fetchone()
        if row:
            return row[0]
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO users (tg_id, username, first_name, last_name, first_seen) VALUES (?, ?, ?, ?, ?)",
            (tg_user.id, tg_user.username, tg_user.first_name, tg_user.last_name, now)
        )
        await db.commit()
        cursor = await db.execute("SELECT id FROM users WHERE tg_id = ?", (tg_user.id,))
        row = await cursor.fetchone()
        return row[0]

# -----------------------
# /start
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    user_id = await get_or_create_user(message.from_user)
    text = (
        "Привет! Я бот-тест по истории и культуре города Серпухова.\n\n"
        "Команды:\n"
        "/test — начать тест\n"
        "/score — посмотреть свои результаты\n"
        "/leaderboard — топ пользователей по лучшему результату\n"
        "/reset — удалить все свои данные"
    )
    await message.answer(text)

# -----------------------
# /test
@dp.message_handler(commands=["test"])
async def cmd_test(message: types.Message):
    user_db_id = await get_or_create_user(message.from_user)
    started_at = datetime.utcnow().isoformat()

    # Выбираем случайные вопросы
    test_questions = sample(ALL_QUESTIONS, NUM_QUESTIONS_PER_TEST)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO attempts (user_id, started_at) VALUES (?, ?)",
            (user_db_id, started_at)
        )
        await db.commit()
        attempt_id = cur.lastrowid

        # Сохраняем выбранные вопросы в таблице answers с индексами
        for i, q in enumerate(test_questions):
            await db.execute(
                "INSERT INTO answers (attempt_id, q_index, chosen, correct) VALUES (?, ?, ?, ?)",
                (attempt_id, i, -1, -1)  # -1 = ещё не отвечал
            )
        await db.commit()

    # Сохраняем вопросы в памяти бота для этой сессии
    dp.current_attempts = getattr(dp, "current_attempts", {})
    dp.current_attempts[attempt_id] = test_questions

    await send_question(message.chat.id, attempt_id, 0)

# -----------------------
async def send_question(chat_id: int, attempt_id: int, q_index: int):
    test_questions = dp.current_attempts[attempt_id]
    if q_index >= len(test_questions):
        await finalize_attempt(chat_id, attempt_id)
        return

    q = test_questions[q_index]
    kb = InlineKeyboardMarkup(row_width=1)
    for i, option in enumerate(q["options"]):
        kb.add(InlineKeyboardButton(text=option, callback_data=f"answer|{attempt_id}|{q_index}|{i}"))

    await bot.send_message(chat_id, f"Вопрос {q_index+1}:\n{q['question']}", reply_markup=kb)

# -----------------------
# Обработка ответа
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("answer|"))
async def process_answer(callback_query: types.CallbackQuery):
    try:
        _, attempt_id_s, q_index_s, choice_s = callback_query.data.split("|")
        attempt_id = int(attempt_id_s)
        q_index = int(q_index_s)
        choice = int(choice_s)
    except Exception:
        await callback_query.answer("Ошибка обработки ответа.", show_alert=True)
        return

    test_questions = dp.current_attempts[attempt_id]
    q = test_questions[q_index]

    # Проверяем, что правильный ответ есть в списке вариантов
    if q["correct_answer"] not in q["options"]:
        await bot.send_message(callback_query.message.chat.id, f"Ошибка: правильный ответ не найден в вариантах для вопроса: {q['question']}")
        return

    # Ищем индекс правильного ответа в списке вариантов
    correct_index = q["options"].index(q["correct_answer"])
    is_correct = 1 if choice == correct_index else 0

    async with aiosqlite.connect(DB_PATH) as db:
        # Обновляем ответ в базе данных
        await db.execute(
            "UPDATE answers SET chosen=?, correct=? WHERE attempt_id=? AND q_index=?",
            (choice, is_correct, attempt_id, q_index)
        )
        if is_correct:
            await db.execute("UPDATE attempts SET score = score + 1 WHERE id = ?", (attempt_id,))
        await db.commit()

    # Отправляем правильный ответ и объяснение в виде обычного сообщения
    explanation = q.get("explanation", "")
    answer_text = f"Вы выбрали: {q['options'][choice]}\nПравильный ответ: {q['correct_answer']}\n{explanation}"
    await bot.send_message(callback_query.message.chat.id, answer_text)

    # Показать текущий счёт после ответа
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT score FROM attempts WHERE id = ?", (attempt_id,))
        row = await cursor.fetchone()
        current_score = row[0] if row else 0
    await bot.send_message(callback_query.message.chat.id, f"Текущий счёт: {current_score} из 12")

    # Переходим к следующему вопросу
    await send_question(callback_query.message.chat.id, attempt_id, q_index + 1)

# -----------------------
# Завершение теста
async def finalize_attempt(chat_id: int, attempt_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE attempts SET completed = 1 WHERE id = ?", (attempt_id,))
        await db.commit()
        cursor = await db.execute("SELECT score, total_questions, user_id, started_at FROM attempts WHERE id = ?", (attempt_id,))
        row = await cursor.fetchone()
        if not row:
            await bot.send_message(chat_id, "Не удалось найти попытку.")
            return
        score, total_questions, user_id, started_at = row
        # get username
        cursor = await db.execute("SELECT username, first_name FROM users WHERE id = ?", (user_id,))
        urow = await cursor.fetchone()
        username = urow[0] or urow[1] or "Пользователь"

    percent = int(score * 100 / total_questions) if total_questions else 0
    result_text = f"Тест завершён!\nРезультат: {score}/{total_questions} ({percent}%)\nДата: {started_at}"

    # Проверка на победителя
    if score >= 10:
        result_text += "\nПоздравляем! Вы допущены к поездке в Серпухов!"
    else:
        result_text += "\nПопробуйте пройти тест ещё раз, чтобы получить более высокий результат."

    await bot.send_message(chat_id, result_text)

    # Показать таблицу лидеров
    await show_leaderboard(chat_id)

# -----------------------
# Таблица лидеров
async def show_leaderboard(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # лучший результат каждой попытки, затем топ
        cur = await db.execute("""
            SELECT u.username, u.first_name, MAX(a.score) as best_score
            FROM attempts a
            JOIN users u ON u.id = a.user_id
            GROUP BY a.user_id
            ORDER BY best_score DESC
            LIMIT 10
        """)
        rows = await cur.fetchall()

    if not rows:
        await bot.send_message(chat_id, "Таблица лидеров пуста. Пройдите тест, чтобы появиться в рейтинге.")
        return

    leaderboard_text = "Топ пользователей (по лучшему результату):"
    for i, r in enumerate(rows, start=1):
        username, first_name, best_score = r
        name = username or first_name or "Пользователь"
        leaderboard_text += f"\n{i}. {name} — {best_score}/12"

    await bot.send_message(chat_id, leaderboard_text)

# -----------------------
# Отправка вопроса
async def send_question(chat_id: int, attempt_id: int, q_index: int):
    test_questions = dp.current_attempts[attempt_id]
    if q_index >= len(test_questions):
        await finalize_attempt(chat_id, attempt_id)
        return

    q = test_questions[q_index]
    kb = InlineKeyboardMarkup(row_width=1)
    for i, option in enumerate(q["options"]):
        kb.add(InlineKeyboardButton(text=option, callback_data=f"answer|{attempt_id}|{q_index}|{i}"))

    await bot.send_message(chat_id, f"Вопрос {q_index+1}:\n{q['question']}", reply_markup=kb)

# -----------------------
# Финализация попытки
async def finalize_attempt(chat_id: int, attempt_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE attempts SET completed=1 WHERE id=?", (attempt_id,))
        await db.commit()
        cur = await db.execute("SELECT score FROM attempts WHERE id=?", (attempt_id,))
        row = await cur.fetchone()
        score = row[0] if row else 0

    if score >= PASS_SCORE:
        text = f"Поздравляем! 🎉 Ты набрал {score}/{NUM_QUESTIONS_PER_TEST}.\nТы допущен до поездки в город Серпухов!"
    else:
        text = f"Тест завершен. Ты набрал {score}/{NUM_QUESTIONS_PER_TEST}.\nПопробуй пройти тест ещё раз, чтобы набрать минимум {PASS_SCORE}."

    await bot.send_message(chat_id, text)

# -----------------------
# /score
@dp.message_handler(commands=["score"])
async def cmd_score(message: types.Message):
    user_db_id = await get_or_create_user(message.from_user)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, started_at, score FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user_db_id,)
        )
        rows = await cur.fetchall()
    if not rows:
        await message.reply("У тебя ещё нет попыток. Нажми /test чтобы начать.")
        return

    text_lines = ["Твои последние попытки:"]
    for r in rows:
        aid, started_at, score = r
        text_lines.append(f"#{aid} — {score}/{NUM_QUESTIONS_PER_TEST} — {started_at}")
    await message.reply("\n".join(text_lines))

# -----------------------
# /leaderboard
@dp.message_handler(commands=["leaderboard"])
async def cmd_leaderboard(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT u.username, u.first_name, MAX(a.score) as best_score
            FROM attempts a
            JOIN users u ON u.id = a.user_id
            GROUP BY a.user_id
            ORDER BY best_score DESC
            LIMIT 10
        """)
        rows = await cur.fetchall()

    if not rows:
        await message.reply("Ещё нет результатов.")
        return

    lines = ["Топ пользователей (по лучшей попытке):"]
    for i, r in enumerate(rows, start=1):
        username, first_name, best_score = r
        name = username or first_name or "Пользователь"
        lines.append(f"{i}. {name} — {best_score}/{NUM_QUESTIONS_PER_TEST}")
    await message.reply("\n".join(lines))

# -----------------------
# /reset
@dp.message_handler(commands=["reset"])
async def cmd_reset(message: types.Message):
    user_db_id = await get_or_create_user(message.from_user)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM answers WHERE attempt_id IN (SELECT id FROM attempts WHERE user_id=?)", (user_db_id,))
        await db.execute("DELETE FROM attempts WHERE user_id=?", (user_db_id,))
        await db.execute("DELETE FROM users WHERE id=?", (user_db_id,))
        await db.commit()
    await message.reply("Твои данные удалены.")

# -----------------------
# Ловушка для остальных сообщений
@dp.message_handler()
async def fallback(message: types.Message):
    await message.reply("Напиши /test чтобы начать тест, или /help для инструкций.")

# -----------------------
# Запуск
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    executor.start_polling(dp, skip_updates=True)
