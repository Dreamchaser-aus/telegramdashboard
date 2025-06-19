import os
import logging
import psycopg2
import asyncio
import nest_asyncio
from datetime import datetime, date
from flask import Flask, render_template, request
from apscheduler.schedulers.asyncio import AsyncIOScheduler
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
    # 建议捕获异常并重试或日志
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn, conn.cursor() as c:
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

@app.route("/")
def dashboard():
    keyword = request.args.get("keyword", "")
    with get_conn() as conn, conn.cursor() as c:
        if keyword:
            c.execute("""
                SELECT u.user_id, u.first_name, u.last_name, u.username, u.phone, u.points, u.plays,
                       u.created_at, u.last_play, u.invited_by, u.inviter_rewarded, u.is_blocked,
                       i.username as inviter_username
                FROM users u
                LEFT JOIN users i ON u.invited_by = i.user_id
                WHERE u.username ILIKE %s OR u.phone ILIKE %s
            """, (f"%{keyword}%", f"%{keyword}%"))
        else:
            c.execute("""
                SELECT u.user_id, u.first_name, u.last_name, u.username, u.phone, u.points, u.plays,
                       u.created_at, u.last_play, u.invited_by, u.inviter_rewarded, u.is_blocked,
                       i.username as inviter_username
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
        c.execute("SELECT COALESCE(SUM(points), 0) FROM users")
        total_points = c.fetchone()[0]

    stats = {
        "total_users": total_users,
        "authorized_users": authorized_users,
        "blocked_users": blocked_users,
        "total_points": total_points
    }

    return render_template("dashboard.html", users=users, stats=stats, total_rank=total_rank, today_rank=today_rank)

@app.route("/update_user", methods=["POST"])
def update_user():
    user_id = request.form.get("user_id")
    try:
        points = int(request.form.get("points", 0))
        plays = int(request.form.get("plays", 0))
        is_blocked = int(request.form.get("is_blocked", 0))
    except ValueError:
        return "参数错误", 400

    with get_conn() as conn, conn.cursor() as c:
        c.execute(
            "UPDATE users SET points = %s, plays = %s, is_blocked = %s WHERE user_id = %s",
            (points, plays, is_blocked, user_id)
        )
        conn.commit()
    return "OK"

@app.route("/delete_user", methods=["POST"])
def delete_user():
    user_id = request.form.get("user_id")
    with get_conn() as conn, conn.cursor() as c:
        c.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        conn.commit()
    return "OK"

# Telegram Bot Handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    inviter_id = int(context.args[0]) if context.args else None
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT 1 FROM users WHERE user_id = %s", (user.id,))
        if not c.fetchone():
            now = datetime.now().isoformat()
            c.execute("""
                INSERT INTO users (user_id, first_name, last_name, username, plays, points, created_at, invited_by)
                VALUES (%s, %s, %s, %s, 0, 0, %s, %s)
            """, (user.id, user.first_name, user.last_name, user.username, now, inviter_id))
            conn.commit()

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 分享手机号", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text("⚠️ 为参与群组游戏，请先授权手机号：", reply_markup=keyboard)

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message.contact or update.message.contact.user_id != user.id:
        await update.message.reply_text("⚠️ 请发送您自己的手机号授权。")
        return
    phone = update.message.contact.phone_number
    with get_conn() as conn, conn.cursor() as c:
        c.execute("UPDATE users SET phone = %s WHERE user_id = %s", (phone, user.id))
        conn.commit()

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 开始游戏", callback_data="start_game")]])
    await update.message.reply_text("✅ 手机号授权成功！点击按钮开始游戏吧～", reply_markup=keyboard)
    await reward_inviter(user.id, context)

async def reward_inviter(user_id, context):
    try:
        with get_conn() as conn, conn.cursor() as c:
            c.execute("SELECT invited_by, phone, inviter_rewarded, plays FROM users WHERE user_id = %s", (user_id,))
            row = c.fetchone()
            if row:
                inviter, phone, rewarded, plays = row
                if inviter and phone and not rewarded and plays > 0:
                    c.execute("UPDATE users SET points = points + 10 WHERE user_id = %s", (inviter,))
                    c.execute("UPDATE users SET inviter_rewarded = 1 WHERE user_id = %s", (user_id,))
                    conn.commit()
                    try:
                        await context.bot.send_message(chat_id=inviter, text="🎁 你邀请的用户已成功参与游戏，积分 +10！")
                    except Exception:
                        logging.warning(f"邀请积分通知发送失败，邀请人ID: {inviter}")
    except Exception as e:
        logging.error(f"奖励邀请者失败: {e}")

async def start_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT is_blocked, plays, phone FROM users WHERE user_id = %s", (user.id,))
        row = c.fetchone()

        if not row:
            await query.edit_message_text("⚠️ 你还未授权手机号，请先私聊我发送手机号授权。")
            return
        is_blocked, plays, phone = row
        if is_blocked:
            await query.edit_message_text("⛔️ 你已被禁止参与互动，请联系管理员。")
            return
        if not phone:
            await query.edit_message_text("📵 请先授权手机号后才能参与游戏！")
            return
        if plays >= 10:
            await query.edit_message_text("❌ 今天已用完10次机会，请明天再来！")
            return

    # 删除原消息，开始游戏
    try:
        await query.delete_message()
        dice1 = await context.bot.send_dice(chat_id=query.message.chat_id)
        await asyncio.sleep(3)
        dice2 = await context.bot.send_dice(chat_id=query.message.chat_id)
        await asyncio.sleep(3)
        score = 10 if dice1.dice.value > dice2.dice.value else -5 if dice1.dice.value < dice2.dice.value else 0

        with get_conn() as conn, conn.cursor() as c:
            c.execute("UPDATE users SET points = points + %s, plays = plays + 1, last_play = %s WHERE user_id = %s",
                      (score, datetime.now().isoformat(), user.id))
            c.execute("SELECT points FROM users WHERE user_id = %s", (user.id,))
            total = c.fetchone()[0]
            conn.commit()

        msg = f"🎲 你掷出{dice1.dice.value}，我掷出{dice2.dice.value}！"
        msg += "赢了！+10积分" if score > 0 else "输了... -5积分" if score < 0 else "平局！"
        msg += f" 当前总积分：{total}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 再来一次", callback_data="start_game")]])
        await context.bot.send_message(chat_id=query.message.chat_id, text=msg, reply_markup=keyboard)
    except Exception as e:
        logging.error(f"游戏开始异常: {e}")
        await query.message.reply_text("⚠️ 游戏出错，请稍后再试。")

async def handle_group_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    dice = update.message.dice
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT is_blocked, plays, phone FROM users WHERE user_id = %s", (user.id,))
        row = c.fetchone()

    if not row or not row[2]:
        bot_username = (await context.bot.get_me()).username
        private_link = f"https://t.me/{bot_username}?start={user.id}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 点我授权手机号", url=private_link)]])
        await update.message.reply_text(
            f"📵 @{user.username or user.first_name} 请私聊我授权手机号后才能参与游戏！",
            reply_markup=keyboard
        )
        return

    is_blocked, plays, phone = row
    if is_blocked:
        await update.message.reply_text("⛔️ 你已被禁止参与，请联系管理员。")
        return

    if plays >= 10:
        await update.message.reply_text("❌ 今天已用完10次机会，请明天再来！")
        return

    try:
        bot_msg = await update.message.reply_dice()
        await asyncio.sleep(3)
        user_score, bot_score = dice.value, bot_msg.dice.value
        score = 10 if user_score > bot_score else -5 if user_score < bot_score else 0
        with get_conn() as conn, conn.cursor() as c:
            c.execute("UPDATE users SET points = points + %s, plays = plays + 1, last_play = %s WHERE user_id = %s",
                      (score, datetime.now().isoformat(), user.id))
            c.execute("SELECT points FROM users WHERE user_id = %s", (user.id,))
            total = c.fetchone()[0]
            conn.commit()

        msg = f"🎲 你掷出 {user_score}，我掷出 {bot_score}，"
        msg += "你赢了！+10分" if score > 0 else "你输了～ -5分" if score < 0 else "平局！"
        msg += f" 当前总积分：{total}"
        await update.message.reply_text(msg)
    except Exception as e:
        logging.error(f"群组骰子游戏异常: {e}")
        await update.message.reply_text("⚠️ 游戏异常，请稍后重试。")

async def show_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT username, first_name, points FROM users WHERE last_play LIKE %s ORDER BY points DESC LIMIT 10", (f"{today}%",))
        rows = c.fetchall()

    if not rows:
        await update.message.reply_text("📬 今日暂无玩家积分记录")
        return
    msg = "📊 今日排行榜：\n"
    medals = ["🥇", "🥈", "🥉"] + ["🎖"] * 7
    for i, row in enumerate(rows):
        name = row[0] or row[1] or "匿名"
        msg += f"{medals[i]} {name[:4]}*** - {row[2]} 分\n"
    await update.message.reply_text(msg)

async def share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_name = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_name}?start={user.id}"
    await update.message.reply_text(f"🔗 你的邀请链接：\n{link}\n\n🎁 邀请成功即可获得 +10 积分奖励！")

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = update.chat_member
    inviter = chat_member.from_user
    new_user = chat_member.new_chat_member.user
    if chat_member.old_chat_member.status == "left" and chat_member.new_chat_member.status == "member":
        if new_user.is_bot or inviter.id == new_user.id:
            return
        with get_conn() as conn, conn.cursor() as c:
            c.execute("SELECT 1 FROM users WHERE user_id = %s", (new_user.id,))
            if not c.fetchone():
                now = datetime.now().isoformat()
                c.execute("INSERT INTO users (user_id, username, invited_by, created_at) VALUES (%s, %s, %s, %s)",
                          (new_user.id, new_user.username or '', inviter.id, now))
                conn.commit()

async def run_telegram_bot():
    app_ = ApplicationBuilder().token(BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", start))
    app_.add_handler(CommandHandler("rank", show_rank))
    app_.add_handler(CommandHandler("share", share))
    app_.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app_.add_handler(MessageHandler(filters.Dice.DICE & filters.ChatType.GROUPS, handle_group_dice))
    app_.add_handler(CallbackQueryHandler(start_game_callback, pattern="^start_game$"))
    app_.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))
    await app_.run_polling(close_loop=False)

def reset_daily():
    with get_conn() as conn, conn.cursor() as c:
        c.execute("UPDATE users SET plays = 0")
        conn.commit()
    logging.info("🔄 已重置每日次数")

async def main():
    init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(reset_daily, "cron", hour=0, minute=0)
    scheduler.start()
    config = Config()
    config.bind = ["0.0.0.0:8080"]
    await asyncio.gather(serve(app, config), run_telegram_bot())

if __name__ == "__main__":
    asyncio.run(main())
