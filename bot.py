"""
GorodTradeBot v8.2 Hybrid C

Архитектура:
- Python = Страж: считает индикаторы, свипы, Confluence Score.
- Score 0-1 = WAIT без ИИ.
- Score >=2 = ИИ-аналитик с подсказкой Python.
- PHOTO = всегда ИИ.
- Render Free Tier keep-alive.
- TwelveData M1 -> локальная агрегация M5/M15/H1.
"""

import os
import json
import time
import base64
import threading
import traceback
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import telebot
from telebot import types as T


# ============================================================
# 1. KEEP-ALIVE SERVER
# ============================================================

class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"GorodTradeBot v8.2 Hybrid C is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_keepalive():
    port = int(os.getenv("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
        print(f"[OK] Keep-alive server started on port {port}", flush=True)
        server.serve_forever()
    except Exception as e:
        print(f"[ERROR] Keep-alive: {e}", flush=True)


threading.Thread(target=run_keepalive, daemon=True).start()


# ============================================================
# 2. ENV
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN обязателен в Environment Render")
if not GEMINI_KEY:
    raise Exception("GEMINI_KEY обязателен в Environment Render")
if not TWELVE_KEY:
    raise Exception("TWELVE_KEY обязателен в Environment Render")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)


# ============================================================
# 3. SAFE JSON LOAD
# ============================================================

def clean_json_obj(obj):
    """
    Убирает случайные пробелы в ключах и строковых значениях JSON.
    Важно для старых файлов, где было: "indicator ", "close ", "/btc ".
    """
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            nk = k.strip() if isinstance(k, str) else k
            cleaned[nk] = clean_json_obj(v)
        return cleaned
    if isinstance(obj, list):
        return [clean_json_obj(x) for x in obj]
    if isinstance(obj, str):
        return obj.strip()
    return obj


def load_json_first(files):
    for fn in files:
        try:
            with open(fn, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = clean_json_obj(data)
            print(f"[OK] {fn}: {len(data)} карточек", flush=True)
            return data
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"[WARN] Ошибка загрузки {fn}: {e}", flush=True)
            return []
    print(f"[WARN] Не найден ни один файл из списка: {files}", flush=True)
    return []


OTC_BASE = load_json_first(["pocket_OTC.json", "pocket_x5f_OTC.json"])
REAL_BASE = load_json_first(["pocket_REAL_v2.json", "pocket_x5f_REAL_x5f_v2.json"])

try:
    with open("master_prompt_v2.md", "r", encoding="utf-8") as f:
        MASTER_PROMPT = f.read()
    print(f"[OK] master_prompt_v2.md: {len(MASTER_PROMPT)} символов", flush=True)
except Exception:
    MASTER_PROMPT = (
        "Ты профессиональный скальпер бинарных опционов Pocket Option. "
        "Дай краткий сигнал CALL/PUT/WAIT с объяснением. "
        "Не обещай прибыль. Уверенность только 55-75%."
    )
    print("[WARN] master_prompt_v2.md не найден, используется базовый prompt", flush=True)


# ============================================================
# 4. CACHE
# ============================================================

CACHE = {}
CACHE_TTL = 30


def cache_get(key):
    item = CACHE.get(key)
    if not item:
        return None
    data, ts = item
    if time.time() - ts < CACHE_TTL:
        return data
    return None


def cache_set(key, data):
    CACHE[key] = (data, time.time())


# ============================================================
# 5. MARKETS / PAIRS / TIMEFRAMES
# ============================================================

PAIRS = {
    "/eurusd": "EUR/USD",
    "/gbpusd": "GBP/USD",
    "/usdjpy": "USD/JPY",
    "/audusd": "AUD/USD",
    "/usdcad": "USD/CAD",
    "/nzdusd": "NZD/USD",
    "/usdchf": "USD/CHF",
    "/eurgbp": "EUR/GBP",
    "/eurjpy": "EUR/JPY",

    "/btc": "BTC/USD",
    "/eth": "ETH/USD",
    "/sol": "SOL/USD",
}

TIMEFRAMES = {
    "1": {"tf": 1, "trigger": "m1", "trend": "m5", "exp": "1-3"},
    "2": {"tf": 2, "trigger": "m1", "trend": "m5", "exp": "2-4"},
    "3": {"tf": 3, "trigger": "m1", "trend": "m5", "exp": "3-5"},
    "5": {"tf": 5, "trigger": "m5", "trend": "m15", "exp": "5-10"},
    "15": {"tf": 15, "trigger": "m15", "trend": "h1", "exp": "15-30"},
}

user_pending = {}
user_photo_mode = {}


def is_crypto_symbol(symbol):
    return any(x in symbol.upper() for x in ["BTC", "ETH", "SOL"])


def is_forex_open():
    """
    Простой UTC-фильтр Forex.
    Forex закрыт: суббота, воскресенье до 22 UTC, пятница после 22 UTC.
    """
    now = datetime.now(timezone.utc)
    wd = now.weekday()
    h = now.hour

    if wd == 5:
        return False
    if wd == 6 and h < 22:
        return False
    if wd == 4 and h >= 22:
        return False
    return True


# ============================================================
# 6. KEYBOARDS
# ============================================================

def kb_main():
    """
    ReplyKeyboardMarkup — только для send_message.
    Нельзя передавать в edit_message_text.
    """
    m = T.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3, is_persistent=True)

    if is_forex_open():
        m.add(T.KeyboardButton("/eurusd"), T.KeyboardButton("/gbpusd"), T.KeyboardButton("/btc"))
        m.add(T.KeyboardButton("/usdjpy"), T.KeyboardButton("/otc"), T.KeyboardButton("/real"))
    else:
        m.add(T.KeyboardButton("/btc"), T.KeyboardButton("/eth"), T.KeyboardButton("/sol"))
        m.add(T.KeyboardButton("/eurusd"), T.KeyboardButton("/otc"), T.KeyboardButton("/real"))

    return m


