import telebot
import json
import os
from google import genai
from google.genai import types
from PIL import Image
import io

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")

if not BOT_TOKEN or not GEMINI_KEY:
    raise Exception("Set BOT_TOKEN and GEMINI_KEY in Render Environment Variables!")

bot = telebot.TeleBot(BOT_TOKEN)
client = genai.Client(api_key=GEMINI_KEY)

print("Loading bases...")
with open('pocket_OTC.json', 'r', encoding='utf-8') as f:
    OTC_BASE = json.load(f)

with open('pocket_REAL_v2.json', 'r', encoding='utf-8') as f:
    REAL_BASE = json.load(f)

with open('master_prompt_v2.md', 'r', encoding='utf-8') as f:
    MASTER = f.read()

print(f"OTC: {len(OTC_BASE)} cards, REAL: {len(REAL_BASE)} cards loaded")
print("Bot GorodTradeBot started...")

user_mode = {}

@bot.message_handler(commands=['start', 'otc', 'real', 'auto', 'help'])
def handle_mode(message):
    txt = message.text
    if txt == '/otc':
        user_mode[message.chat.id] = 'OTC'
        bot.send_message(message.chat.id, "📉 Режим OTC включен (синтетика, 19 паттернов).\nЛогика: ловим ложные пробои BOF и отскоки от Bollinger.\nКидай скрин!")
    elif txt == '/real':
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(message.chat.id, "📈 Режим REAL включен (реальный рынок, 34 паттерна + новостной фильтр).\nЛогика: тренд + пробой с ретестом.\nКидай скрин!")
    else:
        user_mode[message.chat.id] = 'AUTO'
        bot.send_message(message.chat.id,
            "🤖 GorodTradeBot v2.0 готов!\n\n"
            "Я анализирую скрины Pocket Option по книгам Акелиса, Элдера, Нисона, Beggs.\n\n"
            "🧠 Автоопределение: вижу надпись OTC на скрине -> включаю OTC базу (отскок), нет OTC -> REAL базу (тренд).\n\n"
            "Команды:\n"
            "/otc - только ОТС рынок (выходные, 19 карт)\n"
            "/real - только РЕАЛ рынок (будни, 34 карты)\n"
            "/auto - авто (рекомендуется)\n"
            "/help - помощь\n\n"
            "📸 Просто кидай скрин графика!",
            parse_mode='Markdown')

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'AUTO')
    bot.send_message(message.chat.id, f"⏳ Анализирую в режиме {mode}... 1 сек")
    try:
        file_info = bot.get_file(message.file_id)
        downloaded = bot.download_file(file_info.file_path)
        
        # Определяем базу
        if mode == 'OTC':
            base = OTC_BASE
            base_name = "OTC (19 карт: BOF, TST, Bollinger, RSI 7)"
        elif mode == 'REAL':
            base = REAL_BASE
            base_name = "REAL (34 карты: EMA 21/50, MACD, BPB + новости)"
        else:
            base = REAL_BASE
            base_name = "AUTO - определю OTC/REAL по надписи на скрине"
            
        prompt = f"""{MASTER}
БАЗА ЗНАНИЙ ({base_name}):
{json.dumps(base, ensure_ascii=False)}
ЗАДАЧА: Проанализируй этот скрин Pocket Option. Определи OTC/REAL, проверь новостной импульс если REAL, найди паттерн из базы. Если паттерн не сформирован - честно <50% воздержаться.
"""
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[
                types.Part.from_bytes(data=downloaded, mime_type='image/jpeg'),
                prompt
            ]
        )
        
        # Защита от слишком длинного ответа
        text = response.text
        if len(text) > 3500:
            text = text[:3500] + "\n... (обрезано)"
            
        bot.send_message(message.chat.id, text)
    except Exception as e:
        print(f"Error: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}\nПопробуй еще раз кинуть скрин.")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    bot.send_message(message.chat.id, "Кидай скрин графика, а не текст 😉\nЖми /help")

if __name__ == "__main__":
    bot.infinity_polling()
