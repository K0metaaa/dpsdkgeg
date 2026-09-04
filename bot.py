"""
Telegram-бот для создания и редактирования страниц Telegraph с фото-обложкой.

Как это работает:
1. /newpost — бот спрашивает заголовок страницы.
2. Затем просит прислать фото-обложку (оно будет самым верхним изображением на странице).
3. Затем можно присылать ещё фото по одному — они добавятся друг за другом.
4. /done — бот собирает все фото, загружает их на ImgBB (получает прямые ссылки)
   и создаёт страницу Telegraph, присылая готовую ссылку.
5. /editpost — присылаешь ссылку на уже созданную этим ботом страницу, можно
   добавить ещё фото, /save — бот обновляет страницу тем же контентом + новыми фото.

Доступ к боту есть только у одного Telegram user_id (см. ALLOWED_USER_ID ниже).

Установка зависимостей:
    pip install python-telegram-bot==21.6 telegraph requests aiosqlite

Переменные окружения (или впиши значения прямо в код ниже):
    BOT_TOKEN       — токен бота от @BotFather
    IMGBB_API_KEY   — ключ API с https://api.imgbb.com/ (бесплатная регистрация)

Запуск:
    python bot.py
"""

import json
import logging
import os

import aiosqlite
import requests
from telegraph import Telegraph
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_БОТА")
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "ВСТАВЬ_СЮДА_КЛЮЧ_IMGBB")

# Единственный пользователь, которому разрешено пользоваться ботом
ALLOWED_USER_ID = 7618155425

SIGNATURE_NAME = "ПУБЕРТАТНИК"
SIGNATURE_URL = "https://t.me/pubernik"

# БД, где хранится контент уже созданных страниц (нужен для редактирования,
# так как Telegraph API перезаписывает страницу целиком, а не частями)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages_storage.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния диалогов
TITLE, COVER, PHOTOS = range(3)
EDIT_PATH, EDIT_PHOTOS = range(3, 5)

# Фильтр: только разрешённый пользователь
ALLOWED_FILTER = filters.User(user_id=ALLOWED_USER_ID)

# Аккаунт Telegraph создаётся один раз при старте бота
telegraph = Telegraph()
telegraph.create_account(short_name="TelegramPhotoBot")


# ---------------------------------------------------------------------------
# Хранилище страниц (для последующего редактирования) — асинхронный SQLite
# ---------------------------------------------------------------------------

async def init_db(application: Application) -> None:
    """Вызывается один раз при старте приложения (Application.post_init)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pages (
                path TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.commit()
    logger.info("SQLite-хранилище готово: %s", DB_PATH)


async def store_page(path: str, title: str, content_nodes: list) -> None:
    content_json = json.dumps(content_nodes, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO pages (path, title, content_json, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(path) DO UPDATE SET
                title = excluded.title,
                content_json = excluded.content_json,
                updated_at = excluded.updated_at
            """,
            (path, title, content_json),
        )
        await db.commit()