def kb_inline_main():
    """
    InlineKeyboardMarkup — для edit_message_text.
    Все callback_data обязательно должны иметь handlers.
    """
    m = T.InlineKeyboardMarkup(row_width=3)

    if is_forex_open():
        m.add(
            T.InlineKeyboardButton("EUR/USD", callback_data="pair_eurusd"),
            T.InlineKeyboardButton("GBP/USD", callback_data="pair_gbpusd"),
            T.InlineKeyboardButton("BTC", callback_data="pair_btc"),
        )
        m.add(
            T.InlineKeyboardButton("USD/JPY", callback_data="pair_usdjpy"),
            T.InlineKeyboardButton("📉 OTC", callback_data="mode_otc"),
            T.InlineKeyboardButton("📈 REAL", callback_data="mode_real"),
        )
    else:
        m.add(
            T.InlineKeyboardButton("BTC", callback_data="pair_btc"),
            T.InlineKeyboardButton("ETH", callback_data="pair_eth"),
            T.InlineKeyboardButton("SOL", callback_data="pair_sol"),
        )
        m.add(
            T.InlineKeyboardButton("📉 OTC", callback_data="mode_otc"),
            T.InlineKeyboardButton("📈 REAL", callback_data="mode_real"),
        )

    return m


def kb_tf():
    m = T.InlineKeyboardMarkup(row_width=5)
    m.add(
        T.InlineKeyboardButton("1м", callback_data="tf_1"),
        T.InlineKeyboardButton("2м", callback_data="tf_2"),
        T.InlineKeyboardButton("3м", callback_data="tf_3"),
        T.InlineKeyboardButton("5м", callback_data="tf_5"),
        T.InlineKeyboardButton("15м", callback_data="tf_15"),
    )
    return m


def safe_edit(text, chat_id, msg_id, reply_markup=None):
    """
    Безопасный edit:
    - только InlineKeyboardMarkup или None;
    - обрезает слишком длинный текст;
    - не падает на Telegram errors.
    """
    try:
        if text is None:
            text = "Пустой ответ."
        text = str(text)

        if len(text) > 3900:
            text = text[:3800] + "\n\n…Ответ сокращён из-за лимита Telegram."

        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=reply_markup if reply_markup is not None else kb_inline_main()
        )
    except Exception as e:
        print(f"[WARN] edit_message_text failed: {e}", flush=True)


# ============================================================
# 7. DATA / RESAMPLE
# ============================================================

def parse_dt(s):
    """
    TwelveData datetime обычно: 'YYYY-MM-DD HH:MM:SS'
    """
    s = str(s).strip()
    s = s.replace("T", " ").replace("Z", "")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def get_candles_m1(symbol, limit=300):
    key = f"{symbol}_m1_{limit}"
    cached = cache_get(key)
    if cached:
        print(f"[CACHE] {symbol} M1", flush=True)
        return cached

    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": "1min",
            "outputsize": limit,
            "apikey": TWELVE_KEY,
            "format": "JSON",
        },
        timeout=20,
    )
    data = r.json()

    if "values" not in data:
        raise Exception(f"TwelveData: {data.get('message', data)}")

    candles = list(reversed(data["values"]))
    cache_set(key, candles)

    print(f"[API] {symbol} M1: {len(candles)}", flush=True)
    return candles


