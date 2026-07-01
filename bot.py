# -*- coding: utf-8 -*-
"""
Бот «Хи-хик» — учёт внутришуточной валюты дружеской компании.

Установка:
    pip install -r requirements.txt

Запуск:
    python bot.py

Перед запуском обязательно заполните блок НАСТРОЙКИ ниже.
"""

import logging
import os
import random
import sqlite3
import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================== НАСТРОЙКИ =====================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# В Railway: Variables -> ADMIN_IDS = "123456789,987654321" (через запятую, без пробелов)
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

# В Railway: Variables -> YULIA_ID = "987654321"
YULIA_ID = int(os.environ.get("YULIA_ID", "0"))

# ID группового чата, куда бот будет каждый день постить таблицу балансов.
# Как узнать: добавь бота в группу, напиши там что угодно, затем
# в Railway -> Variables временно не задавай эту переменную и посмотри
# в логах Console/Deploy строку с chat id, либо перешли сообщение из
# группы боту @userinfobot — для групп он тоже покажет ID (отрицательное число).
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")

TIMEZONE = ZoneInfo("Asia/Almaty")

DB_PATH = "hihik.db"

# --- Экономические константы (можно менять под себя) ---
DAILY_BASE = 1                 # базовое количество хи-хиков в день
DAILY_YULIA = 4                # количество хи-хиков в день у Юли
UPGRADE1_DAILY_BONUS = 1       # доп. хи-хик в день при апгрейде 1 (итого 2/день)
UPGRADE1_COST = 30             # цена апгрейда 1 в хи-хиках
UPGRADE2_COST = 1              # цена апгрейда 2 в хи-хиках (одноразовый заряд кражи)
STEAL_BONUS = 1                # доп. хи-хик, который ворует обладатель апгрейда 2
HIHIK_PRICE_KZT = 50           # цена покупки одного хи-хика за реальные деньги
FINE_MIN_KZT = 50              # минимальный штраф за использование хи-хика
FINE_DEADLINE_HOURS = 48       # срок оплаты штрафа

# Шансы и выплаты в казино
COIN_WIN_CHANCE = 0.5          # орёл/решка — честная монета
WHEEL_RED_CHANCE = 0.45
WHEEL_BLACK_CHANCE = 0.45
WHEEL_GREEN_CHANCE = 0.10
WHEEL_RED_BLACK_PAYOUT = 2     # ставка x2
WHEEL_GREEN_PAYOUT = 6         # ставка x6

# =========================== /НАСТРОЙКИ =====================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ----------------------------- БАЗА ДАННЫХ ----------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            balance INTEGER NOT NULL DEFAULT 0,
            upgrade1 INTEGER NOT NULL DEFAULT 0,
            upgrade2_charges INTEGER NOT NULL DEFAULT 0,
            debt_kzt REAL NOT NULL DEFAULT 0,
            debt_deadline TEXT,
            debt_confirmed INTEGER NOT NULL DEFAULT 1,
            last_daily_date TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS charity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount_kzt REAL,
            confirmed_by INTEGER,
            ts TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def ensure_user(user_id, username, display_name):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (user_id, username, display_name) VALUES (?,?,?)",
            (user_id, username, display_name),
        )
        conn.commit()
    else:
        conn.execute(
            "UPDATE users SET username=?, display_name=? WHERE user_id=?",
            (username, display_name, user_id),
        )
        conn.commit()
    conn.close()


def update_user(user_id, **fields):
    if not fields:
        return
    conn = db()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [user_id]
    conn.execute(f"UPDATE users SET {set_clause} WHERE user_id=?", values)
    conn.commit()
    conn.close()


def all_users():
    conn = db()
    rows = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return rows


def name_for(row):
    if row["username"]:
        return f"@{row['username']}"
    return row["display_name"] or str(row["user_id"])


# ----------------------------- ВСПОМОГАТЕЛЬНОЕ -------------------------------

def now_str():
    return datetime.datetime.now(TIMEZONE).isoformat()


def today_str():
    return datetime.datetime.now(TIMEZONE).date().isoformat()


def is_admin(user_id):
    return user_id in ADMIN_IDS


async def resolve_target_user(update: Update, context):
    """Определяет, на кого направлено действие: через ответ на сообщение
    или через @username первым аргументом."""
    if update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
        ensure_user(u.id, u.username, u.full_name)
        return get_user(u.id)
    if context.args:
        uname = context.args[0].lstrip("@")
        conn = db()
        row = conn.execute("SELECT * FROM users WHERE username=?", (uname,)).fetchone()
        conn.close()
        return row
    return None


