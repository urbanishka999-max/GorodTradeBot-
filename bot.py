"""
GorodTradeBot v7.7 FINAL (Актуально для 2026: gemini-3.8-flash)
LIVE (Real+Crypto) + OTC/REAL по скринам + Сканеры + Night Mode
"""
import telebot
import json
import os
import threading
import traceback
import requests
import base64
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telebot import types as T

# ============================================================
# 1. KEEP-ALIVE
# ============================================================
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"GorodTradeBot v7.7 FINAL")
    
    def log_message(self, format, *args):
        pass

def run_keepalive():
    port = int(os.getenv("PORT", 10000))
    try:
        HTTPServer(('0.0.0.0', port), KeepAliveHandler).serve_forever()
        print(f"[OK] Keep-alive на порту {port}", flush=True)
    except Exception as e:
        print(f"[ERROR] Keep-alive: {e}", flush=True)

threading.Thread(target=run_keepalive, daemon=True).start()

# ============================================================
# 2. ENV
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")

if not BOT_TOKEN or not GEMINI_KEY or not TWELVE_KEY:
    raise Exception("BOT_TOKEN, GEMINI_KEY, TWELVE_KEY обязательны в ENV")

bot = telebot.TeleBot(BOT_TOKEN)

# ============================================================
# 3. БАЗЫ ЗНАНИЙ (с автоочисткой пробелов)
# ============================================================
def load_json(fn):
    try:
        with open(fn, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    keys = list(item.keys())
                    for k in keys:
                        clean_k = k.strip()
                        if k != clean_k:
                            item[clean_k] = item.pop(k)
                        if isinstance(item.get(clean_k), str):
                            item[clean_k] = item[clean_k].strip()
        print(f"[OK] {fn}: {len(data)} карточек", flush=True)
        return data
    except Exception as e:
        print(f"[WARN] {fn}: {e}", flush=True)
        return []

OTC_BASE = load_json('pocket_OTC.json')
REAL_BASE = load_json('pocket_REAL_v2.json')

MASTER_PROMPT = ""
try:
    with open('master_prompt_v2.md', 'r', encoding='utf-8') as f:
        MASTER_PROMPT = f.read()
    print(f"[OK] master_prompt_v2.md: {len(MASTER_PROMPT)} символов", flush=True)
except FileNotFoundError:
    MASTER_PROMPT = "Ты профессиональный скальпер бинарных опционов Pocket Option."

# ============================================================
# 4. КЭШ И СТАТИСТИКА
# ============================================================
CACHE = {}
CACHE_TTL = 30
STATS = {
    "requests": 0,
    "cache_hits": 0,
    "errors": 0,
    "last_request": None,
    "start_time": time.time()
}

def cache_get(k):
    if k in CACHE:
        d, t = CACHE[k]
        if time.time() - t < CACHE_TTL:
            STATS["cache_hits"] += 1
            return d
    return None

def cache_set(k, d):
    CACHE[k] = (d, time.time())

# ============================================================
# 5. РЫНОК: REAL vs CRYPTO
# ============================================================
def is_forex_open():
    now = datetime.utcnow()
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
# 6. TIME-BASED АГРЕГАЦИЯ
# ============================================================
def resample_m1_to_tf(candles_m1, target_minutes, is_crypto=False):
    if not candles_m1:
        return []
    
    parsed = []
    for c in candles_m1:
        if c.get("close") is None:
            continue
        try:
            dt_str = c["datetime"].replace("Z", "").replace("T", " ")
            dt = datetime.fromisoformat(dt_str)
            parsed.append((dt, c))
        except:
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
# 7. ПАРЫ И ТФ (БЕЗ ПРОБЕЛОВ!)
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

# ============================================================
# 8. КЛАВИАТУРЫ
# ============================================================
def kb_main():
    m = T.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3, is_persistent=True)
    if is_forex_open():
        m.add(T.KeyboardButton("/eurusd"), T.KeyboardButton("/gbpusd"), T.KeyboardButton("/btc"))
        m.add(T.KeyboardButton("/usdjpy"), T.KeyboardButton("/otc"), T.KeyboardButton("/real"))
    else:
        m.add(T.KeyboardButton("/btc"), T.KeyboardButton("/eth"), T.KeyboardButton("/sol"))
        m.add(T.KeyboardButton("/eurusd"), T.KeyboardButton("/otc"), T.KeyboardButton("/real"))
    m.add(T.KeyboardButton("/status"))
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
# 9. СВЕЧИ
# ============================================================
def get_candles_m1(symbol, limit=300):
    key = f"{symbol}_m1_{limit}"
    c = cache_get(key)
    if c:
        print(f"[CACHE] {symbol} M1", flush=True)
        return c
    
    STATS["requests"] += 1
    STATS["last_request"] = datetime.utcnow().strftime("%H:%M:%S")
    
    r = requests.get("https://api.twelvedata.com/time_series",
        params={"symbol": symbol, "interval": "1min", "outputsize": limit, 
                "apikey": TWELVE_KEY, "format": "JSON"},
        timeout=15)
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
# 10. ИНДИКАТОРЫ
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
# 11. GEMINI HTTP (ОБНОВЛЕНО: gemini-3.8-flash)
# ============================================================
def ask_gemini(prompt, image_bytes=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={GEMINI_KEY}"
    parts = [{"text": prompt}]
    
    if image_bytes:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode()}})
    
    payload = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024}}
    
    try:
        r = requests.post(url, json=payload, timeout=60)
        if r.status_code != 200:
            return f"Gemini ошибка {r.status_code}: {r.text[:300]}"
        data = r.json()
        if "candidates" in data and data["candidates"]:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        return f"Gemini: нет ответа {str(data)[:300]}"
    except Exception as e:
        return f"Gemini: {e}"