def resample_m1_to_tf(candles_m1, target_minutes, is_crypto=False):
    if not candles_m1:
        return []

    parsed = []
    for c in candles_m1:
        try:
            if c.get("close") is None:
                continue
            dt = parse_dt(c["datetime"])
            parsed.append((dt, c))
        except Exception:
            continue

    if not parsed:
        return []

    parsed.sort(key=lambda x: x[0])
    first_dt = parsed[0][0]
    last_dt = parsed[-1][0]

    if target_minutes == 60:
        current_start = first_dt.replace(minute=0, second=0, microsecond=0)
    else:
        minute = (first_dt.minute // target_minutes) * target_minutes
        current_start = first_dt.replace(minute=minute, second=0, microsecond=0)

    result = []
    min_required = int(target_minutes * (0.5 if is_crypto else 0.8))
    min_required = max(1, min_required)

    while current_start <= last_dt:
        current_end = current_start + timedelta(minutes=target_minutes)
        chunk = [c for dt, c in parsed if current_start <= dt < current_end]

        if len(chunk) >= min_required:
            try:
                result.append({
                    "datetime": current_start.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": float(chunk[0]["open"]),
                    "high": max(float(x["high"]) for x in chunk),
                    "low": min(float(x["low"]) for x in chunk),
                    "close": float(chunk[-1]["close"]),
                })
            except Exception as e:
                print(f"[WARN] aggregation skip {current_start}: {e}", flush=True)

        current_start = current_end

    print(f"[AGG] M1->M{target_minutes}: {len(candles_m1)}->{len(result)}", flush=True)
    return result


def get_all_tfs(symbol):
    m1 = get_candles_m1(symbol, 300)
    crypto = is_crypto_symbol(symbol)
    return {
        "m1": m1,
        "m5": resample_m1_to_tf(m1, 5, crypto),
        "m15": resample_m1_to_tf(m1, 15, crypto),
        "h1": resample_m1_to_tf(m1, 60, crypto),
    }


# ============================================================
# 8. INDICATORS
# ============================================================

def ema(prices, period):
    if not prices:
        return 0.0
    if len(prices) < period:
        return prices[-1]

    k = 2 / (period + 1)
    e = sum(prices[:period]) / period
    for p in prices[period:]:
        e = p * k + e * (1 - k)
    return e


def ema_series(prices, period):
    if not prices:
        return []
    k = 2 / (period + 1)
    out = [prices[0]]
    for p in prices[1:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period

    if al == 0:
        return 100.0

    rs = ag / al
    return 100 - (100 / (1 + rs))


def rsi_last_values(prices, period=14, count=6):
    vals = []
    for end in range(max(period + 1, len(prices) - count + 1), len(prices) + 1):
        vals.append(round(rsi(prices[:end], period), 2))
    return vals[-count:]


def bollinger(prices, period=20, mult=2.0):
    if len(prices) < period:
        p = prices[-1] if prices else 0.0
        return {"sma": p, "upper": p, "lower": p, "pos": "inside"}

    s = prices[-period:]
    sma = sum(s) / period
    var = sum((x - sma) ** 2 for x in s) / period
    std = var ** 0.5

    upper = sma + mult * std
    lower = sma - mult * std
    p = prices[-1]

    if p > upper:
        pos = "above_upper"
    elif p < lower:
        pos = "below_lower"
    else:
        pos = "inside"

    return {"sma": sma, "upper": upper, "lower": lower, "pos": pos}


def stoch(highs, lows, closes, period=14):
    if len(closes) < period:
        return 50.0

    h = max(highs[-period:])
    l = min(lows[-period:])

    if h == l:
        return 50.0

    return 100 * (closes[-1] - l) / (h - l)


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0.0

    trs = []
    for i in range(-period, 0):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)

    return sum(trs) / period


def trend_direction(prices):
    if len(prices) < 10:
        return "неизвестно"

    recent = prices[-10:]
    up = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
    down = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])

    if up > down + 2:
        return "восходящий"
    if down > up + 2:
        return "нисходящий"
    return "боковик"