# ----------------------------- КОМАНДЫ ---------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username, u.full_name)
    text = (
        "👋 Привет! Это бот учёта хи-хиков — нашей внутришуточной валюты.\n\n"
        "Правила:\n\n"
        "• Юля получает 4 хи-хика в день, так как именно она их производит.\n"
        "• Все остальные получают по 1 хи-хику в день.\n\n"
        "Команды:\n\n"
        "💼 /balance — посмотреть свой баланс, апгрейды и задолженности.\n\n"
        "😂 /hihik (ответом на сообщение) — отдать хи-хик человеку, который "
        "заставил тебя сказать «хи-хик».\n"
        "С твоего счёта списывается 1 хи-хик и зачисляется получателю.\n\n"
        "Если у тебя есть хи-хики на балансе — всё бесплатно. Если баланс "
        "пуст (берёшь хи-хик «в долг»), ты обязан оплатить штраф "
        f"(от {FINE_MIN_KZT} ₸) в течение {FINE_DEADLINE_HOURS} часов.\n\n"
        "💸 /pay <сумма> — задекларировать оплату штрафа. Платёж будет "
        "ожидать подтверждения администратора.\n\n"
        "🛒 /shop — магазин апгрейдов и покупки хи-хиков.\n\n"
        f"• /buyupgrade1 — получать 2 хи-хика в день вместо одного навсегда "
        f"({UPGRADE1_COST} хи-хиков).\n"
        f"• /buyupgrade2 — один раз украсть 2 хи-хика вместо 1 у случайного "
        f"участника во время транзакции ({UPGRADE2_COST} хи-хик).\n"
        f"• /buyhihik <количество> — купить хи-хики за деньги "
        f"({HIHIK_PRICE_KZT} ₸ за штуку).\n\n"
        "🎲 Хи-хик-казино — поставь свои хи-хики на кон.\n\n"
        "• /casinocoin <ставка> — орёл или решка.\n"
        "• /casinowheel <ставка> <red/black/green> — колесо рулетки.\n\n"
        "🏆 /top — таблица лидеров.\n\n"
        "💚 /charity — посмотреть, сколько всего собрано на "
        "благотворительность. Все деньги, полученные со штрафов и покупки "
        "хи-хиков, направляются на благотворительность.\n\n"
        "Команды для администраторов:\n\n"
        "/confirmpayment (ответом на сообщение или через @user) <сумма> — "
        "подтвердить оплату штрафа или покупку хи-хиков.\n"
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username, u.full_name)
    row = get_user(u.id)
    debt_line = ""
    if row["debt_kzt"] and not row["debt_confirmed"]:
        deadline = row["debt_deadline"]
        debt_line = f"\n⚠️ Долг: {row['debt_kzt']:.0f} ₸, оплатить до {deadline}"
    text = (
        f"💼 Баланс {name_for(row)}: *{row['balance']}* хи-хик(ов)\n"
        f"Апгрейд 1 (2/день): {'✅' if row['upgrade1'] else '❌'}\n"
        f"Апгрейд 2 (заряды кражи): {row['upgrade2_charges']}"
        f"{debt_line}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def hihik_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username, u.full_name)
    actor = get_user(u.id)

    target = await resolve_target_user(update, context)
    if target is None:
        await update.message.reply_text(
            "Ответь этой командой на сообщение друга, который тебя рассмешил 😄"
        )
        return
    if target["user_id"] == actor["user_id"]:
        await update.message.reply_text("Самого себя рассмешил? Бывает, но так нельзя 😅")
        return

    # Проверяем, есть ли непогашенный штраф — нельзя тратить дальше, пока не оплачено
    if actor["debt_kzt"] and not actor["debt_confirmed"]:
        await update.message.reply_text(
            "⛔ У тебя есть неподтверждённый штраф. Сначала оплати его (/pay), "
            "иначе новые хи-хики использовать нельзя."
        )
        return

    # Апгрейд 2 принадлежит тому, кто спровоцировал хи-хик (target), а не
    # тому, кто его использует. Если у target есть заряд — он ворует ещё
    # STEAL_BONUS хи-хик(ов) сверх обычной 1 штуки.
    steal_active = target["upgrade2_charges"] > 0
    steal_bonus = STEAL_BONUS if steal_active else 0
    amount = 1 + steal_bonus

    new_actor_balance = actor["balance"] - amount
    new_target_balance = target["balance"] + amount

    # Штраф начисляется, если в результате этой транзакции у actor баланс
    # ушёл в минус — то есть он либо уже был на нуле, либо кражи "добили"
    # его баланс ниже нуля.
    borrowing = new_actor_balance < 0

    update_fields = {"balance": new_actor_balance}
    target_update_fields = {"balance": new_target_balance}
    if steal_active:
        target_update_fields["upgrade2_charges"] = target["upgrade2_charges"] - 1

    fine_note = ""
    if borrowing:
        deadline = (datetime.datetime.now(TIMEZONE) + datetime.timedelta(hours=FINE_DEADLINE_HOURS)).isoformat()
        update_fields.update(debt_kzt=FINE_MIN_KZT, debt_deadline=deadline, debt_confirmed=0)
        fine_note = (
            f"\n\n💸 Баланс {name_for(actor)} ушёл в минус, так что это хи-хик \"в долг\"! "
            f"Оплати штраф (минимум {FINE_MIN_KZT} ₸, можно больше) "
            f"в течение {FINE_DEADLINE_HOURS} часов командой /pay <сумма>. "
            f"Не успеешь — все твои хи-хики обнулятся!"
        )

    update_user(actor["user_id"], **update_fields)
    update_user(target["user_id"], **target_update_fields)

    steal_note = (
        f"\n🕵️ {name_for(target)} применил(а) апгрейд 2 и украл(а) ещё "
        f"{steal_bonus} хи-хик сверху!"
    ) if steal_active else ""

    await update.message.reply_text(
        f"😂 Хи-хик засчитан!\n"
        f"{name_for(actor)} → {name_for(target)}: −{amount} / +{amount}"
        f"{steal_note}"
        f"{fine_note}"
    )


async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username, u.full_name)
    row = get_user(u.id)

    if not row["debt_kzt"] or row["debt_confirmed"]:
        await update.message.reply_text("За тобой сейчас нет неоплаченных штрафов 👍")
        return

    if not context.args:
        await update.message.reply_text(f"Укажи сумму: /pay <сумма от {FINE_MIN_KZT}>")
        return

    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом.")
        return

    if amount < FINE_MIN_KZT:
        await update.message.reply_text(f"Минимальная сумма штрафа — {FINE_MIN_KZT} ₸.")
        return

    update_user(u.id, debt_kzt=amount)
    await update.message.reply_text(
        f"✅ Заявлена оплата {amount:.0f} ₸. Жду подтверждения от администратора "
        f"(перевод нужно сделать вне бота — например, скинуть админу/казначею)."
    )


