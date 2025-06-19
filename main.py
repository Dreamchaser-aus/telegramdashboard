import os
import logging
import psycopg2
import asyncio
from flask import Flask, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# 环境变量
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# 初始化 Flask
app = Flask(__name__)

# 日志
logging.basicConfig(level=logging.INFO)

# 获取数据库连接
def get_conn():
    return psycopg2.connect(DATABASE_URL)

# 初始化数据库表
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
            invited_by BIGINT,
            created_at TEXT
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

# Telegram /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text("🎯 欢迎使用 Telegram Dashboard Bot！")

# 每日重置任务
def daily_reset():
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET plays = 0")
    conn.commit()
    conn.close()
    print("✅ 每日已重置 plays")

# Flask 后台页面
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

# 启动 Flask + Bot
async def run_telegram_bot():
    app_ = ApplicationBuilder().token(BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", start))
    await app_.run_polling(close_loop=False)

async def main():
    init_db()

    # 启动调度器
    scheduler = BackgroundScheduler()
    scheduler.add_job(daily_reset, "cron", hour=0, minute=0)
    scheduler.start()

    # 并发运行 Flask + Telegram Bot
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    config = Config()
    config.bind = ["0.0.0.0:8080"]

    await asyncio.gather(
        serve(app, config),
        run_telegram_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