def macd_histogram(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal:
        return 0.0, 0.0

    ef = ema_series(prices, fast)
    es = ema_series(prices, slow)
    macd_line = [a - b for a, b in zip(ef, es)]
    sig = ema_series(macd_line, signal)
    hist = macd_line[-1] - sig[-1]
    hist_prev = macd_line[-2] - sig[-2]
    return hist, hist_prev


def elder_impulse_state(prices):
    """
    Упрощенный Elder Impulse:
    green = EMA13 растет и MACD histogram растет
    red = EMA13 падает и MACD histogram падает
    blue = конфликт
    """
    if len(prices) < 40:
        return "unknown"

    e = ema_series(prices, 13)
    hist, hist_prev = macd_histogram(prices)

    ema_up = e[-1] > e[-2]
    ema_down = e[-1] < e[-2]
    hist_up = hist > hist_prev
    hist_down = hist < hist_prev

    if ema_up and hist_up:
        return "green"
    if ema_down and hist_down:
        return "red"
    return "blue"


def candle_hint(candles):
    """
    Легкая подсказка по последним свечам.
    Не является полноценным детектором всех паттернов.
    ИИ получает OHLC и может уточнить.
    """
    if len(candles) < 3:
        return "недостаточно свечей"

    c1 = candles[-2]
    c2 = candles[-1]

    o1, h1, l1, cl1 = map(float, [c1["open"], c1["high"], c1["low"], c1["close"]])
    o2, h2, l2, cl2 = map(float, [c2["open"], c2["high"], c2["low"], c2["close"]])

    body2 = abs(cl2 - o2)
    rng2 = h2 - l2 if h2 != l2 else 0.0000001
    upper_wick = h2 - max(o2, cl2)
    lower_wick = min(o2, cl2) - l2

    hints = []

    if lower_wick > body2 * 2 and upper_wick < body2:
        hints.append("pinbar/молот снизу")
    if upper_wick > body2 * 2 and lower_wick < body2:
        hints.append("pinbar/падающая звезда сверху")

    if cl2 > o2 and cl1 < o1 and cl2 >= o1 and o2 <= cl1:
        hints.append("бычье поглощение")
    if cl2 < o2 and cl1 > o1 and o2 >= cl1 and cl2 <= o1:
        hints.append("медвежье поглощение")

    if rng2 > 0 and body2 / rng2 < 0.15:
        hints.append("дожи/пауза")

    return ", ".join(hints) if hints else "явного свечного паттерна нет"


# ============================================================
# 9. KNOWLEDGE SELECTION
# ============================================================

def select_relevant_base(base, live, max_cards=12):
    """
    Не берем просто первые карточки.
    Отбираем релевантные по текущим фактам.
    """
    if not base:
        return []

    scored = []

    rsi_v = live.get("rsi_current", 50)
    stoch_v = live.get("stochastic_current", 50)
    bb_pos = live.get("bollinger_position", "inside")
    sweep = live.get("bearish_liquidity_sweep") or live.get("bullish_liquidity_sweep")

    for card in base:
        name = str(card.get("indicator", "")).lower()
        rel = str(card.get("relevance", "")).upper()
        score = 0

        if "rsi" in name and (rsi_v > 70 or rsi_v < 30):
            score += 5
        if "bollinger" in name and bb_pos != "inside":
            score += 5
        if "stochastic" in name and (stoch_v > 80 or stoch_v < 20):
            score += 4
        if sweep and ("bof" in name or "неудавшийся" in name or "tst" in name or "price action" in name):
            score += 6
        if "свеч" in name or "молот" in name or "поглощ" in name or "харами" in name or "звезда" in name:
            score += 3
        if "микро" in name:
            score += 3
        if "elder" in name or "элдер" in name:
            score += 2
        if "VERY HIGH" in rel:
            score += 2
        elif "HIGH" in rel:
            score += 1

        scored.append((score, card))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:max_cards]]


# ============================================================
# 10. GEMINI
# ============================================================

DEFAULT_GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
]

ENV_MODELS = os.getenv("GEMINI_MODELS", "").strip()
if ENV_MODELS:
    GEMINI_MODELS = [x.strip() for x in ENV_MODELS.split(",") if x.strip()]
else:
    GEMINI_MODELS = DEFAULT_GEMINI_MODELS


