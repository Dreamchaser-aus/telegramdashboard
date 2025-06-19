import os
import logging
import psycopg2
import asyncio
import nest_asyncio
from flask import Flask, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from hypercorn.asyncio import serve
from hypercorn.config import Config

# 应用初始化
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# 环境变量
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# 强制兼容嵌套事件循环（解决 run_polling() 冲突）
nest_asyncio.apply()

# 🔌 数据库连接
def get_conn():
    return psycopg2.connect(DATABASE_URL)

# ✅ 初始化数据库
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

# Telegram Bot /start 命令
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 你好，我是你的 Telegram Dashboard Bot")

# 📊 后台页面（Flask）
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

# 每日积分重置
def daily_reset():
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET plays = 0")
    conn.commit()
    conn.close()
    print("✅ 每日重置完成")

# 启动 Telegram Bot
async def run_telegram_bot():
    app_ = ApplicationBuilder().token(BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", start))
    await app_.run_polling(close_loop=False)

# 主入口：并行运行 Flask + Bot
async def main():
    init_db()

    scheduler = BackgroundScheduler()
    scheduler.add_job(daily_reset, "cron", hour=0, minute=0)
    scheduler.start()

    config = Config()
    config.bind = ["0.0.0.0:8080"]

    await asyncio.gather(
        serve(app, config),
        run_telegram_bot()
    )

# ✅ 启动程序
if __name__ == "__main__":
    asyncio.run(main())