async def get_stored_page(path: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT title, content_json FROM pages WHERE path = ?", (path,)
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return {"title": row["title"], "content": json.loads(row["content_json"])}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def upload_to_imgbb(photo_bytes: bytes) -> str:
    """Загружает байты фото на ImgBB и возвращает прямую ссылку на картинку."""
    response = requests.post(
        "https://api.imgbb.com/1/upload",
        params={"key": IMGBB_API_KEY},
        files={"image": photo_bytes},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"ImgBB вернул ошибку: {data}")
    return data["data"]["url"]


async def download_photo(update: Update) -> bytes:
    """Скачивает фото наибольшего размера из сообщения пользователя."""
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    file_bytes = await tg_file.download_as_bytearray()
    return bytes(file_bytes)


def extract_path_from_link(text: str) -> str:
    """Достаёт path страницы из ссылки вида https://telegra.ph/Some-Title-01-01
    или просто принимает уже готовый path."""
    text = text.strip()
    if "telegra.ph/" in text:
        return text.split("telegra.ph/", 1)[1].strip("/")
    return text.lstrip("/")


def signature_node() -> dict:
    """Нода-подпись, которая всегда добавляется в конец страницы."""
    return {
        "tag": "p",
        "children": [
            {"tag": "b", "children": ["Перевёл "]},
            {
                "tag": "a",
                "attrs": {"href": SIGNATURE_URL},
                "children": [{"tag": "b", "children": [SIGNATURE_NAME]}],
            },
        ],
    }


def build_final_content(content_nodes: list) -> list:
    """Контент страницы + подпись в конце."""
    return content_nodes + [signature_node()]


# ---------------------------------------------------------------------------
# Ограничение доступа
# ---------------------------------------------------------------------------

async def reject_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("Доступ запрещён.")
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# Хэндлеры: создание страницы
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я создаю страницы Telegraph с фотографиями.\n\n"
        "Команды:\n"
        "/newpost — начать создание новой страницы\n"
        "/done — завершить и опубликовать страницу\n"
        "/editpost — отредактировать уже созданную страницу (добавить фото)\n"
        "/cancel — отменить"
    )


async def newpost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["photos"] = []
    await update.message.reply_text("Введи название (заголовок) страницы:")
    return TITLE


async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        "Отлично! Теперь пришли фото для обложки — оно станет самым верхним "
        "изображением на странице."
    )
    return COVER