def ask_gemini(prompt, image_bytes=None, preferred_model=None):
    """
    Gemini API:
    - preferred_model ставится первой, если задана;
    - затем fallback;
    - 2 попытки на модель;
    - timeout 120 сек.
    """
    models = list(GEMINI_MODELS)

    if preferred_model:
        if preferred_model in models:
            models.remove(preferred_model)
        models.insert(0, preferred_model)

    for model in models:
        for attempt in range(2):
            try:
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={GEMINI_KEY}"
                )

                parts = [{"text": prompt}]
                if image_bytes:
                    parts.append({
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": base64.b64encode(image_bytes).decode()
                        }
                    })

                payload = {
                    "contents": [{"parts": parts}],
                    "generationConfig": {
                        "temperature": 0.25,
                        "maxOutputTokens": 1024,
                    }
                }

                print(f"[GEMINI] {model} попытка {attempt + 1}/2", flush=True)
                r = requests.post(url, json=payload, timeout=120)

                if r.status_code == 200:
                    data = r.json()
                    if "candidates" in data and data["candidates"]:
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        if text and text.strip():
                            return text

                print(f"[GEMINI] {model} status={r.status_code}: {r.text[:200]}", flush=True)

            except requests.exceptions.Timeout:
                print(f"[GEMINI] {model} timeout", flush=True)
            except Exception as e:
                print(f"[GEMINI] {model} error: {e}", flush=True)

            if attempt == 0:
                time.sleep(2)

    return "❌ Gemini недоступен. Попробуйте позже."


# ============================================================
# 11. COMMAND HANDLERS
# ============================================================

@bot.message_handler(commands=["start", "help"])
def cmd_start(m):
    status = "REAL открыт" if is_forex_open() else "REAL закрыт, доступна CRYPTO 24/7"
    text = (
        "GorodTradeBot v8.2 Hybrid C\n\n"
        f"{status}\n\n"
        "REAL:\n"
        "/eurusd /gbpusd /usdjpy /audusd /usdcad\n\n"
        "CRYPTO 24/7:\n"
        "/btc /eth /sol\n\n"
        "PHOTO:\n"
        "/otc — режим OTC-скриншотов\n"
        "/real — режим REAL-скриншотов\n\n"
        "Логика:\n"
        "Score 0-1 → WAIT без ИИ\n"
        "Score 2+ → ИИ-анализ с подсказкой Python"
    )
    bot.send_message(m.chat.id, text, reply_markup=kb_main())


@bot.message_handler(commands=["otc"])
def cmd_otc(m):
    user_photo_mode[m.chat.id] = "OTC"
    bot.send_message(m.chat.id, "📉 OTC режим для скриншотов активирован.", reply_markup=kb_main())


@bot.message_handler(commands=["real"])
def cmd_real(m):
    user_photo_mode[m.chat.id] = "REAL"
    bot.send_message(m.chat.id, "📈 REAL режим для скриншотов активирован.", reply_markup=kb_main())


@bot.message_handler(commands=[k.lstrip("/") for k in PAIRS.keys()])
def cmd_pair(m):
    cmd = m.text.lower().split()[0].strip()
    if cmd not in PAIRS:
        return

    symbol = PAIRS[cmd]

    if not is_crypto_symbol(symbol) and not is_forex_open():
        bot.send_message(
            m.chat.id,
            f"⚠️ {symbol} REAL сейчас закрыт.\nДоступны /btc /eth /sol 24/7.",
            reply_markup=kb_main()
        )
        return

    user_pending[m.chat.id] = symbol
    bot.send_message(m.chat.id, f"Пара: {symbol}\nВыбери ТФ:", reply_markup=kb_tf())


