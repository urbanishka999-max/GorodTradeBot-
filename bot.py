"""
GorodTradeBot v8.1 Hybrid C
Python = Страж (считает Score, свип, тренд)
ИИ = Аналитик (вызывается только при Score >= 2)
Актуальные модели Gemini (Ноябрь 2026)
"""
import telebot
import json
import os
import re
import threading
import requests
import base64
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from telebot import types as T

# ============================================================
# 1. KEEP-ALIVE СЕРВЕР
# ============================================================
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"GorodTradeBot v8.1 Hybrid C")
    
    def log_message(self, format, *args):
        pass

def run_keepalive():
    port = int(os.getenv("PORT", 10000))
    try:
        HTTPServer(('0.0.0.0', port), KeepAliveHandler).serve_forever()
    except Exception as e:
        print(f"[ERROR] Keep-alive: {e}", flush=True)

threading.Thread(target=run_keepalive, daemon=True).start()

# ============================================================
# 2. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")

if not BOT_TOKEN or not GEMINI_KEY or not TWELVE_KEY:
    raise Exception("BOT_TOKEN, GEMINI_KEY, TWELVE_KEY обязательны в ENV Render")

bot = telebot.TeleBot(BOT_TOKEN)

# ============================================================
# 3. ЗАГРУЗКА БАЗ ЗНАНИЙ
# ============================================================
def load_json(fn):
    try:
        with open(fn, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Автоочистка ключей от пробелов
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    keys_to_fix = list(item.keys())
                    for key in keys_to_fix:
                        if key != key.strip():
                            item[key.strip()] = item.pop(key)
        print(f"[OK] {fn}: {len(data)} карточек", flush=True)
        return data
    except Exception as e:
        print(f"[WARN] {fn} не найден: {e}", flush=True)
        return []

OTC_BASE = load_json('pocket_OTC.json')
REAL_BASE = load_json('pocket_REAL_v2.json')

MASTER_PROMPT = ""
try:
    with open('master_prompt_v2.md', 'r', encoding='utf-8') as f:
        MASTER_PROMPT = f.read()
    print(f"[OK] master_prompt_v2.md загружен ({len(MASTER_PROMPT)} символов)", flush=True)
except Exception:
    MASTER_PROMPT = "Ты профессиональный скальпер бинарных опционов Pocket Option."
    print("[WARN] master_prompt_v2.md не найден", flush=True)

# ============================================================
# 4. КЭШ
# ============================================================
CACHE = {}
CACHE_TTL = 30

def cache_get(k):
    if k in CACHE:
        d, t = CACHE[k]
        if time.time() - t < CACHE_TTL:
            return d
    return None

def cache_set(k, d):
    CACHE[k] = (d, time.time())

# ============================================================
# 5. ЛОГИКА РЫНКОВ
# ============================================================
def is_forex_open():
    now = datetime.now(timezone.utc)
    weekday = now.weekday()
    hour = now.hour
    if weekday == 5:
        return False
    if weekday == 6 and hour < 22:
        return False
    if weekday == 4 and hour >= 22:
        return False
    return True

def is_crypto_symbol(symbol):
    return any(c in symbol for c in ["BTC", "ETH", "SOL"])

# ============================================================
# 6. АГРЕГАЦИЯ M1 -> M5/M15/H1
# ============================================================
def resample_m1_to_tf(candles_m1, target_minutes, is_crypto=False):
    if not candles_m1:
        return []
    parsed = []
    for c in candles_m1:
        if c.get("close") is None:
            continue
        try:
            dt_str = c["datetime"].replace("Z", " ").replace("T", "  ")
            dt = datetime.fromisoformat(dt_str)
            parsed.append((dt, c))
        except Exception:
            continue
    
    if not parsed:
        return []
    
    parsed.sort(key=lambda x: x[0])
    first_dt = parsed[0][0]
    
    if target_minutes == 60:
        current_start = first_dt.replace(minute=0, second=0, microsecond=0)
    else:
        minutes = (first_dt.minute // target_minutes) * target_minutes
        current_start = first_dt.replace(minute=minutes, second=0, microsecond=0)
    
    last_dt = parsed[-1][0]
    result = []
    min_required = int(target_minutes * (0.5 if is_crypto else 0.8))
    
    while current_start <= last_dt:
        current_end = current_start + timedelta(minutes=target_minutes)
        chunk = [c for dt, c in parsed if current_start <= dt < current_end]
        
        if len(chunk) < min_required:
            current_start = current_end
            continue
        
        try:
            result.append({
                "datetime": current_start.strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(chunk[0]["open"]),
                "high": max(float(x["high"]) for x in chunk),
                "low": min(float(x["low"]) for x in chunk),
                "close": float(chunk[-1]["close"])
            })
        except Exception as e:
            print(f"[WARN] Пропуск {current_start}: {e}", flush=True)
        
        current_start = current_end
    
    print(f"[AGG] M1->M{target_minutes}: {len(candles_m1)}->{len(result)}", flush=True)
    return result

# ============================================================
# 7. НАСТРОЙКИ И КЛАВИАТУРЫ
# ============================================================
PAIRS = {
    "/eurusd": "EUR/USD", "/gbpusd": "GBP/USD", "/usdjpy": "USD/JPY",
    "/audusd": "AUD/USD", "/usdcad": "USD/CAD", "/nzdusd": "NZD/USD",
    "/usdchf": "USD/CHF", "/eurgbp": "EUR/GBP", "/eurjpy": "EUR/JPY",
    "/btc": "BTC/USD", "/eth": "ETH/USD", "/sol": "SOL/USD",
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

def kb_main():
    m = T.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3, is_persistent=True)
    if is_forex_open():
        m.add(T.KeyboardButton("/eurusd"), T.KeyboardButton("/gbpusd"), T.KeyboardButton("/btc"))
        m.add(T.KeyboardButton("/usdjpy"), T.KeyboardButton("/otc"), T.KeyboardButton("/real"))
    else:
        m.add(T.KeyboardButton("/btc"), T.KeyboardButton("/eth"), T.KeyboardButton("/sol"))
        m.add(T.KeyboardButton("/eurusd"), T.KeyboardButton("/otc"), T.KeyboardButton("/real"))
    return m

def kb_inline_main():
    m = T.InlineKeyboardMarkup(row_width=3)
    m.add(
        T.InlineKeyboardButton("EUR/USD", callback_data="pair_eurusd"),
        T.InlineKeyboardButton("GBP/USD", callback_data="pair_gbpusd"),
        T.InlineKeyboardButton("BTC", callback_data="pair_btc")
    )
    m.add(
        T.InlineKeyboardButton("USD/JPY", callback_data="pair_usdjpy"),
        T.InlineKeyboardButton("OTC", callback_data="mode_otc"),
        T.InlineKeyboardButton("REAL", callback_data="mode_real")
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

# ============================================================
# 8. ПОЛУЧЕНИЕ ДАННЫХ
# ============================================================
def get_candles_m1(symbol, limit=300):
    key = f"{symbol}_m1_{limit}"
    c = cache_get(key)
    if c:
        print(f"[CACHE] {symbol} M1", flush=True)
        return c
    
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": symbol, "interval": "1min", "outputsize": limit, 
                "apikey": TWELVE_KEY, "format": "JSON"},
        timeout=15
    )
    data = r.json()
    if "values" not in data:
        raise Exception(f"TwelveData: {data.get('message', data)}")
    
    candles = list(reversed(data["values"]))
    cache_set(key, candles)
    print(f"[API] {symbol} M1: {len(candles)}", flush=True)
    return candles

def get_all_tfs(symbol):
    m1 = get_candles_m1(symbol, 300)
    crypto = is_crypto_symbol(symbol)
    return {
        "m1": m1,
        "m5": resample_m1_to_tf(m1, 5, is_crypto=crypto),
        "m15": resample_m1_to_tf(m1, 15, is_crypto=crypto),
        "h1": resample_m1_to_tf(m1, 60, is_crypto=crypto),
    }

# ============================================================
# 9. ИНДИКАТОРЫ
# ============================================================
def ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
    k = 2 / (period + 1)
    e = sum(prices[:period]) / period
    for p in prices[period:]:
        e = p * k + e * (1 - k)
    return e

def rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag/al))

