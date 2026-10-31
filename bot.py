
import telebot
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Фиктивный веб-сервер для Render (чтобы не ругался на порты)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"GorodTradeBot is running!")

def run_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f"Dummy web server started on port {port} for Render")
    server.serve_forever()

# Запускаем веб-сервер в фоне
threading.Thread(target=run_server, daemon=True).start()

print("=== GorodTradeBot v2.2 starting ===")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")

print(f"BOT_TOKEN exists: {bool(BOT_TOKEN)}")
print(f"GEMINI_KEY exists: {bool(GEMINI_KEY)}")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing! Add in Render -> Environment")
if not GEMINI_KEY:
    raise Exception("GEMINI_KEY missing! Add in Render -> Environment")

bot = telebot.TeleBot(BOT_TOKEN)

try:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_KEY)
    print("Using google-genai new SDK")
    USE_NEW_SDK = True
except Exception as e:
    print(f"New SDK failed: {e}")
    USE_NEW_SDK = False

print("Loading bases...")
with open('pocket_OTC.json','r',encoding='utf-8') as f:
    OTC_BASE = json.load(f)
with open('pocket_REAL_v2.json','r',encoding='utf-8') as f:
    REAL_BASE = json.load(f)
with open('master_prompt_v2.md','r',encoding='utf-8') as f:
    MASTER = f.read()

print(f"Bases loaded: OTC {len(OTC_BASE)}, REAL {len(REAL_BASE)}")

user_mode = {}

@bot.message_handler(commands=['start','otc','real','auto','help'])
def handle_mode(message):
    txt = message.text
    if txt == '/otc':
        user_mode[message.chat.id] = 'OTC'
        bot.send_message(message.chat.id, "📉 OTC режим (19 паттернов) - кидай скрин!")
    elif txt == '/real':
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(message.chat.id, "📈 REAL режим (34 паттерна) - кидай скрин!")
    else:
        user_mode[message.chat.id] = 'AUTO'
        bot.send_message(message.chat.id, "🤖 GorodTradeBot v2.2 готов!\n/otc /real /auto\nКидай скрин!")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'AUTO')
    bot.send_message(message.chat.id, f"⏳ Анализирую {mode}...")
    try:
        # For photo messages, file_id is in message.photo array
        file_id = message.photo[-1].file_id if message.photo else message.file_id
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        base = OTC_BASE if mode == 'OTC' else REAL_BASE
        prompt = f"{MASTER}\n\nБАЗА: {mode} {len(base)} карт\n{json.dumps(base, ensure_ascii=False)}\n\nПроанализируй скрин."
        if USE_NEW_SDK:
            response = client.models.generate_content(model='gemini-1.5-flash', contents=[types.Part.from_bytes(data=downloaded, mime_type='image/jpeg'), prompt])
            text = response.text
        else:
            text = "SDK error"
        bot.send_message(message.chat.id, text[:3500])
    except Exception as e:
        print(f"Error: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    bot.send_message(message.chat.id, "Кидай скрин 😉 /help")

print("Starting polling...")
bot.infinity_polling()