# ============================================================
# 12. ХЕНДЛЕРЫ КОМАНД
# ============================================================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(m):
    status = "Real ОТКРЫТ" if is_forex_open() else "Real ЗАКРЫТ (ночь/выходные)"
    bot.send_message(m.chat.id,
        f"GorodTradeBot v7.7 FINAL\n{status}\n\n"
        "🔍 ОДИНОЧНЫЙ АНАЛИЗ:\n"
        "/eurusd /gbpusd /btc (выбор ТФ)\n\n"
        "🌐 МАССОВОЕ СКАНИРОВАНИЕ (15м):\n"
        "/scan_forex — все валютные пары\n"
        "/scan_crypto — все крипто-пары\n"
        "/scan_all — весь рынок сразу\n\n"
        "📸 СКРИНШОТЫ:\n"
        "/otc — OTC режим (кидай скрин)\n"
        "/real — REAL режим (кидай скрин)\n\n"
        "📊 СТАТУС:\n"
        "/status",
        reply_markup=kb_main())

@bot.message_handler(commands=['status'])
def cmd_status(m):
    uptime = int(time.time() - STATS["start_time"])
    hours = uptime // 3600
    mins = (uptime % 3600) // 60
    
    text = (
        f"📊 Статус бота\n\n"
        f"⏱️ Аптайм: {hours}ч {mins}м\n"
        f"🌐 Forex: {'ОТКРЫТ' if is_forex_open() else 'ЗАКРЫТ'}\n"
        f"📡 Запросов к API: {STATS['requests']}\n"
        f"💾 Кэш попаданий: {STATS['cache_hits']}\n"
        f"❌ Ошибок: {STATS['errors']}\n"
        f"🕐 Последний запрос: {STATS['last_request'] or 'нет'}\n"
        f"📦 Кэш записей: {len(CACHE)}\n"
        f"🔑 ENV: {'✅' if BOT_TOKEN and GEMINI_KEY and TWELVE_KEY else '❌'}"
    )
    bot.send_message(m.chat.id, text, reply_markup=kb_main())