def bollinger(prices, period=20, mult=2.0):
    if len(prices) < period:
        p = prices[-1] if prices else 0
        return {"sma": p, "upper": p, "lower": p, "pos": "inside"}
    s = prices[-period:]
    sma = sum(s) / period
    var = sum((x - sma)**2 for x in s) / period
    std = var ** 0.5
    u, l = sma + mult * std, sma - mult * std
    p = prices[-1]
    pos = "above_upper" if p > u else "below_lower" if p < l else "inside"
    return {"sma": sma, "upper": u, "lower": l, "pos": pos}

def stoch(highs, lows, closes, period=14):
    if len(closes) < period:
        return 50.0
    h, l = max(highs[-period:]), min(lows[-period:])
    if h == l:
        return 50.0
    return 100 * (closes[-1] - l) / (h - l)

def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return sum(trs) / period

def trend_direction(prices):
    if len(prices) < 10:
        return "неизвестно"
    recent = prices[-10:]
    up = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
    down = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i-1])
    if up > down + 2:
        return "восходящий"
    elif down > up + 2:
        return "нисходящий"
    return "боковик"

# ============================================================
# 10. GEMINI API
# ============================================================
def ask_gemini(prompt, image_bytes=None):
    models = [
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.6-flash"
    ]
    
    for model in models:
        for attempt in range(2):
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
                
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
                        "temperature": 0.3, 
                        "maxOutputTokens": 1024
                    }
                }
                
                print(f"[GEMINI] {model} (попытка {attempt+1}/2)...", flush=True)
                r = requests.post(url, json=payload, timeout=120)
                
                if r.status_code == 200:
                    data = r.json()
                    if "candidates" in data and data["candidates"]:
                        response_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        if attempt > 0 or model != models[0]:
                            print(f"[GEMINI] ✅ Успех с {model}", flush=True)
                        return response_text
                
                print(f"[GEMINI] {model} вернул {r.status_code}", flush=True)
                
            except requests.exceptions.Timeout:
                print(f"[GEMINI] {model} таймаут (попытка {attempt+1}/2)", flush=True)
            except Exception as e:
                print(f"[GEMINI] {model} ошибка: {e}", flush=True)
            
            if attempt == 0:
                time.sleep(2)
        
        print(f"[GEMINI] Переход к следующей модели после {model}", flush=True)
    
    return "❌ Gemini недоступен. Попробуйте через 30 секунд."

