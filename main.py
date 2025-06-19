# 完整整合版 main.py
import os
import logging
import psycopg2
import asyncio
import random
import nest_asyncio
from datetime import datetime, date
from flask import Flask, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatMemberHandler, ContextTypes, filters
)
from hypercorn.asyncio import serve
from hypercorn.config import Config
from dotenv import load_dotenv

load_dotenv()
nest_asyncio.apply()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
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
    ''')
    conn.commit()
    conn.close()

@app.route("/")
def dashboard():
    keyword = request.args.get("keyword", "")
    conn = get_conn()
    c = conn.cursor()
    if keyword:
        c.execute("""
            SELECT u.*, i.username
            FROM users u LEFT JOIN users i ON u.invited_by = i.user_id
            WHERE u.username ILIKE %s OR u.phone ILIKE %s
        """, (f"%{keyword}%", f"%{keyword}%"))
    else:
        c.execute("""
            SELECT u.*, i.username
            FROM users u LEFT JOIN users i ON u.invited_by = i.user_id
        """)
    users = c.fetchall()

    c.execute("SELECT username, first_name, points FROM users ORDER BY points DESC LIMIT 10")
    total_rank = c.fetchall()

    today = date.today().isoformat()
    c.execute("SELECT username, first_name, points FROM users WHERE last_play LIKE %s ORDER BY points DESC LIMIT 10", (f"{today}%",))
    today_rank = c.fetchall()

    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE phone IS NOT NULL")
    authorized_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 1")
    blocked_users = c.fetchone()[0]
    c.execute("SELECT SUM(points) FROM users")
    total_points = c.fetchone()[0] or 0
    conn.close()

    stats = {
        "total_users": total_users,
        "authorized_users": authorized_users,
        "blocked_users": blocked_users,
        "total_points": total_points
    }

    return render_template("dashboard.html", users=users, stats=stats, total_rank=total_rank, today_rank=today_rank)

@app.route("/update_user", methods=["POST"])
def update_user():
    user_id = request.form["user_id"]
    points = int(request.form["points"])
    plays = int(request.form["plays"])
    is_blocked = int(request.form["is_blocked"])
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET points = %s, plays = %s, is_blocked = %s WHERE user_id = %s",
              (points, plays, is_blocked, user_id))
    conn.commit()
    conn.close()
    return "OK"

@app.route("/delete_user", methods=["POST"])
def delete_user():
    user_id = request.form["user_id"]
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()
    return "OK"

# Telegram 逻辑
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    inviter_id = int(context.args[0]) if context.args else None
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = %s", (user.id,))
    if not c.fetchone():
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO users (user_id, first_name, last_name, username, plays, points, created_at, invited_by)
            VALUES (%s, %s, %s, %s, 0, 0, %s, %s)
        """, (user.id, user.first_name, user.last_name, user.username, now, inviter_id))
        conn.commit()
    conn.close()

    keyboard = ReplyKeyboardMarkup([[KeyboardButton("\ud83d\udcf1 分享手机号", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("\u26a0\ufe0f 为参与群组游戏，请先授权手机号：", reply_markup=keyboard)

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    phone = update.message.contact.phone_number
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET phone = %s WHERE user_id = %s", (phone, user.id))
    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("\ud83c\udfb2 开始游戏", callback_data="start_game")]])
    await update.message.reply_text("\u2705 手机号授权成功！点击按钮开始游戏吧～", reply_markup=keyboard)
    await reward_inviter(user.id, context)

async def reward_inviter(user_id, context):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT invited_by, phone, inviter_rewarded, plays FROM users WHERE user_id = %s", (user_id,))
    row = c.fetchone()
    if row:
        inviter, phone, rewarded, plays = row
        if inviter and phone and not rewarded and plays > 0:
            c.execute("UPDATE users SET points = points + 10 WHERE user_id = %s", (inviter,))
            c.execute("UPDATE users SET inviter_rewarded = 1 WHERE user_id = %s", (user_id,))
            conn.commit()
            try:
                context.bot.send_message(chat_id=inviter, text="\ud83c\udff1 你邀请的用户已成功参与游戏，积分 +10！")
            except:
                pass
    conn.close()

async def start_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT is_blocked, plays, phone FROM users WHERE user_id = %s", (user.id,))
    row = c.fetchone()
    if not row:
        await query.edit_message_text("\u26a0\ufe0f 你还未授权手机号，请先私聊我发送手机号授权。")
        return
    is_blocked, plays, phone = row
    if is_blocked:
        await query.edit_message_text("\u26d4️ 你已被禁止参与，请联系管理员。")
        return
    if not phone:
        await query.edit_message_text("\ud83d\udcf5 请先授权手机号后参与游戏！")
        return
    if plays >= 10:
        await query.edit_message_text("\u274c 今天已用完10次，请明天再来！")
        return
    await query.delete_message()
    dice1 = await context.bot.send_dice(chat_id=query.message.chat_id)
    await asyncio.sleep(3)
    dice2 = await context.bot.send_dice(chat_id=query.message.chat_id)
    await asyncio.sleep(3)
    score = 10 if dice1.dice.value > dice2.dice.value else -5 if dice1.dice.value < dice2.dice.value else 0
    c.execute("UPDATE users SET points = points + %s, plays = plays + 1, last_play = %s WHERE user_id = %s",
              (score, datetime.now().isoformat(), user.id))
    c.execute("SELECT points FROM users WHERE user_id = %s", (user.id,))
    total = c.fetchone()[0]
    conn.commit()
    conn.close()
    msg = f"\ud83c\udfb2 你掲出{dice1.dice.value}，我掲出{dice2.dice.value}！"
    msg += "本局赢了！+10积分" if score > 0 else "输了... -5积分" if score < 0 else "平局！"
    msg += f" 当前总积分：{total}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("\ud83c\udfb2 再来一次", callback_data="start_game")]])
    await context.bot.send_message(chat_id=query.message.chat_id, text=msg, reply_markup=keyboard)

async def show_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT username, first_name, points FROM users WHERE last_play LIKE %s ORDER BY points DESC LIMIT 10", (f"{today}%",))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("\ud83d\udcec 今日暂无玩家积分记录")
        return
    msg = "\ud83d\udcca 今日排行榜\uff1a\n"
    medals = ["\ud83e\udd47", "\ud83e\udd48", "\ud83e\udd49"] + ["\ud83c\udfc6"] * 7
    for i, row in enumerate(rows):
        name = row[0] or row[1] or "匿名"
        msg += f"{medals[i]} {name[:4]}*** - {row[2]} 分\n"
    await update.message.reply_text(msg)

async def share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_name = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_name}?start={user.id}"
    await update.message.reply_text(f"\ud83d\udd17 你的邀请链接\uff1a\n{link}\n\n\ud83c\udff1 邀请成功即可获得 +10 积分奖励！")

async def group_text_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT phone FROM users WHERE user_id = %s", (user.id,))
    row = c.fetchone()
    if not row or not row[0]:
        await update.message.reply_text(f"\u26a0\ufe0f @{user.username or user.first_name} 请先授权手机号后才能参与游戏！")
    conn.close()

async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.dice:
        await start_game_callback(update, context)
    elif update.message.contact:
        await contact_handler(update, context)
    elif update.message.text and update.message.chat.type in ['group', 'supergroup']:
        await group_text_filter(update, context)

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = update.chat_member
    inviter = chat_member.from_user
    new_user = chat_member.new_chat_member.user
    if chat_member.old_chat_member.status == "left" and chat_member.new_chat_member.status == "member":
        if new_user.is_bot or inviter.id == new_user.id:
            return
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = %s", (new_user.id,))
        if not c.fetchone():
            now = datetime.now().isoformat()
            c.execute("INSERT INTO users (user_id, username, invited_by, created_at) VALUES (%s, %s, %s, %s)",
                      (new_user.id, new_user.username or '', inviter.id, now))
            conn.commit()
        conn.close()

async def run_telegram_bot():
    app_ = ApplicationBuilder().token(BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", start))
    app_.add_handler(CommandHandler("rank", show_rank))
    app_.add_handler(CommandHandler("share", share))
    app_.add_handler(MessageHandler(filters.ALL, message_router))
    app_.add_handler(CallbackQueryHandler(start_game_callback, pattern="^start_game$"))
    app_.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))
    await app_.run_polling(close_loop=False)

def reset_daily():
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET plays = 0")
    conn.commit()
    conn.close()
    print("\ud83d\udd04 已重置每日次数")

async def main():
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(reset_daily, "cron", hour=0, minute=0)
    scheduler.start()
    config = Config()
    config.bind = ["0.0.0.0:8080"]
    await asyncio.gather(serve(app, config), run_telegram_bot())

if __name__ == "__main__":
    asyncio.run(main())