@bot.message_handler(commands=['otc'])
def cmd_otc(m):
    user_photo_mode[m.chat.id] = 'OTC'
    bot.send_message(m.chat.id, "📉 OTC режим активирован. Кидай скриншот графика Pocket Option.", reply_markup=kb_main())

@bot.message_handler(commands=['real'])
def cmd_real(m):
    user_photo_mode[m.chat.id] = 'REAL'
    bot.send_message(m.chat.id, "📈 REAL режим активирован. Кидай скриншот графика.", reply_markup=kb_main())

@bot.message_handler(commands=[k.lstrip('/') for k in PAIRS.keys()])
def cmd_pair(m):
    cmd = "/" + m.text.lower().split()[0].lstrip("/")
    if cmd not in PAIRS:
        return
    
    symbol = PAIRS[cmd]
    
    if not is_crypto_symbol(symbol) and not is_forex_open():
        bot.send_message(
            m.chat.id,
            f"⚠️ {symbol} Real сейчас закрыт.\n"
            f"Forex работает Пн-Пт. Сейчас доступны:\n"
            f"/btc /eth /sol - крипта 24/7",
            reply_markup=kb_main()
        )
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
    bot.edit_message_text(f"Анализирую {symbol} {tf_key}м...",
                          chat_id=call.message.chat.id, message_id=call.message.message_id)
    run_analysis(call.message.chat.id, call.message.message_id, symbol, tf_key, tf)

# ============================================================
# 13. ОДИНОЧНЫЙ АНАЛИЗ
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
        rsi_v = rsi(t_closes, 7 if int(tf_key) <= 3 else 14)
        bb_v = bollinger(t_closes, 20, 2.0)
        stoch_v = stoch(t_highs, t_lows, t_closes, 5 if int(tf_key) <= 3 else 14)
        atr_v = atr(t_highs, t_lows, t_closes, 14)
        ema21 = ema(tr_closes, 21)
        ema50 = ema(tr_closes, 50)
        trend_dir = trend_direction(tr_closes)
        
        live = {
            "symbol": symbol, "signal_tf": int(tf_key), "price": price,
            "trigger_tf": trigger_tf, "trend_tf": trend_tf,
            "trend_direction": trend_dir, "ema21_trend": round(ema21, 5),
            "ema50_trend": round(ema50, 5), "rsi_trigger": round(rsi_v, 2),
            "stochastic_trigger": round(stoch_v, 2), "bollinger_position": bb_v["pos"],
            "atr_14": round(atr_v, 5), "time": datetime.now().strftime("%H:%M:%S"),
            "expiry": tf["exp"], "is_crypto": is_crypto_symbol(symbol)
        }
        
        base_text = json.dumps(REAL_BASE[:10], ensure_ascii=False)[:2500]
        
        prompt = f"""{MASTER_PROMPT}

LIVE ({symbol}, сигнал на {tf_key} мин):
{json.dumps(live, ensure_ascii=False, indent=2)}

БАЗА:
{base_text}

ЗАДАЧА: CALL/PUT/WAIT на {tf['exp']} мин.
Учитывай тренд {trend_tf}={trend_dir}.
Верни по формату из master_prompt."""

        text = ask_gemini(prompt)
        
        header = (
            f"{symbol} | {tf_key}м | {live['time']} | {'CRYPTO' if live['is_crypto'] else 'REAL'}\n"
            f"Цена: {price} | Тренд {trend_tf}: {trend_dir}\n"
            f"EMA21/50: {ema21:.5f}/{ema50:.5f} | RSI: {rsi_v:.1f} Stoch: {stoch_v:.1f}\n"
            f"BB: {bb_v['pos']} ATR: {atr_v:.5f} | Экспирация: {tf['exp']}м\n\n"
        )
        
        bot.edit_message_text(header + text, chat_id=chat_id, message_id=msg_id, reply_markup=kb_main())
        
    except Exception as e:
        STATS["errors"] += 1
        print(f"[ERROR] {symbol}: {e}", flush=True)
        traceback.print_exc()
        bot.edit_message_text(f"Ошибка: {e}", chat_id=chat_id, message_id=msg_id, reply_markup=kb_main())

