"""
GorodTradeBot v6.2 - Исправленная версия (без пробелов в командах)
"""
import telebot
import json
import os
import sys
import threading
import traceback
import requests
import pandas as pd
import time
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ============================================================
# 1. KEEP-ALIVE СЕРВЕР
# ============================================================
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"GorodTradeBot v6.2 is running!")
    def log_message(self, format, *args):
        pass

def run_keepalive_server():
    port = int(os.getenv("PORT", 10000))
    try:
        server = HTTPServer(('0.0.0.0', port), KeepAliveHandler)
        print(f"[OK] Keep-alive сервер запущен на порту {port}", flush=True)
        server.serve_forever()
    except Exception as e:
        print(f"[ERROR] Keep-alive: {e}", flush=True)

threading.Thread(target=run_keepalive_server, daemon=True).start()

# ============================================================
# 2. ПЕРЕМЕННЫЕ И ИНИЦИАЛИЗАЦИЯ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")

if not BOT_TOKEN: raise Exception("BOT_TOKEN отсутствует!")
if not GEMINI_KEY: raise Exception("GEMINI_KEY отсутствует!")

bot = telebot.TeleBot(BOT_TOKEN)

USE_NEW_SDK = False
client = None
types = None
try:
    from google import genai
    from google.genai import types as gemini_types
    client = genai.Client(api_key=GEMINI_KEY)
    types = gemini_types
    USE_NEW_SDK = True
    print("[OK] Gemini SDK инициализирован", flush=True)
except Exception as e:
    print(f"[ERROR] Gemini SDK: {e}", flush=True)

GEMINI_MODELS = ['gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-2.0-flash-lite']

# ============================================================
# 3. ЗАГРУЗКА БАЗ ЗНАНИЙ (С АВТОЧИСТКОЙ ПРОБЕЛОВ В КЛЮЧАХ)
# ============================================================
def load_json_safe(filename):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    # Удаляем пробелы в конце ключей (спасение от ошибок предыдущего ИИ)
                    keys_to_fix = list(item.keys())
                    for key in keys_to_fix:
                        if key != key.strip():
                            item[key.strip()] = item.pop(key)
        print(f"[OK] Загружено {filename}: {len(data)} карточек", flush=True)
        return data
    except Exception as e:
        print(f"[ERROR] Ошибка загрузки {filename}: {e}", flush=True)
        return []

OTC_BASE = load_json_safe('pocket_OTC.json')
REAL_BASE = load_json_safe('pocket_REAL_v2.json')

MASTER_PROMPT = ""
try:
    with open('master_prompt_v2.md', 'r', encoding='utf-8') as f:
        MASTER_PROMPT = f.read()
    print(f"[OK] Загружен master_prompt_v2.md ({len(MASTER_PROMPT)} символов)", flush=True)
except FileNotFoundError:
    MASTER_PROMPT = "Ты профессиональный скальпер бинарных опционов. Дай краткий сигнал."

# ============================================================
# 4. КЭШ И КЛАВИАТУРА (ИСПРАВЛЕНО: УБРАНЫ ВСЕ ПРОБЕЛЫ В КОНЦЕ!)
# ============================================================
CACHE = {}
CACHE_TTL = 30  # 30 секунд для M1

def get_from_cache(key):
    if key in CACHE:
        data, timestamp = CACHE[key]
        if time.time() - timestamp < CACHE_TTL: return data
    return None

def save_to_cache(key, data):
    CACHE[key] = (data, time.time())

from telebot import types as tb_types

def get_main_keyboard():
    markup = tb_types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3, is_persistent=True)
    # ВНИМАНИЕ: здесь НЕ ДОЛЖНО быть пробелов в конце строк!
    markup.add(
        tb_types.KeyboardButton("/start"),
        tb_types.KeyboardButton("/eurusd"),
        tb_types.KeyboardButton("/gbpusd")
    )
    markup.add(
        tb_types.KeyboardButton("/usdjpy"),
        tb_types.KeyboardButton("/otc"),
        tb_types.KeyboardButton("/real")
    )
    return markup

# СЛОВАРЬ ПАР БЕЗ ПРОБЕЛОВ (Иначе бот не распознает команду!)
SUPPORTED_PAIRS = {
    "/eurusd": "EUR/USD",
    "/gbpusd": "GBP/USD",
    "/usdjpy": "USD/JPY",
    "/audusd": "AUD/USD",
    "/usdcad": "USD/CAD",
    "/nzdusd": "NZD/USD"
}
user_mode = {}

