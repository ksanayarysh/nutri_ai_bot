from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI, OpenAIError
import os
import logging

# Загрузка переменных окружения
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в .env")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не задан в .env")

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация OpenAI клиента
client = OpenAI(api_key=OPENAI_API_KEY)

# Функция запроса к OpenAI
async def chatgpt_response(messages: list[dict]) -> str:
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.6,
        )
        return response.choices[0].message.content.strip()
    except OpenAIError as e:
        logger.error(f"OpenAI API error: {e}")
        return "Ошибка доступа к ИИ. Вероятно, закончились токены."
    except Exception as e:
        logger.exception("Unexpected error during chatgpt_response")
        return "Произошла ошибка при обработке запроса."

# Обработчик /start
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Напиши мне что-нибудь, и я отвечу.")

# Обработка обычных текстов
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    chat_id = update.effective_chat.id

    logger.info(f"Запрос от {chat_id}: {text}")
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    response = await chatgpt_response([
        {"role": "system", "content": "Ты умный Telegram бот."},
        {"role": "user", "content": text}
    ])

    await context.bot.send_message(chat_id=chat_id, text=response or "Ответ пуст.")

# Точка входа
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()