@bot.callback_query_handler(func=lambda c: c.data.startswith("pair_"))
def cb_pair(call):
    key = call.data.replace("pair_", "", 1).strip()
    cmd = "/" + key

    if cmd not in PAIRS:
        bot.answer_callback_query(call.id, "Неизвестная пара")
        return

    symbol = PAIRS[cmd]

    if not is_crypto_symbol(symbol) and not is_forex_open():
        bot.answer_callback_query(call.id, "REAL закрыт. Используй BTC/ETH/SOL.")
        return

    user_pending[call.message.chat.id] = symbol
    bot.answer_callback_query(call.id, f"{symbol}")
    safe_edit(
        f"Пара: {symbol}\nВыбери ТФ:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=kb_tf()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("mode_"))
def cb_mode(call):
    mode = call.data.replace("mode_", "", 1).upper().strip()
    if mode not in ["OTC", "REAL"]:
        bot.answer_callback_query(call.id, "Неизвестный режим")
        return

    user_photo_mode[call.message.chat.id] = mode
    bot.answer_callback_query(call.id, f"{mode} режим")
    safe_edit(
        f"✅ Режим скриншотов: {mode}\nТеперь отправь скрин графика или выбери пару.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=kb_inline_main()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("tf_"))
def cb_tf(call):
    tf_key = call.data.split("_", 1)[1].strip()
    symbol = user_pending.get(call.message.chat.id)

    if not symbol:
        bot.answer_callback_query(call.id, "Сначала выбери пару")
        return

    if tf_key not in TIMEFRAMES:
        bot.answer_callback_query(call.id, "Неизвестный ТФ")
        return

    if not is_crypto_symbol(symbol) and not is_forex_open():
        bot.answer_callback_query(call.id, "REAL закрыт. Используй CRYPTO.")
        return

    tf = TIMEFRAMES[tf_key]
    bot.answer_callback_query(call.id, f"{symbol} {tf_key}м")
    safe_edit(
        f"⏳ Анализирую {symbol} {tf_key}м...",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=None
    )

    run_analysis(call.message.chat.id, call.message.message_id, symbol, tf_key, tf)


# ============================================================
# 12. MAIN ANALYSIS — HYBRID C
# ============================================================

def run_analysis(chat_id, msg_id, symbol, tf_key, tf):
    try:
        all_tfs = get_all_tfs(symbol)

        trigger_tf = tf["trigger"]
        trend_tf = tf["trend"]

        trigger = all_tfs.get(trigger_tf)
        trend = all_tfs.get(trend_tf)

        if not trigger or not trend:
            raise Exception(f"Нет данных {trigger_tf}/{trend_tf}")

        t_closes = [float(c["close"]) for c in trigger]
        t_highs = [float(c["high"]) for c in trigger]
        t_lows = [float(c["low"]) for c in trigger]
        tr_closes = [float(c["close"]) for c in trend]

        if len(t_closes) < 25:
            raise Exception(f"Недостаточно свечей на {trigger_tf}: {len(t_closes)}")

        price = t_closes[-1]
        current_high = t_highs[-1]
        current_low = t_lows[-1]

        rsi_period = 7 if int(tf_key) <= 3 else 14
        stoch_period = 5 if int(tf_key) <= 3 else 14

        rsi_v = rsi(t_closes, rsi_period)
        bb_v = bollinger(t_closes, 20, 2.0)
        stoch_v = stoch(t_highs, t_lows, t_closes, stoch_period)
        atr_v = atr(t_highs, t_lows, t_closes, 14)
        ema21 = ema(tr_closes, 21)
        ema50 = ema(tr_closes, 50)
        trend_dir = trend_direction(tr_closes)
        elder = elder_impulse_state(t_closes)

        recent_high = max(t_highs[-6:-1]) if len(t_highs) > 5 else current_high
        recent_low = min(t_lows[-6:-1]) if len(t_lows) > 5 else current_low

        bearish_sweep = (current_high > recent_high) and (price < recent_high)
        bullish_sweep = (current_low < recent_low) and (price > recent_low)

        # ------------------------------
        # Confluence Score: max 5
        # ------------------------------
        score = 0
        reasons = []

        # 1. Четкий старший тренд
        if trend_dir in ["восходящий", "нисходящий"]:
            score += 1
            reasons.append(f"Тренд {trend_tf}: {trend_dir}")

        # 2. Свип ликвидности — весомый фактор
        if bearish_sweep:
            score += 2
            reasons.append("Медвежий свип ликвидности")
        elif bullish_sweep:
            score += 2
            reasons.append("Бычий свип ликвидности")

        # 3. Bollinger экстремум
        if bb_v["pos"] in ["above_upper", "below_lower"]:
            score += 1
            reasons.append(f"Bollinger: {bb_v['pos']}")

        # 4. RSI/Stoch экстремум
        if rsi_v > 70 or rsi_v < 30 or stoch_v > 80 or stoch_v < 20:
            score += 1
            reasons.append(f"RSI {rsi_v:.1f} / Stoch {stoch_v:.1f} экстремум")

        market_label = "CRYPTO" if is_crypto_symbol(symbol) else "REAL"
        now_str = datetime.now().strftime("%H:%M:%S")

        base_header = (
            f"{symbol} | {tf_key}м | {now_str} | {market_label}\n"
            f"Цена: {price} | Тренд {trend_tf}: {trend_dir}\n"
            f"EMA21/50: {ema21:.5f}/{ema50:.5f} | RSI: {rsi_v:.1f} Stoch: {stoch_v:.1f}\n"
            f"BB: {bb_v['pos']} | ATR: {atr_v:.5f} | Экспирация: {tf['exp']}м\n"
            f"Python Score: {score}/5 | Elder: {elder}\n"
        )

        # -----------------------------------------------------
        # Hybrid C: Score 0-1 -> WAIT без ИИ
        # -----------------------------------------------------
        if score <= 1:
            text = (
                base_header
                + "\n⛔ WAIT — недостаточно факторов для входа.\n\n"
                + "Причины:\n"
            )
            if reasons:
                text += "\n".join(f"• {r}" for r in reasons)
            else:
                text += "• Нет выраженного сетапа."

            text += (
                "\n\nИИ не вызывался: Score 0-1 означает мусорную зону, "
                "где модель часто начинает выдумывать сделку."
            )

            safe_edit(text, chat_id, msg_id, reply_markup=kb_inline_main())
            return

        # -----------------------------------------------------
        # Score >=2 -> ИИ с подсказкой Python
        # -----------------------------------------------------
        rsi_hist = rsi_last_values(t_closes, rsi_period, 6)
        price_hist = [round(x, 5) for x in t_closes[-6:]]

        last_candles = []
        for c in trigger[-8:]:
            last_candles.append({
                "time": c.get("datetime"),
                "open": round(float(c["open"]), 5),
                "high": round(float(c["high"]), 5),
                "low": round(float(c["low"]), 5),
                "close": round(float(c["close"]), 5),
            })

        live = {
            "symbol": symbol,
            "market_type": market_label,
            "signal_tf": int(tf_key),
            "trigger_tf": trigger_tf,
            "trend_tf": trend_tf,
            "price": price,
            "trend_direction": trend_dir,
            "ema21_trend": round(ema21, 5),
            "ema50_trend": round(ema50, 5),
            "rsi_current": round(rsi_v, 2),
            "rsi_history": rsi_hist,
            "price_history": price_hist,
            "stochastic_current": round(stoch_v, 2),
            "bollinger": {
                "sma": round(bb_v["sma"], 5),
                "upper": round(bb_v["upper"], 5),
                "lower": round(bb_v["lower"], 5),
                "position": bb_v["pos"],
            },
            "atr_14": round(atr_v, 5),
            "elder_impulse": elder,
            "bearish_liquidity_sweep": bearish_sweep,
            "bullish_liquidity_sweep": bullish_sweep,
            "candle_hint": candle_hint(trigger),
            "last_candles": last_candles,
            "expiry": tf["exp"],
            "time": now_str,
            "python_score": score,
            "python_reasons": reasons,
        }

        # Для CRYPTO используем REAL_BASE, потому что crypto — реальный рынок 24/7.
        base = REAL_BASE if market_label in ["REAL", "CRYPTO"] else OTC_BASE
        relevant_base = select_relevant_base(base, live, max_cards=12)
        base_text = json.dumps(relevant_base, ensure_ascii=False)[:3500]

        preferred_model = "gemini-3.5-flash-lite" if score == 2 else "gemini-3.8-flash"

        prompt = f"""{MASTER_PROMPT}

РЕЖИМ: {market_label}
ВАЖНО:
- Это бинарные опционы, прогноз на {tf['exp']} минут.
- Не обещай прибыль.
- Если сетап слабый — WAIT.
- Уверенность только 55-75%.
- Для CRYPTO не применяй OTC-логику синтетики. Используй реальные рыночные правила, волатильность и структуру.

PYTHON-СТРАЖ УЖЕ ПОСЧИТАЛ:
- Confluence Score: {score}/5
- Факты: {', '.join(reasons)}
- Свип: bearish={bearish_sweep}, bullish={bullish_sweep}
- Тренд {trend_tf}: {trend_dir}
- Elder Impulse: {elder}
- RSI: {rsi_v:.2f}
- Stoch: {stoch_v:.2f}
- Bollinger: {bb_v['pos']}
- Candle hint: {live['candle_hint']}

ПРАВИЛА ГИБРИДА C:
1. Score = 2: сделка спорная. Ищи только качественный редкий сетап: BOF, TST, Micro-M/W, Харами, поглощение у уровня. Если не видно — WAIT.
2. Score >= 3: можно дать CALL/PUT, но только если направление не противоречит фактам.
3. Если сигнал против старшего тренда — нужен свип/дивергенция/сильный разворотный паттерн. Иначе WAIT.
4. Если Elder = blue, будь осторожен: конфликт импульса, чаще WAIT.
5. Назови конкретный паттерн из базы знаний, если он реально есть. Не выдумывай паттерн.
6. Кратко объясни, почему цена может закрыться выше/ниже через экспирацию.

LIVE ДАННЫЕ:
{json.dumps(live, ensure_ascii=False, indent=2)}

БАЗА ЗНАНИЙ:
{base_text}

ФОРМАТ:
Дай короткий текст и JSON:
{{
  "direction": "CALL/PUT/WAIT",
  "confidence": 55-75,
  "market_type": "{market_label}",
  "expiry_minutes": число,
  "pattern": "название паттерна или нет паттерна",
  "reason": "краткая причина",
  "cancel_condition": "когда сценарий ломается"
}}
"""

        ai_text = ask_gemini(prompt, preferred_model=preferred_model)

        final_text = base_header + "\n" + ai_text
        safe_edit(final_text, chat_id, msg_id, reply_markup=kb_inline_main())

    except Exception as e:
        print(f"[ERROR] run_analysis {symbol}: {e}", flush=True)
        traceback.print_exc()
        safe_edit(f"Ошибка анализа {symbol}: {e}", chat_id, msg_id, reply_markup=kb_inline_main())


# ============================================================
# 13. PHOTO MODE
# ============================================================

@bot.message_handler(content_types=["photo"])
def handle_photo(m):
    mode = user_photo_mode.get(m.chat.id, "REAL")
    loading = bot.send_message(m.chat.id, f"⏳ Анализирую скриншот ({mode})...", reply_markup=kb_main())

    try:
        fi = bot.get_file(m.photo[-1].file_id)
        img = bot.download_file(fi.file_path)

        base = OTC_BASE if mode == "OTC" else REAL_BASE
        short_base = json.dumps(base[:14], ensure_ascii=False)[:3500]

        prompt = f"""{MASTER_PROMPT}

PHOTO-РЕЖИМ: {mode}

Скриншот качественный, данные на графике видны.
Твоя задача — прочитать график и дать сигнал для бинарных опционов.

ВАЖНО:
1. Определи актив и тип рынка: OTC или REAL, если видно.
2. Посмотри таймер свечи и время экспирации, если видны.
3. Найди уровень, свечной паттерн, Bollinger/RSI/Stoch, если они видны.
4. Для OTC: отскоки, BOF, TST, границы диапазона. Не используй трендовые пробои как основной сигнал.
5. Для REAL: тренд, уровень, откат/пробой/BOF, свечной триггер.
6. Если паттерн не сформирован — WAIT.
7. Уверенность только 55-75%.

БАЗА ЗНАНИЙ:
{short_base}

Верни краткий анализ и JSON:
{{
  "direction": "CALL/PUT/WAIT",
  "confidence": 55-75,
  "market_type": "{mode}",
  "expiry_minutes": число,
  "pattern": "паттерн из базы или нет паттерна",
  "reason": "краткое объяснение",
  "cancel_condition": "условие отмены"
}}
"""

        text = ask_gemini(prompt, image_bytes=img, preferred_model="gemini-3.8-flash")
        safe_edit(text, m.chat.id, loading.message_id, reply_markup=kb_inline_main())

    except Exception as e:
        print(f"[ERROR] Photo: {e}", flush=True)
        traceback.print_exc()
        safe_edit(f"Ошибка анализа скриншота: {e}", m.chat.id, loading.message_id, reply_markup=kb_inline_main())


# ============================================================
# 14. UNKNOWN TEXT
# ============================================================

@bot.message_handler(content_types=["text"])
def handle_text(m):
    txt = m.text.strip().lower()

    if txt in ["start", "help", "старт", "помощь"]:
        cmd_start(m)
        return

    bot.send_message(
        m.chat.id,
        "Выбери команду пары или отправь скриншот.\n"
        "CRYPTO 24/7: /btc /eth /sol\n"
        "REAL: /eurusd /gbpusd /usdjpy",
        reply_markup=kb_main()
    )


# ============================================================
# 15. START
# ============================================================

if __name__ == "__main__":
    print("[START] GorodTradeBot v8.2 Hybrid C", flush=True)
    print(f"[INFO] Forex open: {is_forex_open()}", flush=True)
    print(f"[INFO]Pairs: {len(PAIRS)}, TF: {len(TIMEFRAMES)}", flush=True)
    print(f"[INFO] OTC base: {len(OTC_BASE)}", flush=True)
    print(f"[INFO] REAL base: {len(REAL_BASE)}", flush=True)
    print(f"[INFO] Gemini models: {GEMINI_MODELS}", flush=True)
    print("[INFO] Hybrid C: Score 0-1 WAIT without AI, Score 2+ AI", flush=True)

    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=60,
        skip_pending=True
    )