# ============================================================
# 11. ОБРАБОТЧИКИ КОМАНД
# ============================================================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(m):
    status = "Real ОТКРЫТ" if is_forex_open() else "Real ЗАКРЫТ (ночь/выходные)"
    bot.send_message(m.chat.id,
        f"GorodTradeBot v8.1 Hybrid C\n{status}\n\n"
        f"REAL (Пн-Пт):\n/eurusd /gbpusd /usdjpy\n\n"
        f"CRYPTO (24/7):\n/btc /eth /sol\n\n"
        f"Скрины:\n/otc /real",
        reply_markup=kb_main())

@bot.message_handler(commands=['otc'])
def cmd_otc(m):
    user_photo_mode[m.chat.id] = 'OTC'
    bot.send_message(m.chat.id, "📉 OTC режим. Кидай скрин.", reply_markup=kb_main())

@bot.message_handler(commands=['real'])
def cmd_real(m):
    user_photo_mode[m.chat.id] = 'REAL'
    bot.send_message(m.chat.id, "📈 REAL режим. Выбери пару.", reply_markup=kb_main())

@bot.message_handler(commands=[k.lstrip('/') for k in PAIRS.keys()])
def cmd_pair(m):
    cmd = m.text.lower().split()[0]
    if cmd not in PAIRS:
        return
    symbol = PAIRS[cmd]
    if not is_crypto_symbol(symbol) and not is_forex_open():
        bot.send_message(m.chat.id,
            f"⚠️ {symbol} Real сейчас закрыт.\n"
            f"Forex работает Пн-Пт. Сейчас доступны:\n"
            f"/btc /eth /sol - крипта 24/7",
            reply_markup=kb_main())
        return
    user_pending[m.chat.id] = symbol
    bot.send_message(m.chat.id, f"Пара: {symbol}\nВыбери ТФ:", reply_markup=kb_tf())

