import os
import asyncio
import logging
import psycopg2
from datetime import datetime, date
from flask import Flask, render_template, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from threading import Thread
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

# === 环境变量 ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# === Flask Web ===
app = Flask(__name__)

def get_conn():
    print("🔍 DATABASE_URL:", repr(DATABASE_URL))  # 调试用
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        username TEXT,
        phone TEXT,
        points INTEGER DEFAULT 0,
        plays INTEGER DEFAULT 0,
        created_at TEXT,
        last_play TEXT,
        invited_by BIGINT,
        inviter_rewarded INTEGER DEFAULT 0,
        is_blocked INTEGER DEFAULT 0
    );
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS game_history (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        result TEXT,
        points_change INTEGER,
        created_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    );
    """)

    conn.commit()
    conn.close()

@app.route("/")
def dashboard():
    keyword = request.args.get("keyword", "")
    conn = get_conn()
    c = conn.cursor()
    if keyword:
        c.execute("""
            SELECT u.user_id, u.username, u.phone, u.points, u.plays, u.invited_by, i.username
            FROM users u LEFT JOIN users i ON u.invited_by = i.user_id
            WHERE u.username ILIKE %s OR u.phone ILIKE %s
        """, (f"%{keyword}%", f"%{keyword}%"))
    else:
        c.execute("""
            SELECT u.user_id, u.username, u.phone, u.points, u.plays, u.invited_by, i.username
            FROM users u LEFT JOIN users i ON u.invited_by = i.user_id
        """)
    users = c.fetchall()
    conn.close()
    return render_template("dashboard.html", users=users)

# === Telegram Bot ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎲 欢迎来到骰子游戏！发送 /play 开始掷骰子～")

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_dice("🎲")

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

async def run_bot():
    init_db()
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("play", play))
    scheduler = AsyncIOScheduler()
    scheduler.start()
    await application.initialize()
    await application.start()
    await application.run_polling()  # ✅ 新写法兼容 python-telegram-bot v20+

if __name__ == "__main__":
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_bot())  # ✅ 替换 asyncio.run() 避免 RuntimeError