async def get_cover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно прислать именно фото. Попробуй ещё раз.")
        return COVER

    await update.message.reply_text("Загружаю обложку…")
    try:
        photo_bytes = await download_photo(update)
        url = upload_to_imgbb(photo_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка загрузки обложки")
        await update.message.reply_text(f"Не удалось загрузить фото: {exc}")
        return COVER

    context.user_data["cover"] = url
    await update.message.reply_text(
        "Обложка загружена!\n"
        "Теперь присылай остальные фото по одному — они добавятся друг за другом.\n"
        "Когда закончишь, отправь /done"
    )
    return PHOTOS


async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Пришли фото или команду /done.")
        return PHOTOS

    try:
        photo_bytes = await download_photo(update)
        url = upload_to_imgbb(photo_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка загрузки фото")
        await update.message.reply_text(f"Не удалось загрузить фото: {exc}")
        return PHOTOS

    context.user_data["photos"].append(url)
    count = len(context.user_data["photos"])
    await update.message.reply_text(
        f"Фото добавлено ({count} шт.). Присылай ещё или отправь /done"
    )
    return PHOTOS


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = context.user_data.get("title") or "Без названия"
    cover = context.user_data.get("cover")
    photos = context.user_data.get("photos", [])

    if not cover and not photos:
        await update.message.reply_text(
            "Нет ни одного фото. Начни заново командой /newpost."
        )
        return ConversationHandler.END

    content_nodes = []
    if cover:
        content_nodes.append({"tag": "img", "attrs": {"src": cover}})
    for url in photos:
        content_nodes.append({"tag": "img", "attrs": {"src": url}})

    await update.message.reply_text("Публикую страницу…")

    try:
        page = telegraph.create_page(
            title=title,
            content=build_final_content(content_nodes),
            author_name=SIGNATURE_NAME,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка создания страницы Telegraph")
        await update.message.reply_text(f"Не удалось создать страницу: {exc}")
        return ConversationHandler.END

    # Сохраняем контент БЕЗ подписи — подпись всегда добавляется заново при
    # публикации/редактировании, чтобы не дублировалась.
    await store_page(page["path"], title, content_nodes)

    page_url = f"https://telegra.ph/{page['path']}"
    await update.message.reply_text(f"Готово! Твоя страница:\n{page_url}")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Хэндлеры: редактирование страницы
# ---------------------------------------------------------------------------

async def editpost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Пришли ссылку на страницу (например https://telegra.ph/Название-01-01), "
        "которую нужно отредактировать."
    )
    return EDIT_PATH


async def get_edit_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    path = extract_path_from_link(update.message.text)
    stored = await get_stored_page(path)

    if not stored:
        await update.message.reply_text(
            "Эта страница не найдена среди созданных этим ботом. "
            "Проверь ссылку или создай новую через /newpost."
        )
        return EDIT_PATH

    context.user_data["edit_path"] = path
    context.user_data["edit_title"] = stored["title"]
    context.user_data["edit_content"] = list(stored["content"])

    await update.message.reply_text(
        f"Страница «{stored['title']}» найдена, сейчас в ней "
        f"{len(stored['content'])} изображени(й).\n"
        "Присылай новые фото, которые нужно добавить в конец, а когда закончишь — "
        "отправь /save. Или /cancel, чтобы отменить."
    )
    return EDIT_PHOTOS


async def get_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Пришли фото или команду /save.")
        return EDIT_PHOTOS

    try:
        photo_bytes = await download_photo(update)
        url = upload_to_imgbb(photo_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка загрузки фото при редактировании")
        await update.message.reply_text(f"Не удалось загрузить фото: {exc}")
        return EDIT_PHOTOS

    context.user_data["edit_content"].append({"tag": "img", "attrs": {"src": url}})
    count = len(context.user_data["edit_content"])
    await update.message.reply_text(
        f"Фото добавлено (всего {count} шт.). Присылай ещё или отправь /save"
    )
    return EDIT_PHOTOS


async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    path = context.user_data.get("edit_path")
    title = context.user_data.get("edit_title")
    content_nodes = context.user_data.get("edit_content")

    if not path:
        await update.message.reply_text("Нечего сохранять. Начни через /editpost.")
        return ConversationHandler.END

    await update.message.reply_text("Обновляю страницу…")

    try:
        page = telegraph.edit_page(
            path=path,
            title=title,
            content=build_final_content(content_nodes),
            author_name=SIGNATURE_NAME,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка редактирования страницы Telegraph")
        await update.message.reply_text(f"Не удалось обновить страницу: {exc}")
        return ConversationHandler.END

    await store_page(path, title, content_nodes)

    page_url = f"https://telegra.ph/{page['path']}"
    await update.message.reply_text(f"Готово! Страница обновлена:\n{page_url}")

    context.user_data.clear()
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).post_init(init_db).build()

    # Группа -1: отсекаем всех, кроме разрешённого пользователя, раньше, чем
    # сработает любой другой хендлер.
    application.add_handler(
        MessageHandler(~ALLOWED_FILTER, reject_unauthorized), group=-1
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("newpost", newpost, filters=ALLOWED_FILTER)],
        states={
            TITLE: [MessageHandler(ALLOWED_FILTER & filters.TEXT & ~filters.COMMAND, get_title)],
            COVER: [MessageHandler(ALLOWED_FILTER & filters.PHOTO, get_cover)],
            PHOTOS: [
                MessageHandler(ALLOWED_FILTER & filters.PHOTO, get_photo),
                CommandHandler("done", done, filters=ALLOWED_FILTER),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel, filters=ALLOWED_FILTER)],
    )

    edit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("editpost", editpost, filters=ALLOWED_FILTER)],
        states={
            EDIT_PATH: [
                MessageHandler(ALLOWED_FILTER & filters.TEXT & ~filters.COMMAND, get_edit_path)
            ],
            EDIT_PHOTOS: [
                MessageHandler(ALLOWED_FILTER & filters.PHOTO, get_edit_photo),
                CommandHandler("save", save_edit, filters=ALLOWED_FILTER),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel, filters=ALLOWED_FILTER)],
    )

    application.add_handler(CommandHandler("start", start, filters=ALLOWED_FILTER))
    application.add_handler(conv_handler)
    application.add_handler(edit_conv_handler)

    logger.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