@bot.callback_query_handler(func=lambda c: c.data.startswith("tf_"))
def cb_tf(call):
    tf_key = call.data.split("_")[1]
    symbol = user_pending.get(call.message.chat.id)
    if not symbol:
        bot.answer_callback_query(call.id, "Сначала /eurusd или /btc")
        return
    if tf_key not in TIMEFRAMES:
        return
    if not is_crypto_symbol(symbol) and not is_forex_open():
        bot.answer_callback_query(call.id, "Real закрыт, используй /btc")
        return
    
    tf = TIMEFRAMES[tf_key]
    bot.answer_callback_query(call.id, f"{symbol} {tf_key}м...")
    bot.edit_message_text(f"⏳ Анализирую {symbol} {tf_key}м...",
                          chat_id=call.message.chat.id, message_id=call.message.message_id)
    run_analysis(call.message.chat.id, call.message.message_id, symbol, tf_key, tf)

# ============================================================
# 12. ГЛАВНЫЙ АНАЛИЗ (ГИБРИД C: ДВА ПОРОГА)
# ============================================================
def run_analysis(chat_id, msg_id, symbol, tf_key, tf):
    try:
        all_tfs = get_all_tfs(symbol)
        trigger_tf = tf["trigger"]
        trend_tf = tf["trend"]
        trigger = all_tfs[trigger_tf]
        trend = all_tfs[trend_tf]
        
        if not trigger or not trend:
            raise Exception(f"Нет данных {trigger_tf}/{trend_tf}")
        
        t_closes = [float(c["close"]) for c in trigger]
        t_highs = [float(c["high"]) for c in trigger]
        t_lows = [float(c["low"]) for c in trigger]
        tr_closes = [float(c["close"]) for c in trend]
        
        price = t_closes[-1]
        current_high = t_highs[-1]
        current_low = t_lows[-1]
        
        # Индикаторы
        rsi_period = 7 if int(tf_key) <= 3 else 14
        rsi_v = rsi(t_closes, rsi_period)
        bb_v = bollinger(t_closes, 20, 2.0)
        stoch_v = stoch(t_highs, t_lows, t_closes, 5 if int(tf_key) <= 3 else 14)
        atr_v = atr(t_highs, t_lows, t_closes, 14)
        ema21 = ema(tr_closes, 21)
        ema50 = ema(tr_closes, 50)
        trend_dir = trend_direction(tr_closes)
        
        # === PYTHON-СТРАЖ: Свип ликвидности ===
        recent_high = max(t_highs[-6:-1]) if len(t_highs) > 5 else current_high
        recent_low = min(t_lows[-6:-1]) if len(t_lows) > 5 else current_low
        
        bearish_sweep = (current_high > recent_high) and (price < recent_high)
        bullish_sweep = (current_low < recent_low) and (price > recent_low)
        
        # === PYTHON-СТРАЖ: Confluence Score ===
        score = 0
        reasons = []
        
        # 1. Чёткий тренд M15 (+1)
        if trend_dir in ["восходящий", "нисходящий"]:
            score += 1
            reasons.append(f"📉 Тренд {trend_tf}: {trend_dir}")
        
        # 2. Свип ликвидности (+2)
        if bearish_sweep:
            score += 2
            reasons.append("🐻 Медвежий свип ликвидности")
        elif bullish_sweep:
            score += 2
            reasons.append("🐂 Бычий свип ликвидности")
        
        # 3. Экстремум Bollinger (+1)
        if bb_v["pos"] in ["above_upper", "below_lower"]:
            score += 1
            reasons.append(f"📊 Цена за границей BB ({bb_v['pos']})")
        
        # 4. Экстремум RSI/Stoch (+1)
        if rsi_v > 70 or rsi_v < 30 or stoch_v > 80 or stoch_v < 20:
            score += 1
            reasons.append(f"📈 RSI {rsi_v:.0f} / Stoch {stoch_v:.0f} в экстремуме")
        
        # === ГИБРИД C: ДВА ПОРОГА ===
        
        # ПОРОГ 1: Score 0-1 → WAIT без ИИ (мусор)
        if score <= 1:
            header = (
                f"{symbol} | {tf_key}м | {datetime.now().strftime('%H:%M:%S')} | "
                f"{'CRYPTO' if is_crypto_symbol(symbol) else 'REAL'}\n"
                f"⛔ WAIT — недостаточно факторов ({score}/5)\n\n"
                f"Что есть:\n"
            )
            if reasons:
                header += "\n".join(f"• {r}" for r in reasons)
            else:
                header += "• Нет явных сигналов"
            
            bot.edit_message_text(header, chat_id=chat_id, message_id=msg_id, 
                                 reply_markup=kb_inline_main())
            return  # ИИ НЕ ВЫЗЫВАЕМ
        
        # ПОРОГ 2: Score 2+ → зовём ИИ с подсказкой
        live = {
            "symbol": symbol, "signal_tf": int(tf_key), "price": price,
            "trigger_tf": trigger_tf, "trend_tf": trend_tf,
            "trend_direction": trend_dir, 
            "ema21_trend": round(ema21, 5), "ema50_trend": round(ema50, 5), 
            "rsi_current": round(rsi_v, 2), "stochastic_current": round(stoch_v, 2), 
            "bollinger_position": bb_v["pos"], "atr_14": round(atr_v, 5), 
            "time": datetime.now().strftime("%H:%M:%S"), "expiry": tf["exp"], 
            "is_crypto": is_crypto_symbol(symbol),
            "bearish_liquidity_sweep": bearish_sweep, 
            "bullish_liquidity_sweep": bullish_sweep
        }
        
        base = REAL_BASE if not is_crypto_symbol(symbol) else OTC_BASE
        base_text = json.dumps(base[:10], ensure_ascii=False)[:2500]
        
        reasons_text = ', '.join(reasons) if reasons else 'Нет явных сигналов'
        
        prompt = f"""{MASTER_PROMPT}

PYTHON УЖЕ ПОСЧИТАЛ ДЛЯ ТЕБЯ:
- Confluence Score: {score}/5
- Факты: {reasons_text}
- Свип ликвидности: bearish={bearish_sweep}, bullish={bullish_sweep}
- Тренд {trend_tf}: {trend_dir}
- RSI: {rsi_v:.1f}, Stoch: {stoch_v:.1f}
- BB: {bb_v['pos']}

ТВОЯ ЗАДАЧА:
1. Если Score = 2 → будь осторожен, ищи редкий паттерн (Микро-М/W, Харами)
2. Если Score >= 3 → определи направление (CALL/PUT) и назови конкретный паттерн из БАЗЫ ЗНАНИЙ
3. Если ты НЕ согласен с Python (видишь что-то особенное) → объясни почему
4. Дай краткое объяснение логики

LIVE ДАННЫЕ ({symbol}, сигнал на {tf_key} мин):
{json.dumps(live, ensure_ascii=False, indent=2)}

БАЗА ЗНАНИЙ (используй эти правила):
{base_text}

Верни JSON: {{"direction": "CALL/PUT/WAIT", "confidence": 55-75, "pattern": "название паттерна из базы или 'нет паттерна'", "reason": "краткое объяснение"}}
"""
        text = ask_gemini(prompt)
        
        # === ФОРМАТИРОВАНИЕ ОТВЕТА ===
        sweep_text = "🐻 Медвежий" if bearish_sweep else ("🐂 Бычий" if bullish_sweep else "Нет")
        
        header = (
            f"{symbol} | {tf_key}м | {live['time']} | {'CRYPTO' if live['is_crypto'] else 'REAL'}\n"
            f"Цена: {price} | Тренд {trend_tf}: {trend_dir}\n"
            f"EMA21/50: {ema21:.5f}/{ema50:.5f} | RSI: {rsi_v:.1f} Stoch: {stoch_v:.1f}\n"
            f"BB: {bb_v['pos']} ATR: {atr_v:.5f} | Экспирация: {tf['exp']}м\n"
            f"📊 Python Score: {score}/5 | Свип: {sweep_text}\n\n"
        )
        
        bot.edit_message_text(header + text, chat_id=chat_id, message_id=msg_id, 
                             reply_markup=kb_inline_main())
        
    except Exception as e:
        print(f"[ERROR] {symbol}: {e}", flush=True)
        traceback.print_exc()
        bot.edit_message_text(f"Ошибка: {e}", chat_id=chat_id, message_id=msg_id, 
                             reply_markup=kb_inline_main())