# ============================================================
# 5. ПОЛУЧЕНИЕ СВЕЧЕЙ
# ============================================================
def get_forex_candles(symbol="EUR/USD", interval="1min", limit=100):
    if not TWELVE_KEY: raise Exception("TWELVE_KEY не установлен!")
    cache_key = f"{symbol}_{interval}_{limit}"
    cached = get_from_cache(cache_key)
    if cached: return cached

    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": limit, "apikey": TWELVE_KEY, "format": "JSON"}
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()
    
    if "values" not in data:
        raise Exception(f"TwelveData: {data.get('message', 'Unknown error')}")
    
    candles = list(reversed(data["values"]))
    save_to_cache(cache_key, candles)
    return candles

# ============================================================
# 6. РАСЧЁТ ИНДИКАТОРОВ (ДОБАВЛЕНЫ EMA, ATR, STOCH %D)
# ============================================================
def calc_rsi(prices, period=7):
    if len(prices) < period + 1: return 50.0
    series = pd.Series(prices)
    deltas = series.diff()
    gains = deltas.where(deltas > 0, 0)
    losses = -deltas.where(deltas < 0, 0)
    rs = gains.rolling(period).mean() / losses.rolling(period).mean().replace(0, float('nan'))
    rsi = 100 - (100 / (1 + rs))
    result = rsi.iloc[-1]
    return float(result) if pd.notna(result) else 50.0

