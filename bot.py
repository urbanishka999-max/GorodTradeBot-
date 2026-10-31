
import telebot
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"GorodTradeBot is running!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f"Dummy web server started on port {port}")
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

print("=== GorodTradeBot v2.6 starting ===")

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
        bot.send_message(message.chat.id, "🤖 GorodTradeBot v2.6 готов!\n/otc /real /auto\nКидай скрин!")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'AUTO')
    bot.send_message(message.chat.id, f"⏳ Анализирую {mode}...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        base = OTC_BASE if mode == 'OTC' else REAL_BASE
        prompt = f"{MASTER}\n\nБАЗА: {mode} {len(base)} карт\n{json.dumps(base, ensure_ascii=False)}\n\nПроанализируй скрин."
        
        text = None
        last_err = None
        
        if USE_NEW_SDK:
            models_to_try = ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-3.8-flash', 'gemini-flash-latest', 'gemini-1.5-flash-001']
            for model_name in models_to_try:
                try:
                    print(f"Trying model {model_name}...")
                    response = client.models.generate_content(
                        model=model_name, 
                        contents=[types.Part.from_bytes(data=downloaded, mime_type='image/jpeg'), prompt]
                    )
                    text = response.text
                    print(f"Success with {model_name}")
                    break
                except Exception as e:
                    print(f"Model {model_name} failed: {e}")
                    last_err = e
                    continue
            if text is None:
                raise last_err
        else:
            text = "SDK error"
        
        if len(text) > 3500:
            text = text[:3500] + "\n...обрезано"
        bot.send_message(message.chat.id, text)
    except Exception as e:
        print(f"Error in handle_photo: {e}")
        import traceback
        traceback.print_exc()
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    bot.send_message(message.chat.id, "Кидай скрин 😉 /help")

print("Starting polling...")
bot.infinity_polling()
