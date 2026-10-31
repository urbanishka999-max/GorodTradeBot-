
import telebot
import json
import os
from google import genai
from google.genai import types

print("=== GorodTradeBot starting ===")
print(f"Env vars present: {list(os.environ.keys())}")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not found in environment!")
    print("Available vars containing TOKEN:", [k for k in os.environ.keys() if "TOKEN" in k])
    # Don't crash immediately, let Render show logs
    raise Exception("BOT_TOKEN missing! Add it in Render -> Environment -> Add Variable")

if not GEMINI_KEY:
    print("ERROR: GEMINI_KEY not found in environment!")
    print("Available vars containing GEMINI or KEY:", [k for k in os.environ.keys() if "GEMINI" in k or "KEY" in k])
    raise Exception("GEMINI_KEY missing! Add it in Render -> Environment -> Add Variable")

print("Both keys found, starting bot...")

bot = telebot.TeleBot(BOT_TOKEN)
client = genai.Client(api_key=GEMINI_KEY)

print("Loading bases...")
try:
    with open('pocket_OTC.json','r',encoding='utf-8') as f:
        OTC_BASE = json.load(f)
    with open('pocket_REAL_v2.json','r',encoding='utf-8') as f:
        REAL_BASE = json.load(f)
    with open('master_prompt_v2.md','r',encoding='utf-8') as f:
        MASTER = f.read()
    print(f"OTC: {len(OTC_BASE)} cards, REAL: {len(REAL_BASE)} cards loaded")
except Exception as e:
    print(f"Error loading bases: {e}")
    OTC_BASE = []
    REAL_BASE = []
    MASTER = "You are a trading analyst"

print("Bot GorodTradeBot started...")

user_mode = {}