def calc_ema(prices, period):
    if len(prices) < period: return prices[-1]
    return pd.Series(prices).ewm(span=period, adjust=False).mean().iloc[-1]

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return 0.0
    high_s, low_s, close_s = pd.Series(highs), pd.Series(lows), pd.Series(closes)
    tr1 = high_s - low_s
    tr2 = abs(high_s - close_s.shift())
    tr3 = abs(low_s - close_s.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def calc_bollinger(prices, period=20, std=2.0):
    if len(prices) < period:
        return {"sma": prices[-1], "upper": prices[-1], "lower": prices[-1], "width": 0, "position": "inside"}
    series = pd.Series(prices)
    sma = series.rolling(period).mean().iloc[-1]
    std_dev = series.rolling(period).std().iloc[-1]
    upper = sma + std * std_dev
    lower = sma - std * std_dev
    width = upper - lower
    price = prices[-1]
    position = "above_upper" if price > upper else "below_lower" if price < lower else "inside"
    return {"sma": float(sma), "upper": float(upper), "lower": float(lower), "width": float(width), "position": position}

def calc_stochastic(highs, lows, closes, period=5):
    """Стохастик %K и %D (как в Базе Знаний)"""
    if len(closes) < period: return 50.0, 50.0
    high_max = max(highs[-period:])
    low_min = min(lows[-period:])
    k = 100 * (closes[-1] - low_min) / (high_max - low_min) if high_max != low_min else 50.0
    
    # Считаем %D (SMA от %K за последние 3 значения)
    k_values = []
    for i in range(3):
        idx = len(closes) - 1 - i
        if idx < period: break
        h_max = max(highs[idx-period+1:idx+1])
        l_min = min(lows[idx-period+1:idx+1])
        val = 100 * (closes[idx] - l_min) / (h_max - l_min) if h_max != l_min else 50.0
        k_values.append(val)
    d = sum(k_values) / len(k_values) if k_values else k
    return float(k), float(d)

# ============================================================
# 7. GEMINI API
# ============================================================
def clean_gemini_text(text):
    text = re.sub(r'```json\n?', '', text)
    text = re.sub(r'```\n?', '', text)
    return text.strip()

def ask_gemini(prompt, image_bytes=None):
    if not USE_NEW_SDK: return "❌ Gemini SDK не инициализирован"
    for model_name in GEMINI_MODELS:
        try:
            contents = [types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'), prompt] if image_bytes else [prompt]
            response = client.models.generate_content(model=model_name, contents=contents)
            return clean_gemini_text(response.text)
        except Exception as e:
            continue
    return "❌ Все модели Gemini недоступны."

# ============================================================
# 8. ХЕНДЛЕРЫ КОМАНД
# ============================================================
@bot.message_handler(commands=['start', 'help'])
def handle_start(message):
    user_mode[message.chat.id] = 'REAL'
    text = "🤖 GorodTradeBot v6.2\n\n🚀 LIVE: /eurusd /gbpusd /usdjpy\n📸 PHOTO: /otc или /real, затем кидай скриншот!"
    bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())

@bot.message_handler(commands=['otc'])
def handle_otc(message):
    user_mode[message.chat.id] = 'OTC'
    bot.send_message(message.chat.id, "📉 OTC режим. Кидай скриншот!", reply_markup=get_main_keyboard())

@bot.message_handler(commands=['real'])
def handle_real(message):
    user_mode[message.chat.id] = 'REAL'
    bot.send_message(message.chat.id, "📈 REAL режим. Жми /eurusd для LIVE.", reply_markup=get_main_keyboard())

@bot.message_handler(commands=list(SUPPORTED_PAIRS.keys()))
def handle_pair_command(message):
    cmd = message.text.lower().split()[0]
    if cmd in SUPPORTED_PAIRS:
        handle_forex_live(message, SUPPORTED_PAIRS[cmd])
    else:
        bot.send_message(message.chat.id, "Пара не найдена. Используйте /pairs")

# ============================================================
# 9. LIVE АНАЛИЗ (С НОВЫМИ ИНДИКАТОРАМИ ИЗ БАЗЫ ЗНАНИЙ)
# ============================================================
def handle_forex_live(message, symbol):
    loading_msg = bot.send_message(message.chat.id, f"⏳ Анализ {symbol} M1...")
    try:
        candles = get_forex_candles(symbol, "1min", 100)
        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        
        rsi = calc_rsi(closes, 7)
        bb = calc_bollinger(closes, 20, 2.0)
        stoch_k, stoch_d = calc_stochastic(highs, lows, closes, 5)
        ema_21 = calc_ema(closes, 21)
        ema_50 = calc_ema(closes, 50)
        atr = calc_atr(highs, lows, closes, 14)
        
        if ema_21 > ema_50: trend = "восходящий (EMA21 > EMA50)"
        elif ema_21 < ema_50: trend = "нисходящий (EMA21 < EMA50)"
        else: trend = "боковик"
        
        live_json = {
            "symbol": symbol, "price": closes[-1], "trend": trend,
            "rsi_7": round(rsi, 2), "stoch_k": round(stoch_k, 2), "stoch_d": round(stoch_d, 2),
            "bb_position": bb["position"], "bb_width": round(bb["width"], 5),
            "atr_14": round(atr, 5),
            "time": datetime.now().strftime("%H:%M:%S")
        }
        
        base = REAL_BASE[:15]
        # УБРАНО [:2000], чтобы не обрезался формат JSON в конце промпта!
        prompt = f"""{MASTER_PROMPT}

LIVE ДАННЫЕ REAL ФОРЕКС M1:
{json.dumps(live_json, ensure_ascii=False, indent=2)}

База знаний (фрагмент): {json.dumps(base, ensure_ascii=False)[:3000]}

ЗАДАЧА: Дай сигнал CALL/PUT/WAIT на 2-4 минуты.
ВАЖНО: 
1. Если BB width очень малый -> сужение, жди пробоя (WAIT).
2. Если ATR падает -> флет, не входить (WAIT).
3. Уверенность строго 55-75%.
Верни ТОЛЬКО JSON: {{"direction": "CALL/PUT/WAIT", "confidence": 60, "expiry_minutes": 3, "reason": "кратко"}}"""

        text = ask_gemini(prompt)
        
        header = f"📊 LIVE {symbol} | ⏱️ {live_json['time']}\n📈 Тренд: {trend}\nRSI: {rsi:.1f} | Stoch: {stoch_k:.1f}/{stoch_d:.1f}\nATR: {atr:.5f}\n\n"
        bot.edit_message_text(header + text, chat_id=message.chat.id, message_id=loading_msg.message_id)
        
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка: {e}", chat_id=message.chat.id, message_id=loading_msg.message_id)

# ============================================================
# 10. PHOTO РЕЖИМ
# ============================================================
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'REAL')
    loading_msg = bot.send_message(message.chat.id, f"⏳ Анализирую скриншот ({mode})...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        base = OTC_BASE if mode == 'OTC' else REAL_BASE
        
        prompt = f"""{MASTER_PROMPT}
Режим: {mode}
База знаний: {json.dumps(base[:12], ensure_ascii=False)[:3000]}

ЗАДАЧА: Проанализируй скриншот. 
ВНИМАНИЕ: Если видишь аномально длинную новостную свечу (импульс) -> сразу WAIT (торговля запрещена).
Если полосы Боллинджера сузились -> WAIT.
Уверенность 55-75%. Верни JSON: {{"direction": "CALL/PUT/WAIT", "confidence": 60, "expiry_minutes": 3, "reason": "кратко"}}"""

        text = ask_gemini(prompt, image_bytes=downloaded)
        bot.edit_message_text(text, chat_id=message.chat.id, message_id=loading_msg.message_id)
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка фото: {e}", chat_id=message.chat.id, message_id=loading_msg.message_id)

# ============================================================
# 11. ЗАПУСК (ИСПРАВЛЕНО __name__)
# ============================================================
if __name__ == "__main__":
    print("[START] GorodTradeBot v6.2 запуск...", flush=True)
    print(f"[INFO] Поддерживается {len(SUPPORTED_PAIRS)} пар", flush=True)
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
