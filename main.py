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

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

nest_asyncio.apply()

# 获取数据库连接
def get_conn():
    return psycopg2.connect(DATABASE_URL)

# 初始化数据库
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

# Flask 后台管理页面
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

# Telegram Bot 逻辑开始
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    inviter_id = None
    if context.args:
        try:
            inviter_id = int(context.args[0])
        except:
            pass

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = %s", (user.id,))
    if not c.fetchone():
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO users (user_id, first_name, last_name, username, plays, points, created_at, invited_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (user.id, user.first_name, user.last_name, user.username, 0, 0, now, inviter_id))
        conn.commit()
    conn.close()

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 分享手机号", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text("⚠️ 为参与群组游戏，请先授权手机号：", reply_markup=keyboard)

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    phone = update.message.contact.phone_number
    conn = get_conn()
    c = conn.cursor()

    c.execute("UPDATE users SET phone = %s WHERE user_id = %s", (phone, user.id))
    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 开始游戏", callback_data="start_game")]])
    await update.message.reply_text("✅ 手机号授权成功！点击按钮开始游戏吧～", reply_markup=keyboard)
    await reward_inviter(user.id, context)

async def start_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT is_blocked, plays, phone FROM users WHERE user_id = %s", (user.id,))
    row = c.fetchone()
    if not row:
        await query.edit_message_text("⚠️ 你还未授权手机号，请先私聊我发送手机号授权。")
        conn.close()
        return
    is_blocked, plays, phone = row
    if is_blocked == 1:
        await query.edit_message_text("⛔️ 你已被禁止参与互动，请联系管理员。")
        conn.close()
        return
    if not phone:
        await query.edit_message_text("📵 请先私聊我授权手机号后才能参与游戏！")
        conn.close()
        return
    if plays >= 10:
        await query.edit_message_text("❌ 今天已用完10次机会，请明天再来！")
        conn.close()
        return
    await query.delete_message()

    dice1 = await context.bot.send_dice(chat_id=query.message.chat_id)
    await asyncio.sleep(3)
    dice2 = await context.bot.send_dice(chat_id=query.message.chat_id)
    await asyncio.sleep(3)

    score = 0
    if dice1.dice.value > dice2.dice.value:
        score = 10
    elif dice1.dice.value < dice2.dice.value:
        score = -5

    c.execute("UPDATE users SET points = points + %s, plays = plays + 1, last_play = %s WHERE user_id = %s",
              (score, datetime.now().isoformat(), user.id))
    c.execute("SELECT points FROM users WHERE user_id = %s", (user.id,))
    total = c.fetchone()[0]
    conn.commit()
    conn.close()

    msg = f"🎲 你掷出{dice1.dice.value}，我掷出{dice2.dice.value}！本局"
    msg += "赢了！+10积分" if score > 0 else "输了... -5积分" if score < 0 else "平局！"
    msg += f" 当前总积分：{total}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 再来一次", callback_data="start_game")]])
    await context.bot.send_message(chat_id=query.message.chat_id, text=msg, reply_markup=keyboard)

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
                context.bot.send_message(chat_id=inviter, text="🎁 你邀请的用户已成功参与游戏，积分 +10！")
            except:
                pass
    conn.close()

async def show_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT username, first_name, points FROM users
        WHERE last_play LIKE %s
        ORDER BY points DESC LIMIT 10
    """, (f"{today}%",))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📬 今日暂无玩家积分记录")
        return
    msg = "📊 今日排行榜：\n"
    medals = ["🥇", "🥈", "🥉"] + ["🎖"] * 7
    for i, row in enumerate(rows):
        name = row[0] or row[1] or "匿名"
        name = name[:4] + "***"
        msg += f"{medals[i]} {name} - {row[2]} 分\n"
    await update.message.reply_text(msg)

async def share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_name = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_name}?start={user.id}"
    await update.message.reply_text(f"🔗 你的专属邀请链接：\n{link}\n\n🎁 成功邀请好友后将自动获得 +10 积分奖励！")

async def run_telegram_bot():
    app_ = ApplicationBuilder().token(BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", start))
    app_.add_handler(CommandHandler("rank", show_rank))
    app_.add_handler(CommandHandler("share", share))
    app_.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app_.add_handler(CallbackQueryHandler(start_game_callback, pattern="^start_game$"))
    app_.add_handler(ChatMemberHandler(lambda u, c: None, ChatMemberHandler.CHAT_MEMBER))  # 可扩展群组事件
    await app_.run_polling(close_loop=False)

def reset_daily():
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET plays = 0")
    conn.commit()
    conn.close()
    print("🔄 已重置每日次数")

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