async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if not is_admin(admin.id):
        await update.message.reply_text("Эта команда только для админов.")
        return

    target = await resolve_target_user(update, context)
    if target is None:
        await update.message.reply_text(
            "Ответь этой командой на сообщение должника или укажи @username."
        )
        return

    amount = target["debt_kzt"]
    if context.args:
        # последний аргумент может быть суммой, если был указан @username отдельно
        try:
            amount = float(context.args[-1])
        except ValueError:
            pass

    if not amount or amount <= 0:
        await update.message.reply_text("У этого участника нет долга к подтверждению.")
        return

    update_user(target["user_id"], debt_kzt=0, debt_deadline=None, debt_confirmed=1)

    conn = db()
    conn.execute(
        "INSERT INTO charity_log (user_id, amount_kzt, confirmed_by, ts) VALUES (?,?,?,?)",
        (target["user_id"], amount, admin.id, now_str()),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Оплата {amount:.0f} ₸ от {name_for(target)} подтверждена и зачислена "
        f"в фонд благотворительности. /charity"
    )


async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛒 *Магазин*\n\n"
        f"1️⃣ Апгрейд 1 — 2 хи-хика в день вместо одного. Цена: {UPGRADE1_COST} хи-хик(ов).\n"
        f"   Купить: /buyupgrade1\n\n"
        f"2️⃣ Апгрейд 2 — разовая кража: когда кто-то в следующий раз использует "
        f"/hihik против *тебя*, ты украдёшь у него ещё {STEAL_BONUS} хи-хик(ов) сверху "
        f"обычной 1 штуки. Цена: {UPGRADE2_COST} хи-хик(ов).\n"
        f"   Купить: /buyupgrade2\n\n"
        f"💵 Купить хи-хики за деньги — {HIHIK_PRICE_KZT} ₸ за штуку.\n"
        f"   Запросить: /buyhihik <количество>\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def buy_upgrade1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username, u.full_name)
    row = get_user(u.id)
    if row["upgrade1"]:
        await update.message.reply_text("У тебя уже есть апгрейд 1.")
        return
    if row["balance"] < UPGRADE1_COST:
        await update.message.reply_text(
            f"Нужно {UPGRADE1_COST} хи-хик(ов), у тебя {row['balance']}."
        )
        return
    update_user(u.id, balance=row["balance"] - UPGRADE1_COST, upgrade1=1)
    await update.message.reply_text("🎉 Апгрейд 1 куплен! Теперь ты получаешь 2 хи-хика в день.")


