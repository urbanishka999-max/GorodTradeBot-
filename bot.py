"""
GorodTradeBot v6.0 - Бинарные опционы для Pocket Option
LIVE режим через TwelveData + PHOTO режим через Gemini Vision
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
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ============================================================
# 1. KEEP-ALIVE СЕРВЕР (чтобы Render не усыплял бота)
# ============================================================
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"GorodTradeBot v6 is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def run_keepalive_server():
    port = int(os.getenv("PORT", 10000))
    try:
        server = HTTPServer(('0.0.0.0', port), KeepAliveHandler)
        print(f"[OK] Keep-alive сервер запущен на порту {port}", flush=True)
        server.serve_forever()
    except Exception as e:
        print(f"[ERROR] Keep-alive сервер: {e}", flush=True)


threading.Thread(target=run_keepalive_server, daemon=True).start()

# ============================================================
# 2. ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК
# ============================================================
def global_exception_handler(exc_type, exc_value, exc_traceback):
    print("[CRITICAL] Необработанная ошибка:", flush=True)
    traceback.print_exception(exc_type, exc_value, exc_traceback)
    sys.exit(1)

sys.excepthook = global_exception_handler

# ============================================================
# 3. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN отсутствует! Добавьте в Render -> Environment")
if not GEMINI_KEY:
    raise Exception("GEMINI_KEY отсутствует! Добавьте в Render -> Environment")

bot = telebot.TeleBot(BOT_TOKEN)

# ============================================================
# 4. ИНИЦИАЛИЗАЦИЯ GEMINI
# ============================================================
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

# Модели для перебора (от быстрых к медленным)
GEMINI_MODELS = [
    'gemini-2.0-flash',
    'gemini-1.5-flash',
    'gemini-2.0-flash-lite',
    'gemini-1.5-flash-8b'
]

# ============================================================
# 5. ЗАГРУЗКА БАЗ ЗНАНИЙ
# ============================================================
def load_json_safe(filename):
    """Безопасная загрузка JSON файла с очисткой ключей от пробелов"""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Очищаем ключи от пробелов в конце (защита от ошибок)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    keys_to_fix = list(item.keys())
                    for key in keys_to_fix:
                        if key != key.strip():
                            item[key.strip()] = item.pop(key)
        print(f"[OK] Загружено {filename}: {len(data)} карточек", flush=True)
        return data
    except FileNotFoundError:
        print(f"[WARN] Файл {filename} не найден, используем пустую базу", flush=True)
        return []
    except json.JSONDecodeError as e:
        print(f"[ERROR] Ошибка парсинга {filename}: {e}", flush=True)
        return []


OTC_BASE = load_json_safe('pocket_OTC.json')
REAL_BASE = load_json_safe('pocket_REAL_v2.json')

# Загрузка мастер-промпта
MASTER_PROMPT = ""
try:
    with open('master_prompt_v2.md', 'r', encoding='utf-8') as f:
        MASTER_PROMPT = f.read()
    print(f"[OK] Загружен master_prompt_v2.md ({len(MASTER_PROMPT)} символов)", flush=True)
except FileNotFoundError:
    MASTER_PROMPT = "Ты профессиональный скальпер бинарных опционов. Дай краткий сигнал."
    print("[WARN] master_prompt_v2.md не найден, используем базовый промпт", flush=True)

# ============================================================
# 6. КЭШ ДЛЯ ЭКОНОМИИ ЛИМИТОВ TWELVEDATA
# ============================================================
CACHE = {}
CACHE_TTL = 60  # секунд


def get_from_cache(key):
    """Получить данные из кэша если они свежие"""
    if key in CACHE:
        data, timestamp = CACHE[key]
        if time.time() - timestamp < CACHE_TTL:
            return data
    return None


def save_to_cache(key, data):
    """Сохранить данные в кэш"""
    CACHE[key] = (data, time.time())


# ============================================================
# 7. КЛАВИАТУРА
# ============================================================
from telebot import types as tb_types


def get_main_keyboard():
    markup = tb_types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=3,
        is_persistent=True
    )
    markup.add(
        tb_types.KeyboardButton("/start"),
        tb_types.KeyboardButton("/eurusd"),
        tb_types.KeyboardButton("/gbpusd"),
        tb_types.KeyboardButton("/usdjpy")
    )
    markup.add(
        tb_types.KeyboardButton("/audusd"),
        tb_types.KeyboardButton("/usdcad"),
        tb_types.KeyboardButton("/nzdusd")
    )
    markup.add(
        tb_types.KeyboardButton("/otc"),
        tb_types.KeyboardButton("/real"),
        tb_types.KeyboardButton("/pairs")
    )
    return markup


# ============================================================
# 8. ПОДДЕРЖИВАЕМЫЕ ПАРЫ
# ============================================================
SUPPORTED_PAIRS = {
    "/eurusd": "EUR/USD",
    "/gbpusd": "GBP/USD",
    "/usdjpy": "USD/JPY",
    "/audusd": "AUD/USD",
    "/usdcad": "USD/CAD",
    "/nzdusd": "NZD/USD",
    "/usdchf": "USD/CHF",
    "/eurgbp": "EUR/GBP",
    "/eurjpy": "EUR/JPY",
    "/gbpjpy": "GBP/JPY",
}

# Режим пользователя: 'OTC' или 'REAL'
user_mode = {}

# ============================================================
# 9. ПОЛУЧЕНИЕ СВЕЧЕЙ С TWELVEDATA
# ============================================================
def get_forex_candles(symbol="EUR/USD", interval="1min", limit=100):
    """Получение свечей с TwelveData с кэшированием"""
    if not TWELVE_KEY:
        raise Exception("TWELVE_KEY не установлен! Добавьте в Render -> Environment")

    cache_key = f"{symbol}_{interval}_{limit}"
    cached = get_from_cache(cache_key)
    if cached:
        print(f"[CACHE] {symbol} из кэша", flush=True)
        return cached

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": limit,
        "apikey": TWELVE_KEY,
        "format": "JSON"
    }

    print(f"[API] Запрос {symbol} {interval}...", flush=True)
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()

    if "values" not in data:
        error_msg = data.get('message', 'Unknown error')
        print(f"[ERROR] TwelveData: {error_msg}", flush=True)
        raise Exception(f"TwelveData ошибка: {error_msg}")

    candles = list(reversed(data["values"]))
    save_to_cache(cache_key, candles)
    return candles


# ============================================================
# 10. РАСЧЁТ ИНДИКАТОРОВ
# ============================================================
def calc_rsi(prices, period=7):
    """RSI с защитой от деления на ноль"""
    if len(prices) < period + 1:
        return 50.0

    series = pd.Series(prices)
    deltas = series.diff()
    gains = deltas.where(deltas > 0, 0)
    losses = -deltas.where(deltas < 0, 0)

    avg_gain = gains.rolling(period).mean()
    avg_loss = losses.rolling(period).mean()

    rs = avg_gain / avg_loss.replace(0, float('nan'))
    rsi = 100 - (100 / (1 + rs))

    result = rsi.iloc[-1]
    return float(result) if pd.notna(result) else 50.0


def calc_bollinger(prices, period=20, std=2.0):
    """Bollinger Bands"""
    if len(prices) < period:
        price = prices[-1]
        return {"sma": price, "upper": price, "lower": price,
                "price": price, "position": "inside"}

    series = pd.Series(prices)
    sma = series.rolling(period).mean().iloc[-1]
    std_dev = series.rolling(period).std().iloc[-1]
    upper = sma + std * std_dev
    lower = sma - std * std_dev
    price = prices[-1]

    if price > upper:
        position = "above_upper"
    elif price < lower:
        position = "below_lower"
    else:
        position = "inside"

    return {
        "sma": float(sma),
        "upper": float(upper),
        "lower": float(lower),
        "price": float(price),
        "position": position
    }


def calc_stochastic(highs, lows, closes, period=5):
    """Stochastic %K"""
    if len(closes) < period:
        return 50.0

    high_max = max(highs[-period:])
    low_min = min(lows[-period:])

    if high_max == low_min:
        return 50.0

    k = 100 * (closes[-1] - low_min) / (high_max - low_min)
    return float(k)


# ============================================================
# 11. ЗАПРОС К GEMINI С FALLBACK
# ============================================================
def ask_gemini(prompt, image_bytes=None):
    """Запрос к Gemini с перебором моделей"""
    if not USE_NEW_SDK:
        return "❌ Gemini SDK не инициализирован"

    last_error = None

    for model_name in GEMINI_MODELS:
        try:
            if image_bytes:
                contents = [
                    types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'),
                    prompt
                ]
            else:
                contents = [prompt]

            response = client.models.generate_content(
                model=model_name,
                contents=contents
            )
            print(f"[OK] Gemini ответ от {model_name}", flush=True)
            return response.text

        except Exception as e:
            print(f"[WARN] Модель {model_name}: {e}", flush=True)
            last_error = e
            continue

    return f"❌ Все модели недоступны. Ошибка: {last_error}"


# ============================================================
# 12. ОБРАБОТЧИКИ КОМАНД
# ============================================================
@bot.message_handler(commands=['start', 'help'])
def handle_start(message):
    user_mode[message.chat.id] = 'REAL'
    text = (
        "🤖 GorodTradeBot v6.0\n\n"
        "🚀 LIVE режим (мгновенный анализ):\n"
        "/eurusd /gbpusd /usdjpy /audusd\n"
        "/usdcad /nzdusd /usdchf /eurgbp\n"
        "/eurjpy /gbpjpy\n\n"
        "📸 PHOTO режим:\n"
        "/otc - для OTC рынка (скриншоты)\n"
        "/real - для REAL рынка (скриншоты)\n\n"
        "📊 /pairs - список всех пар"
    )
    bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())


@bot.message_handler(commands=['pairs'])
def handle_pairs(message):
    pairs_list = "\n".join([f"{cmd} → {symbol}" for cmd, symbol in SUPPORTED_PAIRS.items()])
    bot.send_message(
        message.chat.id,
        f"📊 Доступные пары:\n{pairs_list}",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=['otc'])
def handle_otc(message):
    user_mode[message.chat.id] = 'OTC'
    bot.send_message(
        message.chat.id,
        "📉 OTC режим активирован. Кидай скриншот графика!",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=['real'])
def handle_real(message):
    user_mode[message.chat.id] = 'REAL'
    bot.send_message(
        message.chat.id,
        "📈 REAL режим. Жми /eurusd или другую команду для LIVE анализа.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=list(SUPPORTED_PAIRS.keys()))
def handle_pair_command(message):
    """Обработка команд валютных пар"""
    cmd = message.text.lower().split()[0]

    if cmd in SUPPORTED_PAIRS:
        symbol = SUPPORTED_PAIRS[cmd]
        handle_forex_live(message, symbol)
    else:
        bot.send_message(message.chat.id, "Неизвестная пара. /pairs для списка.")


# ============================================================
# 13. LIVE АНАЛИЗ
# ============================================================
def handle_forex_live(message, symbol):
    """Главный LIVE режим - свечи + индикаторы + Gemini"""

    if not TWELVE_KEY:
        bot.send_message(
            message.chat.id,
            "❌ TWELVE_KEY не настроен!\n"
            "Render -> Environment -> TWELVE_KEY",
            reply_markup=get_main_keyboard()
        )
        return

    loading_msg = bot.send_message(
        message.chat.id,
        f"⏳ Анализ {symbol} M1...",
        reply_markup=get_main_keyboard()
    )

    try:
        # 1. Получаем свечи
        candles = get_forex_candles(symbol, "1min", 100)
        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]

        current_price = closes[-1]
        prev_price = closes[-2] if len(closes) > 1 else current_price

        # 2. Считаем индикаторы
        rsi = calc_rsi(closes, 7)
        bb = calc_bollinger(closes, 20, 2.0)
        stoch = calc_stochastic(highs, lows, closes, 5)

        # 3. Определяем тренд
        trend = "восходящий" if closes[-1] > closes[-5] else "нисходящий"
        rsi_signal = "перекуплен" if rsi > 70 else "перепродан" if rsi < 30 else "нейтрально"

        # 4. Формируем данные для Gemini
        live_json = {
            "symbol": symbol,
            "market_type": "REAL",
            "current_price": current_price,
            "previous_price": prev_price,
            "trend_M1": trend,
            "rsi_7": round(rsi, 2),
            "rsi_signal": rsi_signal,
            "stochastic_5": round(stoch, 2),
            "bollinger": {
                "sma": round(bb["sma"], 5),
                "upper": round(bb["upper"], 5),
                "lower": round(bb["lower"], 5),
                "position": bb["position"]
            },
            "last_5_closes": [round(c, 5) for c in closes[-5:]],
            "time": datetime.now().strftime("%H:%M:%S"),
            "expiry_recommended": "3-4 мин"
        }

        # 5. Берём релевантную часть базы
        base = REAL_BASE[:15]

        prompt = f"""{MASTER_PROMPT[:2000]}

