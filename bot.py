import telebot
import json
import os
import threading
import requests
import pandas as pd
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# === Render Port Fix ===
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
import traceback
import sys
def handle_exception(exc_type, exc_value, exc_traceback):
    print("UNCAUGHT EXCEPTION:", flush=True)
    traceback.print_exception(exc_type, exc_value, exc_traceback)
    sys.exit(1)
sys.excepthook = handle_exception


BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")  # Новый ключ для форекс

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
        tb_types.KeyboardButton("/real"),
        tb_types.KeyboardButton("/auto")
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

print("Loading bases...")
with open('pocket_OTC.json','r',encoding='utf-8') as f:
    OTC_BASE = json.load(f)
with open('pocket_REAL_v2.json','r',encoding='utf-8') as f:
    REAL_BASE = json.load(f)
with open('master_prompt_v2.md','r',encoding='utf-8') as f:
    MASTER = f.read()

# === FOREX API - TwelveData ===
def get_forex_candles(symbol="EUR/USD", interval="1min", limit=100):
    """Тянет свечи с TwelveData - бесплатно, стабильно, без сессий"""
    if not TWELVE_KEY:
        raise Exception("TWELVE_KEY не установлен! Добавь в Render Environment -> TWELVE_KEY")
    
    # Формат для TwelveData: EUR/USD, 1min
    url = f"https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": limit,
        "apikey": TWELVE_KEY,
        "format": "JSON"
    }
    print(f"Fetching {symbol} {interval} from TwelveData...")
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    
    if "values" not in data:
        print(f"TwelveData error: {data}")
        raise Exception(f"TwelveData error: {data.get('message', 'Unknown')}")
    
    # values приходят в обратном порядке (новые сверху), переворачиваем
    candles = list(reversed(data["values"]))
    return candles