# ============================================================
# 13. PHOTO РЕЖИМ (ВСЕГДА ИИ)
# ============================================================
@bot.message_handler(content_types=['photo'])
def handle_photo(m):
    mode = user_photo_mode.get(m.chat.id, 'REAL')
    loading = bot.send_message(m.chat.id, f"⏳ Анализирую скрин ({mode})...")
    try:
        fi = bot.get_file(m.photo[-1].file_id)
        img = bot.download_file(fi.file_path)
        base = OTC_BASE if mode == 'OTC' else REAL_BASE
        base_text = json.dumps(base[:10], ensure_ascii=False)[:2500]
        
        prompt = f"""{MASTER_PROMPT}

Режим: {mode}

БАЗА ЗНАНИЙ:
{base_text}

ЗАДАЧА: Проанализируй скриншот графика и дай сигнал для бинарных опционов.
Назови конкретный паттерн из БАЗЫ ЗНАНИЙ (BOF, TST, Молот, Поглощение и т.д.).
Уверенность 55-75%.

Верни JSON: {{"direction": "CALL/PUT/WAIT", "confidence": 55-75, "pattern": "название паттерна", "reason": "краткое объяснение"}}
"""
        text = ask_gemini(prompt, image_bytes=img)
        bot.edit_message_text(text, chat_id=m.chat.id, message_id=loading.message_id, 
                             reply_markup=kb_inline_main())
    except Exception as e:
        print(f"[ERROR] Photo: {e}", flush=True)
        bot.edit_message_text(f"Ошибка фото: {e}", chat_id=m.chat.id, message_id=loading.message_id,
                             reply_markup=kb_inline_main())

# ============================================================
# 14. ЗАПУСК
# ============================================================
if __name__ == "__main__":
    print("[START] GorodTradeBot v8.1 Hybrid C", flush=True)
    print(f"[INFO] Forex open: {is_forex_open()}", flush=True)
    print(f"[INFO] Пар: {len(PAIRS)}, ТФ: {len(TIMEFRAMES)}", flush=True)
    print(f"[INFO] OTC база: {len(OTC_BASE)} индикаторов", flush=True)
    print(f"[INFO] REAL база: {len(REAL_BASE)} индикаторов", flush=True)
    print(f"[INFO] Гибрид C: Score 0-1 → WAIT без ИИ, Score 2+ → ИИ с подсказкой", flush=True)
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