LIVE ДАННЫЕ REAL ФОРЕКС (точные цифры):
{json.dumps(live_json, ensure_ascii=False, indent=2)}

База знаний: {json.dumps(base, ensure_ascii=False)[:3000]}

ЗАДАЧА: Ты скальпер бинарных опционов Pocket Option M1 REAL.
По данным выше дай сигнал CALL/PUT на 3-4 минуты.

Правила:
- Если RSI>70 и цена above_upper → PUT (отскок вниз)
- Если RSI<30 и цена below_lower → CALL (отскок вверх)
- Если внутри канала и нет сигнала → WAIT
- Уверенность строго 55-75%
- Верни JSON: {{"direction": "CALL/PUT/WAIT", "confidence": 55-75, "reason": "кратко"}}

Кратко!
"""

        # 6. Запрос к Gemini
        text = ask_gemini(prompt)

        # 7. Обновляем сообщение
        header = (
            f"📊 LIVE {symbol} REAL\n"
            f"⏱️ {live_json['time']} | Цена: {current_price}\n"
            f"📈 RSI(7): {rsi:.1f} ({rsi_signal})\n"
            f"📊 Stoch(5): {stoch:.1f}\n"
            f"📉 BB: {bb['position']}\n\n"
        )

        bot.edit_message_text(
            header + text,
            chat_id=message.chat.id,
            message_id=loading_msg.message_id
        )

    except Exception as e:
        print(f"[ERROR] LIVE {symbol}: {e}", flush=True)
        traceback.print_exc()
        bot.edit_message_text(
            f"❌ Ошибка {symbol}: {e}\n\nПроверь TWELVE_KEY или попробуй позже.",
            chat_id=message.chat.id,
            message_id=loading_msg.message_id
        )


# ============================================================
# 14. PHOTO РЕЖИМ
# ============================================================
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'REAL')

    loading_msg = bot.send_message(
        message.chat.id,
        f"⏳ Анализирую скриншот ({mode})...",
        reply_markup=get_main_keyboard()
    )

    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)

        base = OTC_BASE if mode == 'OTC' else REAL_BASE
        short_base = base[:12]

        prompt = f"""{MASTER_PROMPT[:2000]}

Режим: {mode}
База знаний: {json.dumps(short_base, ensure_ascii=False)[:3000]}

ЗАДАЧА: Проанализируй скриншот графика и дай сигнал для бинарных опционов.
Уверенность 55-75%. Верни JSON с направлением и причиной.
Кратко!
"""

        text = ask_gemini(prompt, image_bytes=downloaded)

        bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=loading_msg.message_id
        )

    except Exception as e:
        print(f"[ERROR] Photo: {e}", flush=True)
        bot.edit_message_text(
            f"❌ Ошибка обработки фото: {e}",
            chat_id=message.chat.id,
            message_id=loading_msg.message_id
        )


# ============================================================
# 15. ЗАПУСК
# ============================================================
if __name__ == "__main__":
    print("[START] GorodTradeBot v6.0 запуск...", flush=True)
    print(f"[INFO] Поддерживается {len(SUPPORTED_PAIRS)} пар", flush=True)
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