def calc_rsi(prices, period=7):
    """RSI 7 как в твоей базе"""
    deltas = pd.Series(prices).diff()
    gains = deltas.where(deltas > 0, 0)
    losses = -deltas.where(deltas < 0, 0)
    avg_gain = gains.rolling(period).mean()
    avg_loss = losses.rolling(period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def calc_bollinger(prices, period=20, std=2.0):
    """Bollinger 20/2.0"""
    series = pd.Series(prices)
    sma = series.rolling(period).mean().iloc[-1]
    std_dev = series.rolling(period).std().iloc[-1]
    upper = sma + std * std_dev
    lower = sma - std * std_dev
    price = prices[-1]
    return {
        "sma": sma,
        "upper": upper,
        "lower": lower,
        "price": price,
        "position": "above_upper" if price > upper else "below_lower" if price < lower else "inside"
    }

user_mode = {}

@bot.message_handler(commands=['start','otc','real','auto','help','eurusd','gbpusd','usd_jpy','eur_jpy'])
def handle_commands(message):
    txt = message.text.lower()
    
    if 'eurusd' in txt:
        handle_forex_live(message, "EUR/USD")
        return
    elif 'gbpusd' in txt:
        handle_forex_live(message, "GBP/USD")
        return
    elif 'usd_jpy' in txt or 'usdjpy' in txt:
        handle_forex_live(message, "USD/JPY")
        return
    
    if 'otc' in txt:
        user_mode[message.chat.id] = 'OTC'
        bot.send_message(message.chat.id, "📉 OTC режим - кидай скрин! Для REAL используй /eurusd", reply_markup=get_main_keyboard())
    elif 'real' in txt:
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(message.chat.id, "📈 REAL режим - теперь LIVE! Жми /eurusd или /gbpusd для мгновенного сигнала", reply_markup=get_main_keyboard())
    else:
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(message.chat.id, "🤖 GorodTradeBot v5 FOREX готов!\\n\\n🚀 LIVE команды (без скринов, 2 сек):\\n/eurusd - EUR/USD REAL\\n/gbpusd - GBP/USD REAL\\n\\n📸 Фото-режим: /otc для OTC\\n\\nЖми /eurusd для теста!", reply_markup=get_main_keyboard())

def handle_forex_live(message, symbol):
    """Главный LIVE режим - тянет свечи и дает сигнал за 2-3 сек"""
    if not TWELVE_KEY:
        bot.send_message(message.chat.id, "❌ TWELVE_KEY не настроен!\\n\\n1. Зарегайся на https://twelvedata.com (бесплатно)\\n2. Скопируй API Key\\n3. В Render -> Environment -> добавь переменную TWELVE_KEY=твой_ключ\\n4. Manual Deploy", reply_markup=get_main_keyboard())
        return
    
    bot.send_message(message.chat.id, f"⏳ Тяну LIVE свечи {symbol} M1 с TwelveData...", reply_markup=get_main_keyboard())
    
    try:
        # 1. Тянем свечи
        candles = get_forex_candles(symbol, "1min", 100)
        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        
        current_price = closes[-1]
        prev_price = closes[-2]
        
        # 2. Считаем индикаторы по твоей базе
        rsi = calc_rsi(closes, 7)
        bb = calc_bollinger(closes, 20, 2.0)
        
        # 3. Формируем триггеры для Gemini
        trend = "восходящий" if closes[-1] > closes[-5] else "нисходящий"
        rsi_signal = "перекуплен" if rsi > 70 else "перепродан" if rsi < 30 else "нейтрально"
        bb_signal = f"цена {bb['position']}, верх {bb['upper']:.5f}, низ {bb['lower']:.5f}"
        
        # 4. Готовим промпт для Gemini с живыми данными
        live_json = {
            "symbol": symbol,
            "market_type": "REAL",
            "current_price": current_price,
            "previous_price": prev_price,
            "trend_M1": trend,
            "rsi_7": round(rsi, 2),
            "rsi_signal": rsi_signal,
            "bollinger": bb_signal,
            "last_5_closes": closes[-5:],
            "time": datetime.now().strftime("%H:%M:%S"),
            "expiry_recommended": "3-4 мин (скальп от Bollinger)"
        }
        
        base = REAL_BASE
        short_base = base[:15]
        
        prompt = f"""{MASTER[:1500]}

LIVE ДАННЫЕ REAL ФОРЕКС (без скрина, точные цифры):
{json.dumps(live_json, ensure_ascii=False, indent=2)}

База REAL: {json.dumps(short_base, ensure_ascii=False)[:2500]}

ЗАДАЧА: Ты скальпер бинарок Pocket Option M1 REAL. 
По LIVE данным выше дай сигнал CALL/PUT на 3-4 минуты.
Учитывай RSI 7, Bollinger, тренд. 
Если RSI>70 и цена above_upper -> PUT (отскок от Bollinger)
Если RSI<30 и цена below_lower -> CALL
Дай уверенность 55-75% и JSON.
Коротко!
"""
        
        # 5. Отправляем в Gemini (текстом, без картинки - быстрее)
        text = None
        last_err = None
        if USE_NEW_SDK:
            models_to_try = ['gemini-1.5-flash-8b', 'gemini-2.0-flash-lite', 'gemini-2.5-flash-lite', 'gemini-1.5-flash']
            for model_name in models_to_try:
                try:
                    print(f"Trying {model_name} for LIVE {symbol}...")
                    response = client.models.generate_content(
                        model=model_name,
                        contents=[prompt]
                    )
                    text = response.text
                    print(f"Success LIVE with {model_name}")
                    break
                except Exception as e:
                    print(f"Model {model_name} failed: {e}")
                    last_err = e
                    continue
            if text is None:
                raise last_err
        
        # 6. Отправляем результат
        header = f"📊 LIVE {symbol} REAL (TwelveData)\\n⏱️ Время: {live_json['time']} | Цена: {current_price}\\n📈 RSI 7: {rsi:.1f} ({rsi_signal}) | {bb_signal}\\n\\n"
        bot.send_message(message.chat.id, header + text, reply_markup=get_main_keyboard())
        
    except Exception as e:
        print(f"LIVE error {symbol}: {e}")
        import traceback
        traceback.print_exc()
        bot.send_message(message.chat.id, f"❌ LIVE ошибка {symbol}: {e}\\n\\nПроверь TWELVE_KEY в Render или попробуй позже. Пока кидай скрин!", reply_markup=get_main_keyboard())

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'AUTO')
    bot.send_message(message.chat.id, f"⏳ Анализирую скрин {mode}...", reply_markup=get_main_keyboard())
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        base = OTC_BASE if mode == 'OTC' else REAL_BASE
        short_base = base[:12]
        prompt = f"""{MASTER[:1500]}\nБаза {mode}: {json.dumps(short_base, ensure_ascii=False)[:3000]}\nДай сигнал 55-75%!"""
        text = None
        if USE_NEW_SDK:
            for model_name in ['gemini-1.5-flash-8b', 'gemini-2.0-flash-lite', 'gemini-1.5-flash']:
                try:
                    response = client.models.generate_content(model=model_name, contents=[types.Part.from_bytes(data=downloaded, mime_type='image/jpeg'), prompt])
                    text = response.text
                    break
                except:
                    continue
        bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", reply_markup=get_main_keyboard())

print("Starting polling v5...")
bot.infinity_polling()