# ============================================================
# 14. ФОТО РЕЖИМ (OTC/REAL по скринам)
# ============================================================
@bot.message_handler(content_types=['photo'])
def handle_photo(m):
    mode = user_photo_mode.get(m.chat.id, 'REAL')
    loading = bot.send_message(m.chat.id, f"📸 Анализирую скриншот ({mode})...")
    
    try:
        fi = bot.get_file(m.photo[-1].file_id)
        img = bot.download_file(fi.file_path)
        
        base = OTC_BASE if mode == 'OTC' else REAL_BASE
        base_text = json.dumps(base[:10], ensure_ascii=False)[:2500]
        
        prompt = f"""{MASTER_PROMPT}

Режим: {mode}
БАЗА ЗНАНИЙ ({len(base)} индикаторов):
{base_text}

ЗАДАЧА: Проанализируй скриншот графика Pocket Option.
- Если видишь аномально длинную свечу (новостной импульс) → WAIT
- Если полосы Боллинджера сузились → WAIT
- Уверенность 55-75%

Верни ответ по формату из master_prompt."""

        text = ask_gemini(prompt, image_bytes=img)
        bot.edit_message_text(text, chat_id=m.chat.id, message_id=loading.message_id, reply_markup=kb_main())
    except Exception as e:
        STATS["errors"] += 1
        bot.edit_message_text(f"❌ Ошибка фото: {e}", chat_id=m.chat.id, message_id=loading.message_id)

# ============================================================
# 15. МАССОВОЕ СКАНИРОВАНИЕ
# ============================================================
def run_batch_scan(message, pair_dict, scan_name, tf_key="15"):
    tf = TIMEFRAMES[tf_key]
    loading_msg = bot.send_message(
        message.chat.id, 
        f"🔄 Запускаю сканирование {scan_name} (ТФ {tf_key}м).\n"
        f"Это займет ~{len(pair_dict)*8} сек. Жди...", 
        reply_markup=kb_main()
    )
    
    results_data = []
    total = len(pair_dict)
    
    for i, (cmd, symbol) in enumerate(pair_dict.items(), 1):
        try:
            bot.edit_message_text(
                f"⏳ Анализирую {symbol}... ({i}/{total})", 
                chat_id=message.chat.id, 
                message_id=loading_msg.message_id
            )
        except:
            pass
        
        try:
            all_tfs = get_all_tfs(symbol)
            trigger = all_tfs[tf["trigger"]]
            trend = all_tfs[tf["trend"]]
            
            if not trigger or not trend:
                results_data.append(f"- {symbol}: Нет данных")
                continue
                
            t_closes = [float(c["close"]) for c in trigger]
            t_highs = [float(c["high"]) for c in trigger]
            t_lows = [float(c["low"]) for c in trigger]
            tr_closes = [float(c["close"]) for c in trend]
            
            price = t_closes[-1]
            rsi_v = rsi(t_closes, 14)
            bb_v = bollinger(t_closes, 20, 2.0)
            stoch_v = stoch(t_highs, t_lows, t_closes, 14)
            ema21 = ema(tr_closes, 21)
            ema50 = ema(tr_closes, 50)
            
            trend_dir = "восходящий" if ema21 > ema50 else "нисходящий" if ema21 < ema50 else "боковик"
            
            results_data.append(
                f"- {symbol}: Цена={price}, Тренд={trend_dir}, "
                f"RSI={rsi_v:.1f}, Stoch={stoch_v:.1f}, BB={bb_v['pos']}"
            )
            
            if i < total:
                time.sleep(8)
                
        except Exception as e:
            results_data.append(f"- {symbol}: Ошибка ({e})")
            if i < total:
                time.sleep(8)

    try:
        bot.edit_message_text(
            "🧠 Отправляю данные в Gemini...", 
            chat_id=message.chat.id, 
            message_id=loading_msg.message_id
        )
    except:
        pass
    
    batch_prompt = f"""Ты профессиональный скальпер. Проанализируй эти данные для ТФ {tf_key} минут.

ДАННЫЕ:
{chr(10).join(results_data)}

ЗАДАЧА:
Верни ТОЛЬКО пары с уверенностью 60%+. Формат:
🟢 {symbol}: CALL (X%) - Причина
🔴 {symbol}: PUT (X%) - Причина
🟡 {symbol}: WAIT (X%) - Причина

Если сильных сигналов нет: "Сильных сигналов нет, лучше переждать"."""

    final_text = ask_gemini(batch_prompt)
    
    try:
        bot.edit_message_text(
            f"✅ СКАНИРОВАНИЕ {scan_name} ЗАВЕРШЕНО!\n\n"
            f"ТФ: {tf_key} мин | Пар: {total}\n\n"
            f"📊 РЕЗУЛЬТАТЫ:\n{final_text}",
            chat_id=message.chat.id,
            message_id=loading_msg.message_id,
            reply_markup=kb_main()
        )
    except:
        bot.send_message(message.chat.id, f"✅ Скан завершен!\n\n{final_text}", reply_markup=kb_main())

