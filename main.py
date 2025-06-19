import os
import logging
import psycopg2
import asyncio
import nest_asyncio
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify
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
                is_blocked INTEGER DEFAULT 0
            );
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS invite_rewards (
                invited_user_id BIGINT PRIMARY KEY,
                inviter_user_id BIGINT NOT NULL,
                rewarded_at TEXT
            );
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS game_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                play_time TIMESTAMP NOT NULL,
                user_score INTEGER,
                bot_score INTEGER,
                result TEXT,
                points_change INTEGER
            );
        ''')
        conn.commit()

@app.route("/")
def dashboard():
    keyword = request.args.get("keyword", "").strip()
    invited_by_filter = request.args.get("invited_by", "").strip()
    phone_filter = request.args.get("phone", "").strip()
    is_authorized = request.args.get("authorized", "").strip()
    page = int(request.args.get("page", 1))
    per_page = 50

    where_clauses = []
    params = []

    if keyword:
        where_clauses.append("(u.username ILIKE %s OR u.phone ILIKE %s)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if invited_by_filter:
        where_clauses.append("i.username ILIKE %s")
        params.append(f"%{invited_by_filter}%")
    if phone_filter:
        where_clauses.append("u.phone ILIKE %s")
        params.append(f"%{phone_filter}%")
    if is_authorized == "1":
        where_clauses.append("u.phone IS NOT NULL")
    elif is_authorized == "0":
        where_clauses.append("u.phone IS NULL")

    where_sql = " AND ".join(where_clauses)
    if where_sql:
        where_sql = "WHERE " + where_sql

    with get_conn() as conn, conn.cursor() as c:
        c.execute(f"""
            SELECT COUNT(*)
            FROM users u LEFT JOIN users i ON u.invited_by = i.user_id
            {where_sql}
        """, params)
        total_count = c.fetchone()[0]

        offset = (page - 1) * per_page

        c.execute(f"""
            SELECT u.user_id, u.first_name, u.last_name, u.username, u.phone, u.points, u.plays,
                   u.created_at, u.last_play, u.invited_by, u.is_blocked,
                   i.username as inviter_username
            FROM users u LEFT JOIN users i ON u.invited_by = i.user_id
            {where_sql}
            ORDER BY u.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset])
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
        "total_points": total_points,
        "total_count": total_count,
        "page": page,
        "per_page": per_page,
        "total_pages": (total_count + per_page - 1) // per_page
    }

    return render_template("dashboard.html", users=users, stats=stats, total_rank=total_rank, today_rank=today_rank,
                           keyword=keyword, invited_by_filter=invited_by_filter, phone_filter=phone_filter,
                           is_authorized=is_authorized)

@app.route('/update_block_status', methods=['POST'])
def update_block_status():
    data = request.get_json()
    user_id = data.get('user_id')
    is_blocked = data.get('is_blocked')
    if user_id is None or is_blocked not in ['0','1']:
        return jsonify(success=False), 400
    try:
        with get_conn() as conn, conn.cursor() as c:
            c.execute("UPDATE users SET is_blocked = %s WHERE user_id = %s", (int(is_blocked), int(user_id)))
            conn.commit()
        return jsonify(success=True)
    except Exception as e:
        logging.error(f"更新封禁状态失败: {e}")
        return jsonify(success=False), 500

@app.route("/game_history")
def game_history():
    user_id = request.args.get("user_id")
    page = int(request.args.get("page", 1))
    per_page = 50

    where_sql = ""
    params = []

    if user_id:
        where_sql = "WHERE user_id = %s"
        params.append(user_id)

    with get_conn() as conn, conn.cursor() as c:
        c.execute(f"SELECT COUNT(*) FROM game_history {where_sql}", params)
        total_count = c.fetchone()[0]

        offset = (page - 1) * per_page

        c.execute(f"""
            SELECT user_id, play_time, user_score, bot_score, result, points_change
            FROM game_history
            {where_sql}
            ORDER BY play_time DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset])

        records = c.fetchall()

    total_pages = (total_count + per_page - 1) // per_page

    return render_template("game_history.html",
                           records=records,
                           page=page,
                           total_pages=total_pages,
                           user_id=user_id)

async def send_game_rules(chat_id, bot, language_code='zh'):
    if language_code and language_code.startswith('en'):
        text = (
            "🎲 Game Rules:\n"
            "1. Click the button or send a dice to start.\n"
            "2. You and the bot each roll a dice, higher score wins.\n"
            "3. Win: +10 points, Lose: -5 points, Tie: no change.\n"
            "4. You can play up to 10 times per day.\n"
            "5. Phone number authorization is required.\n"
            "6. Invite friends to earn bonus points!\n"
            "Good luck and have fun!"
        )
    else:
        text = (
            "🎲 游戏玩法说明：\n"
            "1. 通过点击按钮或发送骰子开始游戏。\n"
            "2. 你和Bot各掷一次骰子，点数大者获胜。\n"
            "3. 赢得 +10 积分，输掉 -5 积分，平局不加减。\n"
            "4. 每天最多可以玩10次。\n"
            "5. 授权手机号后方可参与游戏。\n"
            "6. 邀请好友可获得额外积分奖励！\n"
            "祝你游戏愉快！"
        )
    await bot.send_message(chat_id=chat_id, text=text)

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_lang = query.from_user.language_code or 'zh'
    await send_game_rules(query.message.chat_id, context.bot, user_lang)

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
    await update.message.reply_text("ℹ️ 想了解游戏玩法，请发送 /help 查看详细说明。")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_lang = update.effective_user.language_code or 'zh'
    await send_game_rules(update.message.chat_id, context.bot, user_lang)

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
            c.execute("SELECT invited_by, phone, plays FROM users WHERE user_id = %s", (user_id,))
            row = c.fetchone()
            if not row:
                return
            inviter, phone, plays = row
            if not inviter or not phone or plays == 0:
                return

            c.execute("SELECT 1 FROM invite_rewards WHERE invited_user_id = %s", (user_id,))
            if c.fetchone():
                return

            c.execute("UPDATE users SET points = points + 10 WHERE user_id = %s RETURNING points", (inviter,))
            inviter_points = c.fetchone()[0]

            c.execute(
                "INSERT INTO invite_rewards (invited_user_id, inviter_user_id, rewarded_at) VALUES (%s, %s, %s)",
                (user_id, inviter, datetime.now().isoformat())
            )
            conn.commit()

            try:
                await context.bot.send_message(
                    chat_id=inviter,
                    text=(
                        f"🎉 你邀请的用户成功参与游戏，获得 +10 积分奖励！\n"
                        f"🏆 当前总积分：{inviter_points}\n"
                        "继续邀请更多好友，积分越多越精彩！"
                    )
                )
            except Exception as e:
                logging.warning(f"邀请积分通知发送失败，邀请人ID: {inviter}, 错误: {e}")

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

            result_str = "win" if score > 0 else "lose" if score < 0 else "draw"
            c.execute("""
                INSERT INTO game_history (user_id, play_time, user_score, bot_score, result, points_change)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user.id, datetime.now(), dice1.dice.value, dice2.dice.value, result_str, score))

            conn.commit()

        await reward_inviter(user.id, context)

        if score > 0:
            result_emoji = "🎉🎉🎉"
            result_text = f"你赢了！+10积分 {result_emoji}"
        elif score < 0:
            result_emoji = "😞💔"
            result_text = f"你输了... -5积分 {result_emoji}"
        else:
            result_emoji = "😐"
            result_text = f"平局！ {result_emoji}"

        msg = (
            f"🎲 你掷出 {dice1.dice.value}，我掷出 {dice2.dice.value}！\n"
            f"{result_text}\n"
            f"📊 当前总积分：{total}"
        )

        help_button = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❓ 玩法说明", callback_data="help_rules")]]
        )
        await context.bot.send_message(chat_id=query.message.chat_id, text=msg, reply_markup=help_button)
    except Exception as e:
        logging.error(f"游戏开始异常: {e}")
        await query.message.reply_text("⚠️ 游戏出错，请稍后再试。")

def reset_daily():
    with get_conn() as conn, conn.cursor() as c:
        c.execute("UPDATE users SET plays = 0")
        conn.commit()
    logging.info("🔄 已重置每日次数")

async def run_telegram_bot():
    app_ = ApplicationBuilder().token(BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", start))
    app_.add_handler(CommandHandler("help", help_command))
    app_.add_handler(CommandHandler("profile", profile))
    app_.add_handler(CommandHandler("invite", invite))
    app_.add_handler(CommandHandler("rank", show_rank))
    app_.add_handler(CommandHandler("share", share))
    app_.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app_.add_handler(MessageHandler(filters.Dice.DICE & filters.ChatType.GROUPS, handle_group_dice))
    app_.add_handler(CallbackQueryHandler(start_game_callback, pattern="^start_game$"))
    app_.add_handler(CallbackQueryHandler(help_callback, pattern="^help_rules$"))
    app_.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))
    await app_.run_polling(close_loop=False)

async def main():
    init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(reset_daily, "cron", hour=0, minute=0)
    scheduler.start()
    config = Config()
    config.bind = ["0.0.0.0:8080"]
    web_task = asyncio.create_task(serve(app, config))
    bot_task = asyncio.create_task(run_telegram_bot())
    await asyncio.gather(web_task, bot_task)

if __name__ == "__main__":
    asyncio.run(main())
