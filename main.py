import os
import asyncio
import logging
import psycopg2
from datetime import datetime
from flask import Flask, render_template, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from hypercorn.asyncio import serve
from hypercorn.config import Config

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

app = Flask(__name__)

def get_conn():
    print("🔍 DATABASE_URL:", repr(DATABASE_URL))
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
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

# === Telegram Bot Handler ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 欢迎来到骰子游戏！发送 /play 开始游戏。")

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_dice()

async def main():
    init_db()

    # Step 1: 创建 Telegram Application
    app_ = ApplicationBuilder().token(BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", start))
    app_.add_handler(CommandHandler("play", play))

    scheduler = AsyncIOScheduler()
    scheduler.start()

    # Step 2: 初始化 bot（但不 run_polling）
    await app_.initialize()
    await app_.start()
    await app_.updater.start_polling()

    # Step 3: 启动 Flask 使用 Hypercorn
    config = Config()
    config.bind = ["0.0.0.0:8080"]
    await serve(app, config)

if __name__ == "__main__":
    asyncio.run(main())