def run_batch_scan_threaded(message, pair_dict, scan_name, tf_key="15"):
    thread = threading.Thread(
        target=run_batch_scan,
        args=(message, pair_dict, scan_name, tf_key),
        daemon=True
    )
    thread.start()

@bot.message_handler(commands=['scan_forex'])
def cmd_scan_forex(m):
    if not is_forex_open():
        bot.send_message(
            m.chat.id,
            "⚠️ Forex закрыт (ночь/выходные).\n"
            "Используй /scan_crypto для криптовалют 24/7.",
            reply_markup=kb_main()
        )
        return
    
    forex_pairs = {k: v for k, v in PAIRS.items() if not is_crypto_symbol(v)}
    bot.send_message(m.chat.id, "🚀 Запускаю сканер FOREX...", reply_markup=kb_main())
    run_batch_scan_threaded(m, forex_pairs, "FOREX", tf_key="15")

@bot.message_handler(commands=['scan_crypto'])
def cmd_scan_crypto(m):
    crypto_pairs = {k: v for k, v in PAIRS.items() if is_crypto_symbol(v)}
    bot.send_message(m.chat.id, "🚀 Запускаю сканер CRYPTO...", reply_markup=kb_main())
    run_batch_scan_threaded(m, crypto_pairs, "CRYPTO", tf_key="15")

@bot.message_handler(commands=['scan_all'])
def cmd_scan_all(m):
    if not is_forex_open():
        bot.send_message(
            m.chat.id,
            "⚠️ Forex закрыт. Сканирую только криптовалюту...",
            reply_markup=kb_main()
        )
        crypto_pairs = {k: v for k, v in PAIRS.items() if is_crypto_symbol(v)}
        run_batch_scan_threaded(m, crypto_pairs, "CRYPTO (Forex закрыт)", tf_key="15")
        return
    
    bot.send_message(m.chat.id, "🚀 Запускаю сканер ВСЕХ РЫНКОВ...", reply_markup=kb_main())
    run_batch_scan_threaded(m, PAIRS, "ВСЕХ РЫНКОВ", tf_key="15")

# ============================================================
# 16. ЗАПУСК
# ============================================================
if __name__ == "__main__":
    print("[START] GorodTradeBot v7.7 FINAL", flush=True)
    print(f"[INFO] Forex open: {is_forex_open()}", flush=True)
    print(f"[INFO] Пар: {len(PAIRS)}, ТФ: {len(TIMEFRAMES)}", flush=True)
    print(f"[INFO] OTC база: {len(OTC_BASE)} индикаторов", flush=True)
    print(f"[INFO] REAL база: {len(REAL_BASE)} индикаторов", flush=True)
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
