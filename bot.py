import os
import sqlite3
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
DB = "creator_manager.db"

# Временное ожидание сообщения от рекламодателя
waiting = {}


def db():
    return sqlite3.connect(DB)


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blogger_id INTEGER,
                title TEXT,
                price TEXT,
                description TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blogger_id INTEGER,
                advertiser_id INTEGER,
                ad_id INTEGER,
                message TEXT,
                status TEXT DEFAULT 'new'
            )
        """)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if args and args[0].startswith("blogger_"):
        try:
            blogger_id = int(args[0].replace("blogger_", ""))
        except ValueError:
            await update.message.reply_text("Некорректная ссылка.")
            return

        with db() as con:
            ads = con.execute(
                "SELECT id, title, price, description FROM ads "
                "WHERE blogger_id = ?",
                (blogger_id,),
            ).fetchall()

        if not ads:
            await update.message.reply_text(
                "У этого блогера пока нет рекламных предложений."
            )
            return

        await update.message.reply_text(
            "Рекламные предложения блогера:\n\n"
            + "\n\n".join(
                f"📌 {title}\n💰 {price}\n{description}"
                for _, title, price, description in ads
            )
            + "\n\nНажми на предложение, чтобы отправить заявку:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"Заявка: {title}",
                        callback_data=f"request_{ad_id}"
                    )
                ]
                for ad_id, title, price, description in ads
            ])
        )
        return

    await update.message.reply_text(
        "👋 Добро пожаловать в Creator Manager!\n\n"
        "Блогер:\n"
        "/addad — добавить рекламное предложение\n"
        "/myads — посмотреть предложения\n"
        "/link — получить личную ссылку\n\n"
        "Рекламодатель может открыть личную ссылку блогера "
        "и отправить заявку.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🚀 Открыть Creator Manager",
                web_app=WebAppInfo(
                    url="https://keril3727-oss.github.io/Creator-Manger/"
                )
            )]
        ])
    )


async def addad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace("/addad", "", 1).strip()
    parts = [part.strip() for part in text.split("|", 2)]

    if len(parts) < 3:
        await update.message.reply_text(
            "Используй формат:\n"
            "/addad Сторис | 50 € | Реклама в сторис"
        )
        return

    title, price, description = parts

    with db() as con:
        con.execute(
            "INSERT INTO ads (blogger_id, title, price, description) "
            "VALUES (?, ?, ?, ?)",
            (update.effective_user.id, title, price, description),
        )

    await update.message.reply_text("✅ Предложение добавлено!")


async def myads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        ads = con.execute(
            "SELECT title, price, description FROM ads "
            "WHERE blogger_id = ?",
            (update.effective_user.id,),
        ).fetchall()

    if not ads:
        await update.message.reply_text(
            "Пока нет предложений. Добавь первое командой /addad"
        )
        return

    text = "\n\n".join(
        f"📌 {title}\n💰 {price}\n{description}"
        for title, price, description in ads
    )
    await update.message.reply_text(text)


async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = await context.bot.get_me()
    user_id = update.effective_user.id
    url = f"https://t.me/{bot.username}?start=blogger_{user_id}"

    await update.message.reply_text(
        "🔗 Твоя личная ссылка для рекламодателей:\n\n"
        f"{url}\n\n"
        "Отправляй её рекламодателям."
    )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("request_"):
        ad_id = int(data.replace("request_", ""))

        with db() as con:
            ad = con.execute(
                "SELECT blogger_id, title, price FROM ads WHERE id = ?",
                (ad_id,),
            ).fetchone()

        if not ad:
            await query.message.reply_text("Предложение не найдено.")
            return

        blogger_id, title, price = ad
        waiting[query.from_user.id] = (blogger_id, ad_id)

        await query.message.reply_text(
            f"Ты выбрал: {title} — {price}\n\n"
            "Напиши одним сообщением, что именно хочешь заказать."
        )
        return

    if data.startswith("accept_") or data.startswith("reject_"):
        request_id = int(data.split("_")[1])
        status = "accepted" if data.startswith("accept_") else "rejected"

        with db() as con:
            request = con.execute(
                "SELECT blogger_id FROM requests WHERE id = ?",
                (request_id,),
            ).fetchone()

            if not request or request[0] != query.from_user.id:
                await query.message.reply_text(
                    "Эта заявка не принадлежит тебе."
                )
                return

            con.execute(
                "UPDATE requests SET status = ? WHERE id = ?",
                (status, request_id),
            )

            advertiser = con.execute(
                "SELECT advertiser_id FROM requests WHERE id = ?",
                (request_id,),
            ).fetchone()[0]

        answer = (
            "✅ Блогер принял твою заявку."
            if status == "accepted"
            else "❌ Блогер отклонил твою заявку."
        )

        await context.bot.send_message(advertiser, answer)
        await query.message.reply_text("Статус заявки обновлён.")


async def receive_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    if user_id not in waiting:
        return

    blogger_id, ad_id = waiting.pop(user_id)
    message = update.message.text

    with db() as con:
        cursor = con.execute(
            "INSERT INTO requests "
            "(blogger_id, advertiser_id, ad_id, message) "
            "VALUES (?, ?, ?, ?)",
            (blogger_id, user_id, ad_id, message),
        )
        request_id = cursor.lastrowid

        ad = con.execute(
            "SELECT title, price FROM ads WHERE id = ?",
            (ad_id,),
        ).fetchone()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Принять",
                callback_data=f"accept_{request_id}"
            ),
            InlineKeyboardButton(
                "❌ Отклонить",
                callback_data=f"reject_{request_id}"
            ),
        ]
    ])

    await context.bot.send_message(
        blogger_id,
        f"📩 Новая рекламная заявка!\n\n"
        f"Предложение: {ad[0]}\n"
        f"Цена: {ad[1]}\n"
        f"Рекламодатель: @{update.effective_user.username or 'без username'}\n\n"
        f"Сообщение:\n{message}",
        reply_markup=keyboard,
    )

    await update.message.reply_text(
        "✅ Заявка отправлена блогеру!"
    )


def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    # Render автоматически предоставляет публичный URL веб-сервиса.
    # WEBHOOK_URL можно задать вручную, но это необязательно.
    base_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if not base_url:
        raise RuntimeError("Не найден адрес сервиса: задайте WEBHOOK_URL или используйте Render Web Service")

    init_db()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addad", addad))
    app.add_handler(CommandHandler("myads", myads))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_message)
    )

    port = int(os.getenv("PORT", "10000"))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path="telegram",
        webhook_url=f"{base_url.rstrip('/')}/telegram",
    )


if __name__ == "__main__":
    main()