@bot.message_handler(commands=['start','otc','real','auto','help'])
def handle_mode(message):
    txt = message.text
    if txt == '/otc':
        user_mode[message.chat.id] = 'OTC'
        bot.send_message(message.chat.id, "ð Ð ÐµÐ¶Ð¸Ð¼ OTC Ð²ÐºÐ»ÑÑÐµÐ½ (ÑÐ¸Ð½ÑÐµÑÐ¸ÐºÐ°, 19 Ð¿Ð°ÑÑÐµÑÐ½Ð¾Ð²).\nÐÐ¾Ð³Ð¸ÐºÐ°: Ð»Ð¾Ð²Ð¸Ð¼ Ð»Ð¾Ð¶Ð½ÑÐµ Ð¿ÑÐ¾Ð±Ð¾Ð¸ BOF Ð¸ Ð¾ÑÑÐºÐ¾ÐºÐ¸ Ð¾Ñ Bollinger.\nÐÐ¸Ð´Ð°Ð¹ ÑÐºÑÐ¸Ð½!")
    elif txt == '/real':
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(message.chat.id, "ð Ð ÐµÐ¶Ð¸Ð¼ REAL Ð²ÐºÐ»ÑÑÐµÐ½ (ÑÐµÐ°Ð»ÑÐ½ÑÐ¹ ÑÑÐ½Ð¾Ðº, 34 Ð¿Ð°ÑÑÐµÑÐ½Ð° + Ð½Ð¾Ð²Ð¾ÑÑÐ½Ð¾Ð¹ ÑÐ¸Ð»ÑÑÑ).\nÐÐ¾Ð³Ð¸ÐºÐ°: ÑÑÐµÐ½Ð´ + Ð¿ÑÐ¾Ð±Ð¾Ð¹ Ñ ÑÐµÑÐµÑÑÐ¾Ð¼.\nÐÐ¸Ð´Ð°Ð¹ ÑÐºÑÐ¸Ð½!")
    else:
        user_mode[message.chat.id] = 'AUTO'
        bot.send_message(message.chat.id, 
        "ð¤ *GorodTradeBot v2.0* Ð³Ð¾ÑÐ¾Ð²!\n\n"
        "Ð¯ Ð°Ð½Ð°Ð»Ð¸Ð·Ð¸ÑÑÑ ÑÐºÑÐ¸Ð½Ñ Pocket Option Ð¿Ð¾ ÐºÐ½Ð¸Ð³Ð°Ð¼ ÐÐºÐµÐ»Ð¸ÑÐ°, Ð­Ð»Ð´ÐµÑÐ°, ÐÐ¸ÑÐ¾Ð½Ð°, Beggs.\n\n"
        "ð§  ÐÐ²ÑÐ¾Ð¾Ð¿ÑÐµÐ´ÐµÐ»ÐµÐ½Ð¸Ðµ: Ð²Ð¸Ð¶Ñ Ð½Ð°Ð´Ð¿Ð¸ÑÑ OTC Ð½Ð° ÑÐºÑÐ¸Ð½Ðµ -> Ð²ÐºÐ»ÑÑÐ°Ñ OTC Ð±Ð°Ð·Ñ (Ð¾ÑÑÐºÐ¾Ðº), Ð½ÐµÑ OTC -> REAL Ð±Ð°Ð·Ñ (ÑÑÐµÐ½Ð´).\n\n"
        "ÐÐ¾Ð¼Ð°Ð½Ð´Ñ:\n"
        "/otc - ÑÐ¾Ð»ÑÐºÐ¾ ÐÐ¢Ð¡ ÑÑÐ½Ð¾Ðº (Ð²ÑÑÐ¾Ð´Ð½ÑÐµ, 19 ÐºÐ°ÑÑ)\n"
        "/real - ÑÐ¾Ð»ÑÐºÐ¾ Ð ÐÐÐ ÑÑÐ½Ð¾Ðº (Ð±ÑÐ´Ð½Ð¸, 34 ÐºÐ°ÑÑÑ)\n"
        "/auto - Ð°Ð²ÑÐ¾ (ÑÐµÐºÐ¾Ð¼ÐµÐ½Ð´ÑÐµÑÑÑ)\n"
        "/help - Ð¿Ð¾Ð¼Ð¾ÑÑ\n\n"
        "ð¸ ÐÑÐ¾ÑÑÐ¾ ÐºÐ¸Ð´Ð°Ð¹ ÑÐºÑÐ¸Ð½ Ð³ÑÐ°ÑÐ¸ÐºÐ°!",
        parse_mode='Markdown')

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'AUTO')
    bot.send_message(message.chat.id, f"â³ ÐÐ½Ð°Ð»Ð¸Ð·Ð¸ÑÑÑ Ð² ÑÐµÐ¶Ð¸Ð¼Ðµ {mode}... 1 ÑÐµÐº")

    try:
        file_info = bot.get_file(message.file_id)
        downloaded = bot.download_file(file_info.file_path)
        
        if mode == 'OTC':
            base = OTC_BASE
            base_name = "OTC (19 ÐºÐ°ÑÑ: BOF, TST, Bollinger, RSI 7)"
        elif mode == 'REAL':
            base = REAL_BASE
            base_name = "REAL (34 ÐºÐ°ÑÑÑ: EMA 21/50, MACD, BPB + Ð½Ð¾Ð²Ð¾ÑÑÐ¸)"
        else:
            base = REAL_BASE
            base_name = "AUTO - Ð¾Ð¿ÑÐµÐ´ÐµÐ»Ñ OTC/REAL Ð¿Ð¾ Ð½Ð°Ð´Ð¿Ð¸ÑÐ¸ Ð½Ð° ÑÐºÑÐ¸Ð½Ðµ"

        prompt = f"""{MASTER}

ÐÐÐÐ ÐÐÐÐÐÐ ({base_name}):
{json.dumps(base, ensure_ascii=False)}

ÐÐÐÐÐ§Ð: ÐÑÐ¾Ð°Ð½Ð°Ð»Ð¸Ð·Ð¸ÑÑÐ¹ ÑÑÐ¾Ñ ÑÐºÑÐ¸Ð½ Pocket Option. ÐÐ¿ÑÐµÐ´ÐµÐ»Ð¸ OTC/REAL, Ð¿ÑÐ¾Ð²ÐµÑÑ Ð½Ð¾Ð²Ð¾ÑÑÐ½Ð¾Ð¹ Ð¸Ð¼Ð¿ÑÐ»ÑÑ ÐµÑÐ»Ð¸ REAL, Ð½Ð°Ð¹Ð´Ð¸ Ð¿Ð°ÑÑÐµÑÐ½ Ð¸Ð· Ð±Ð°Ð·Ñ. ÐÑÐ»Ð¸ Ð¿Ð°ÑÑÐµÑÐ½ Ð½Ðµ ÑÑÐ¾ÑÐ¼Ð¸ÑÐ¾Ð²Ð°Ð½ - ÑÐµÑÑÐ½Ð¾ <50% Ð²Ð¾Ð·Ð´ÐµÑÐ¶Ð°ÑÑÑÑ.
"""

        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[
                types.Part.from_bytes(data=downloaded, mime_type='image/jpeg'),
                prompt
            ]
        )
        
        text = response.text
        if len(text) > 3500:
            text = text[:3500] + "\n... (Ð¾Ð±ÑÐµÐ·Ð°Ð½Ð¾)"
            
        bot.send_message(message.chat.id, text)

    except Exception as e:
        print(f"Error: {e}")
        bot.send_message(message.chat.id, f"â ÐÑÐ¸Ð±ÐºÐ°: {e}\nÐÐ¾Ð¿ÑÐ¾Ð±ÑÐ¹ ÐµÑÐµ ÑÐ°Ð· ÐºÐ¸Ð½ÑÑÑ ÑÐºÑÐ¸Ð½.")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    bot.send_message(message.chat.id, "ÐÐ¸Ð´Ð°Ð¹ ÑÐºÑÐ¸Ð½ Ð³ÑÐ°ÑÐ¸ÐºÐ°, Ð° Ð½Ðµ ÑÐµÐºÑÑ ð\nÐÐ¼Ð¸ /help")

if __name__ == "__main__":
    print("Starting polling...")
    bot.infinity_polling()
