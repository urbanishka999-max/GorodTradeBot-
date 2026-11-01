import telebot
import json
import os
import threading
import requests
import pandas as pd
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# === Render Port Fix (чтобы Render не усыплял бота) ===
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"GorodTradeBot v5 FOREX is running!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, format, *args):
        return

def run_server():
    port = int(os.getenv("PORT", 10000))
    try:
        server = HTTPServer(('0.0.0.0', port), Handler)
        print(f"Dummy web server started on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"Server error: {e}")

threading.Thread(target=run_server, daemon=True).start()

print("=== GorodTradeBot v5 FOREX REAL ===", flush=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing!")
if not GEMINI_KEY:
    raise Exception("GEMINI_KEY missing!")

bot = telebot.TeleBot(BOT_TOKEN)

from telebot import types as tb_types
def get_main_keyboard():
    markup = tb_types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3, is_persistent=True)
    markup.add(
        tb_types.KeyboardButton("/start"),
        tb_types.KeyboardButton("/eurusd"),
        tb_types.KeyboardButton("/gbpusd"),
        tb_types.KeyboardButton("/otc"),
        tb_types.KeyboardButton("/real")
    )
    return markup

try:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_KEY)
    print("Gemini SDK ready")
    USE_NEW_SDK = True
except Exception as e:
    print(f"Gemini failed: {e}")
    USE_NEW_SDK = False

# Встроенный промпт (больше не зависит от внешних файлов!)
MASTER_PROMPT = """
Ты профессиональный скальпер бинарных опционов. 
Анализируй график и индикаторы. 
Дай четкий сигнал: CALL (вверх) или PUT (вниз) или WAIT (ждать).
Укажи уверенность в процентах (55-75%) и время экспирации (3-5 минут).
Будь краток и используй эмодзи.
"""

user_mode = {}

@bot.message_handler(commands=['start','otc','real','help','eurusd','gbpusd'])
def handle_commands(message):
    txt = message.text.lower()
    
    if 'eurusd' in txt:
        handle_forex_live(message, "EUR/USD")
        return
    elif 'gbpusd' in txt:
        handle_forex_live(message, "GBP/USD")
        return
    
    if 'otc' in txt:
        user_mode[message.chat.id] = 'OTC'
        bot.send_message(message.chat.id, "📉 OTC режим - кидай скрин!", reply_markup=get_main_keyboard())
    elif 'real' in txt:
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(message.chat.id, "📈 REAL режим - жми /eurusd или /gbpusd для мгновенного LIVE сигнала!", reply_markup=get_main_keyboard())
    else:
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(message.chat.id, "🤖 GorodTradeBot v5 FOREX готов!\n\n🚀 LIVE команды (без скринов, 2 сек):\n/eurusd - EUR/USD REAL\n/gbpusd - GBP/USD REAL\n\n📸 Фото-режим: просто кидай скрин графика!", reply_markup=get_main_keyboard())

def handle_forex_live(message, symbol):
    if not TWELVE_KEY:
        bot.send_message(message.chat.id, "❌ TWELVE_KEY не настроен в Render Environment!", reply_markup=get_main_keyboard())
        return
    
    bot.send_message(message.chat.id, f"⏳ Тяну LIVE свечи {symbol} M1 с TwelveData...", reply_markup=get_main_keyboard())
    
    try:
        url = f"https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol,
            "interval": "1min",
            "outputsize": 100,
            "apikey": TWELVE_KEY,
            "format": "JSON"
        }
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        if "values" not in data:
            raise Exception(f"TwelveData error: {data.get('message', 'Unknown')}")
        
        # TwelveData отдает новые свечи первыми, переворачиваем
        candles = list(reversed(data["values"]))
        closes = [float(c["close"]) for c in candles]
        
        current_price = closes[-1]
        
        # Расчет RSI 7
        deltas = pd.Series(closes).diff()
        gains = deltas.where(deltas > 0, 0)
        losses = -deltas.where(deltas < 0, 0)
        avg_gain = gains.rolling(7).mean()
        avg_loss = losses.rolling(7).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = rsi.iloc[-1]
        rsi_signal = "перекуплен 🔴" if rsi_val > 70 else "перепродан 🟢" if rsi_val < 30 else "нейтрально ⚪"
        
        # Расчет Bollinger 20/2.0
        series = pd.Series(closes)
        sma = series.rolling(20).mean().iloc[-1]
        std_dev = series.rolling(20).std().iloc[-1]
        upper = sma + 2.0 * std_dev
        lower = sma - 2.0 * std_dev
        
        if current_price > upper:
            bb_pos = "выше верхней границы"
        elif current_price < lower:
            bb_pos = "ниже нижней границы"
        else:
            bb_pos = "внутри канала"
        
        live_json = {
            "symbol": symbol,
            "current_price": current_price,
            "rsi_7": round(rsi_val, 2),
            "rsi_signal": rsi_signal,
            "bollinger_position": bb_pos,
            "bollinger_upper": round(upper, 5),
            "bollinger_lower": round(lower, 5)
        }
        
        prompt = f"""{MASTER_PROMPT}

LIVE ДАННЫЕ REAL ФОРЕКС:
{json.dumps(live_json, ensure_ascii=False, indent=2)}

ЗАДАЧА: Дай сигнал CALL/PUT/WAIT на 3-4 минуты на основе RSI и Bollinger.
Если RSI>70 и цена выше верхней границы -> PUT (отскок)
Если RSI<30 и цена ниже нижней границы -> CALL (отскок)
Коротко и по делу.
"""
        
        text = None
        if USE_NEW_SDK:
            response = client.models.generate_content(
                model='gemini-1.5-flash',
                contents=[prompt]
            )
            text = response.text
        
        header = f"📊 LIVE {symbol} REAL (TwelveData)\n⏱️ Цена: {current_price}\n📈 RSI 7: {rsi_val:.1f} ({rsi_signal})\n📉 Bollinger: {bb_pos}\n\n"
        bot.send_message(message.chat.id, header + text, reply_markup=get_main_keyboard())
        
    except Exception as e:
        print(f"LIVE error {symbol}: {e}")
        bot.send_message(message.chat.id, f"❌ LIVE ошибка {symbol}: {e}\n\nПроверь TWELVE_KEY в Render.", reply_markup=get_main_keyboard())

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'REAL')
    bot.send_message(message.chat.id, f"⏳ Анализирую скрин ({mode})...", reply_markup=get_main_keyboard())
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        
        prompt = f"{MASTER_PROMPT}\nПроанализируй этот скриншот графика Pocket Option и дай сигнал."
        text = None
        
        if USE_NEW_SDK:
            response = client.models.generate_content(
                model='gemini-1.5-flash',
                contents=[types.Part.from_bytes(data=downloaded, mime_type='image/jpeg'), prompt]
            )
            text = response.text
            
        if text:
            bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())
        else:
            bot.send_message(message.chat.id, "❌ Не удалось получить ответ от Gemini", reply_markup=get_main_keyboard())
            
    except Exception as e:
        print(f"Photo error: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка анализа фото: {e}", reply_markup=get_main_keyboard())

print("Starting polling v5...")
bot.infinity_polling()