async def buy_upgrade2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username, u.full_name)
    row = get_user(u.id)
    if row["balance"] < UPGRADE2_COST:
        await update.message.reply_text(
            f"Нужно {UPGRADE2_COST} хи-хик(ов), у тебя {row['balance']}."
        )
        return
    update_user(
        u.id,
        balance=row["balance"] - UPGRADE2_COST,
        upgrade2_charges=row["upgrade2_charges"] + 1,
    )
    await update.message.reply_text(
        f"🎉 Апгрейд 2 куплен! В следующий раз, когда кто-то использует /hihik "
        f"против тебя, ты украдёшь у него ещё {STEAL_BONUS} хи-хик сверху."
    )


async def buy_hihik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username, u.full_name)
    if not context.args:
        await update.message.reply_text("Укажи количество: /buyhihik <число>")
        return
    try:
        qty = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Количество должно быть целым числом.")
        return
    if qty <= 0:
        await update.message.reply_text("Количество должно быть больше нуля.")
        return

    cost = qty * HIHIK_PRICE_KZT
    row = get_user(u.id)
    update_user(u.id, balance=row["balance"] + qty, debt_kzt=row["debt_kzt"] + cost, debt_confirmed=0,
                debt_deadline=(datetime.datetime.now(TIMEZONE) + datetime.timedelta(hours=FINE_DEADLINE_HOURS)).isoformat())
    await update.message.reply_text(
        f"🛍 Начислено {qty} хи-хик(ов). С тебя {cost:.0f} ₸ — переведи администратору, "
        f"после чего он подтвердит оплату командой /confirmpayment."
    )


async def casino_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username, u.full_name)
    row = get_user(u.id)
    if not context.args:
        await update.message.reply_text("Укажи ставку: /casinocoin <ставка>")
        return
    try:
        bet = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Ставка должна быть целым числом.")
        return
    if bet <= 0 or bet > row["balance"]:
        await update.message.reply_text(f"Некорректная ставка. У тебя {row['balance']} хи-хик(ов).")
        return

    win = random.random() < COIN_WIN_CHANCE
    if win:
        update_user(u.id, balance=row["balance"] + bet)
        await update.message.reply_text(f"🪙 Орёл! Ты выиграл {bet} хи-хик(ов).")
    else:
        update_user(u.id, balance=row["balance"] - bet)
        await update.message.reply_text(f"🪙 Решка. Ты проиграл {bet} хи-хик(ов).")


