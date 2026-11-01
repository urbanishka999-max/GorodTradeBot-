import telebot
import json
import os
import threading
import requests
import pandas as pd
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# === 1. Render Keep-Alive Server (чтобы Render не усыплял бота) ===
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
        print(f"Dummy web server started on port {port}", flush=True)
        server.serve_forever()
    except Exception as e:
        print(f"Server error: {e}", flush=True)

threading.Thread(target=run_server, daemon=True).start()

print("=== GorodTradeBot v5 FOREX REAL ===", flush=True)

# === 2. Проверка ключей ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing! Add to Render Environment.")
if not GEMINI_KEY:
    raise Exception("GEMINI_KEY missing! Add to Render Environment.")

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

# === 3. Инициализация Gemini с надежным фоллбэком ===
try:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_KEY)
    print("Gemini SDK ready", flush=True)
    USE_NEW_SDK = True
except Exception as e:
    print(f"Gemini SDK init failed: {e}", flush=True)
    USE_NEW_SDK = False

def ask_gemini(prompt: str, image_bytes: bytes = None):
    """Умная функция: пробует несколько моделей, если одна выдает ошибку"""
    if not USE_NEW_SDK:
        return "Ошибка: Gemini SDK не загружен."
    
    models_to_try = ['gemini-1.5-flash', 'gemini-1.5-flash-8b', 'gemini-2.0-flash-lite', 'gemini-1.5-pro']
    last_error = None
    
    for model_name in models_to_try:
        try:
            if image_bytes:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'),
                        prompt
                    ]
                )
            else:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[prompt]
                )
            print(f"Success with model: {model_name}", flush=True)
            return response.text
        except Exception as e:
            last_error = e
            continue
            
    return f"Все модели Gemini недоступны. Последняя ошибка: {last_error}"

# === 4. Встроенный промпт (никаких внешних файлов!) ===
MASTER_PROMPT = """
Ты профессиональный скальпер бинарных опционов Pocket Option.
Твоя задача: проанализировать данные и дать четкий, краткий сигнал.
Правила:
1. Если RSI > 70 и цена выше верхней линии Bollinger -> сигнал PUT (отскок вниз).
2. Если RSI < 30 и цена ниже нижней линии Bollinger -> сигнал CALL (отскок вверх).
3. Если цена внутри канала и RSI нейтральный -> сигнал WAIT (ждать).
4. Укажи рекомендуемое время экспирации: 3-5 минут.
5. Укажи уверенность в процентах (строго 55-75%).
6. Будь краток, используй эмодзи, не пиши воду.
"""

user_mode = {}

# === 5. Обработчики команд ===
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
        bot.send_message(message.chat.id, "📉 OTC режим активирован. Кидай скриншот графика!", reply_markup=get_main_keyboard())
    elif 'real' in txt:
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(message.chat.id, "📈 REAL режим активирован. Используй /eurusd или /gbpusd для мгновенного LIVE сигнала!", reply_markup=get_main_keyboard())
    else:
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(
            message.chat.id, 
            "🤖 GorodTradeBot v5 FOREX готов!\n\n"
            "🚀 LIVE команды (мгновенный анализ без скринов):\n"
            "/eurusd - EUR/USD REAL\n"
            "/gbpusd - GBP/USD REAL\n\n"
            "📸 Фото-режим: просто отправь скриншот графика в чат!", 
            reply_markup=get_main_keyboard()
        )

# === 6. LIVE Режим (TwelveData) ===
def handle_forex_live(message, symbol):
    if not TWELVE_KEY:
        bot.send_message(message.chat.id, "❌ Ошибка: TWELVE_KEY не найден в настройках Render!", reply_markup=get_main_keyboard())
        return
    
    bot.send_message(message.chat.id, f"⏳ Загружаю LIVE свечи {symbol} M1...", reply_markup=get_main_keyboard())
    
    try:
        url = "https://api.twelvedata.com/time_series"
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
            raise Exception(f"TwelveData вернул ошибку: {data.get('message', 'Unknown')}")
        
        # TwelveData отдает свежие свечи первыми, переворачиваем список
        candles = list(reversed(data["values"]))
        closes = [float(c["close"]) for c in candles]
        
        current_price = closes[-1]
        
        # Расчет RSI (период 7)
        deltas = pd.Series(closes).diff()
        gains = deltas.where(deltas > 0, 0)
        losses = -deltas.where(deltas < 0, 0)
        avg_gain = gains.rolling(7).mean()
        avg_loss = losses.rolling(7).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = rsi.iloc[-1]
        
        if rsi_val > 70:
            rsi_signal = "перекуплен 🔴"
        elif rsi_val < 30:
            rsi_signal = "перепродан 🟢"
        else:
            rsi_signal = "нейтрально ⚪"
        
        # Расчет Bollinger Bands (период 20, отклонение 2.0)
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

АКТУАЛЬНЫЕ LIVE ДАННЫЕ:
{json.dumps(live_json, ensure_ascii=False, indent=2)}

Дай сигнал CALL/PUT/WAIT на 3-4 минуты. Коротко и по делу."""
        
        text = ask_gemini(prompt)
        
        header = (
            f"📊 LIVE {symbol} REAL (TwelveData)\n"
            f"⏱️ Цена: {current_price}\n"
            f"📈 RSI 7: {rsi_val:.1f} ({rsi_signal})\n"
            f"📉 Bollinger: {bb_pos}\n\n"
        )
        bot.send_message(message.chat.id, header + text, reply_markup=get_main_keyboard())
        
    except Exception as e:
        print(f"LIVE error {symbol}: {e}", flush=True)
        bot.send_message(message.chat.id, f"❌ Ошибка LIVE {symbol}: {e}\n\nПроверь ключ TWELVE_KEY в Render.", reply_markup=get_main_keyboard())

# === 7. PHOTO Режим (Анализ скриншотов) ===
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'REAL')
    bot.send_message(message.chat.id, f"⏳ Анализирую скриншот ({mode})...", reply_markup=get_main_keyboard())
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        
        prompt = f"{MASTER_PROMPT}\n\nПроанализируй этот скриншот графика и дай четкий сигнал."
        text = ask_gemini(prompt, image_bytes=downloaded)
        
        if text and not text.startswith("Ошибка"):
            bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())
        else:
            bot.send_message(message.chat.id, f"❌ Не удалось получить ответ от AI: {text}", reply_markup=get_main_keyboard())
            
    except Exception as e:
        print(f"Photo error: {e}", flush=True)
        bot.send_message(message.chat.id, f"❌ Ошибка обработки фото: {e}", reply_markup=get_main_keyboard())

# === 8. Запуск ===
print("Starting polling v5...", flush=True)
bot.infinity_polling()