async def casino_wheel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username, u.full_name)
    row = get_user(u.id)
    if len(context.args) < 2:
        await update.message.reply_text("Используй: /casinowheel <ставка> <red/black/green>")
        return
    try:
        bet = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Ставка должна быть целым числом.")
        return
    color = context.args[1].lower()
    if color not in ("red", "black", "green"):
        await update.message.reply_text("Цвет должен быть red, black или green.")
        return
    if bet <= 0 or bet > row["balance"]:
        await update.message.reply_text(f"Некорректная ставка. У тебя {row['balance']} хи-хик(ов).")
        return

    roll = random.random()
    if roll < WHEEL_RED_CHANCE:
        result = "red"
    elif roll < WHEEL_RED_CHANCE + WHEEL_BLACK_CHANCE:
        result = "black"
    else:
        result = "green"

    if color == result:
        payout = WHEEL_GREEN_PAYOUT if result == "green" else WHEEL_RED_BLACK_PAYOUT
        winnings = bet * payout - bet
        update_user(u.id, balance=row["balance"] + winnings)
        await update.message.reply_text(
            f"🎡 Выпало {result.upper()}! Ты угадал и выиграл {winnings} хи-хик(ов) "
            f"(шансы: red {WHEEL_RED_CHANCE*100:.0f}%, black {WHEEL_BLACK_CHANCE*100:.0f}%, "
            f"green {WHEEL_GREEN_CHANCE*100:.0f}%)."
        )
    else:
        update_user(u.id, balance=row["balance"] - bet)
        await update.message.reply_text(f"🎡 Выпало {result.upper()}. Ты проиграл {bet} хи-хик(ов).")


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = sorted(all_users(), key=lambda r: r["balance"], reverse=True)
    if not users:
        await update.message.reply_text("Пока никто не зарегистрирован.")
        return
    lines = ["🏆 *Таблица лидеров:*"]
    for i, row in enumerate(users, start=1):
        lines.append(f"{i}. {name_for(row)} — {row['balance']} хи-хик(ов)")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def charity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    total = conn.execute("SELECT SUM(amount_kzt) as t FROM charity_log").fetchone()["t"] or 0
    conn.close()
    await update.message.reply_text(
        f"💚 Всего собрано на благотворительность: *{total:.0f} ₸*\n\n"
        f"Все деньги от штрафов и покупки хи-хиков идут на благотворительность. "
        f"Бот не принимает платежи напрямую — переводы делаются вручную "
        f"администратору/казначею, а он подтверждает оплату в боте.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ----------------------------- ФОНОВЫЕ ЗАДАЧИ --------------------------------

async def daily_distribution(context: ContextTypes.DEFAULT_TYPE):
    today = today_str()
    for row in all_users():
        if row["last_daily_date"] == today:
            continue
        base = DAILY_YULIA if row["user_id"] == YULIA_ID else DAILY_BASE
        bonus = UPGRADE1_DAILY_BONUS if row["upgrade1"] else 0
        update_user(
            row["user_id"],
            balance=row["balance"] + base + bonus,
            last_daily_date=today,
        )
    logger.info("Ежедневная раздача хи-хиков выполнена.")


async def daily_balance_announcement(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет в групповой чат список друзей с их балансами хи-хиков.
    Запускается каждый день в 10:00 по времени Алматы."""
    if not GROUP_CHAT_ID:
        return
    users = sorted(all_users(), key=lambda r: r["balance"], reverse=True)
    if not users:
        return
    lines = ["☀️ *Балансы хи-хиков на сегодня:*"]
    for row in users:
        lines.append(f"• {name_for(row)} — {row['balance']} хи-хик(ов)")
    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text="\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        logger.exception("Не удалось отправить ежедневную сводку в групповой чат.")


async def check_overdue_fines(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now(TIMEZONE)
    for row in all_users():
        if row["debt_kzt"] and not row["debt_confirmed"] and row["debt_deadline"]:
            deadline = datetime.datetime.fromisoformat(row["debt_deadline"])
            if now > deadline:
                update_user(row["user_id"], balance=0, debt_kzt=0, debt_deadline=None, debt_confirmed=1)
                logger.info(f"Хи-хики {name_for(row)} обнулены за просрочку штрафа.")


# ----------------------------------- MAIN ------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Переменная BOT_TOKEN не задана! В Railway: Variables -> добавь BOT_TOKEN."
        )
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("hihik", hihik_use))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("confirmpayment", confirm_payment))
    app.add_handler(CommandHandler("shop", shop))
    app.add_handler(CommandHandler("buyupgrade1", buy_upgrade1))
    app.add_handler(CommandHandler("buyupgrade2", buy_upgrade2))
    app.add_handler(CommandHandler("buyhihik", buy_hihik))
    app.add_handler(CommandHandler("casinocoin", casino_coin))
    app.add_handler(CommandHandler("casinowheel", casino_wheel))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("charity", charity))

    job_queue = app.job_queue
    job_queue.run_daily(
        daily_distribution,
        time=datetime.time(hour=0, minute=0, tzinfo=TIMEZONE),
    )
    job_queue.run_daily(
        daily_balance_announcement,
        time=datetime.time(hour=10, minute=0, tzinfo=TIMEZONE),
    )
    job_queue.run_repeating(check_overdue_fines, interval=3600, first=10)

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
