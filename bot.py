
import telebot
import json
import os
from telebot import types as tb_types
from pocket_connector import get_market_data

# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)

# --- ЗАГРУЗКА БАЗ ПАТТЕРНОВ ---
print("Loading bases...")
with open('pocket_OTC.json', 'r', encoding='utf-8') as f:
    OTC_BASE = json.load(f)
with open('pocket_REAL_v2.json', 'r', encoding='utf-8') as f:
    REAL_BASE = json.load(f)

# --- ПРОМПТ ---
MASTER = """
=== КРИТИЧНО: ЭТО БИНАРКИ, А НЕ КЛАССИКА ===
1. ВРЕМЯ ЭКСПИРАЦИИ - СМОТРИ ВНИЗ СКРИНА:
   - На скрине внизу есть поле "Time: 04:00:00" или "01:00" или "02:00" - ЭТО ВРЕМЯ СДЕЛКИ КОТОРОЕ ВЫБРАЛ ТРЕЙДЕР
   - Если видишь Time: 01:00 -> сигнал на 1 минуту (следующая свеча)
   - Time: 02:00-03:00 -> сигнал на 2-3 свечи (краткосрок)
   - Time: 04:00-05:00 -> сигнал на 4-5 свечей (стандарт для M1)
   - ОБЯЗАТЕЛЬНО УЧИТЫВАЙ ЭТО ВРЕМЯ В ОТВЕТЕ!

2. ТАЙМЕР СВЕЧИ:
   - Вверху скрина есть таймер "00:12" или "23:56" - это время до закрытия текущей M1 свечи
   - Если до закрытия <10 сек - ЖДИ следующую свечу, не входи в самом конце!
   - Если 15-40 сек - идеальное время для входа

3. СУТЬ БИНАРОК:
   - Тебе не нужно предсказать куда пойдет цена через час
   - Тебе нужно: куда закроется цена ЧЕРЕЗ ВЫБРАННОЕ ВРЕМЯ (Time)?
   - Пример: Цена сейчас 1.06751, Time=04:00, ты говоришь PUT -> значит через 4 минуты цена будет НИЖЕ 1.06751

=== АЛГОРИТМ ОПРЕДЕЛЕНИЯ РЫНКА ===
ШАГ 0: Посмотри на скриншот. Найди название актива в левом верхнем углу.
- IF в названии есть "OTC":
  -> market_type = "OTC"
  -> ЗАГРУЗИ ТОЛЬКО БАЗУ pocket_OTC.json (19 карточек)
  -> ЗАПРЕЩЕНО: EMA 21/50/200, MACD, Parabolic (тренд не работает на OTC)
  -> РАЗРЕШЕНО: Bollinger 20/2.0, RSI 7, Stochastic 5-3-3, BOF, TST, поглощения
  -> ЛОГИКА: "OTC - это синтетика, 70% пробоев ложные, ищем отскок от уровня"

- ELSE (нет OTC):
  -> market_type = "REAL"
  -> БАЗА pocket_REAL_v2.json (34 карточки)
  -> РАЗРЕШЕНО ВСЕ + новостной фильтр
  -> IF видишь 1 свечу в 3 раза длиннее всех -> "⚠️ Новостной импульс, торговля ЗАПРЕЩЕНА"

=== ПОРЯДОК АНАЛИЗА ДЛЯ БИНАРОК M1 ===
1. ВРЕМЯ: Какое Time выбрано? 01:00/04:00? Сколько до закрытия свечи?
2. УРОВЕНЬ: Где цена сейчас относительно уровня? Подошла к сопротивлению/поддержке?
3. ТРИГГЕР: Есть ли закрытый свечной паттерн (пин-бар, поглощение) ПРЯМО СЕЙЧАС?
4. ЭКСПИРАЦИЯ: На сколько свечей ставить? 
   - Скальп отскок от Bollinger = 2-3 минуты
   - TST тест уровня = 3-4 минуты
   - BOF ложный пробой = 2 минуты
   - Харами/Доджи = 1-2 минуты

=== ЗОЛОТЫЕ ПРАВИЛА ===
- ЗАПРЕЩЕНО: "100%", "гарантия", "на все", мартингейл
- ВЕРОЯТНОСТЬ: только 55-75%, никогда 90%
- ЕСЛИ паттерн не сформирован: "Уверенность <50%, WAIT" - это нормально
- ВСЕГДА УКАЗЫВАЙ рекомендуемое время экспирации в минутах!

=== ФОРМАТ ОТВЕТА ДЛЯ БИНАРОК (строго) ===
📊 Актив: [Название + OTC/REAL]
⏱️ Время: [Какое Time на скрине + сколько до закрытия свечи + рекомендованная экспирация]
🔍 Контекст: [Тренд M1 и уровень]
📈 Факты: [Что видишь: цена, уровень, индикаторы если есть]
🧠 Интерпретация: [Паттерн из базы + страница книги + почему для бинарок на X минут]
🎯 Сигнал: [CALL/PUT/WAIT на N минут] + Уверенность: [55-75%] + Вероятность [X%]
⛔ Отмена: [Когда сценарий ломается]
📝 Итог: [1 предложение + JSON]

JSON: {"direction": "CALL/PUT/WAIT", "confidence": 55-75, "market_type": "OTC/REAL", "expiry_minutes": 2, "reason": "BOF + RSI", "cancel_condition": "пробой уровня"}
"""

# --- РЕЖИМЫ ПОЛЬЗОВАТЕЛЕЙ ---
user_mode = {}

# --- КЛАВИАТУРА ---
def get_main_keyboard():
    markup = tb_types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3, is_persistent=True)
    markup.add(
        tb_types.KeyboardButton("/start"),
        tb_types.KeyboardButton("/otc"),
        tb_types.KeyboardButton("/real"),
        tb_types.KeyboardButton("/auto"),
        tb_types.KeyboardButton("/help"),
        tb_types.KeyboardButton("📊 Pocket Option")
    )
    return markup

# --- ХЕНДЛЕРЫ КОМАНД ---
@bot.message_handler(commands=['start', 'otc', 'real', 'auto', 'help'])
def handle_mode(message):
    txt = message.text
    txt_lower = txt.lower() if txt else ""
    if txt == '/otc' or txt_lower == 'otc' or 'otc' in txt_lower:
        user_mode[message.chat.id] = 'OTC'
        bot.send_message(message.chat.id, "📉 OTC режим (19 паттернов) - кидай скрин!", reply_markup=get_main_keyboard())
    elif txt == '/real':
        user_mode[message.chat.id] = 'REAL'
        bot.send_message(message.chat.id, "📈 REAL режим (34 паттерна) - кидай скрин!", reply_markup=get_main_keyboard())
    elif txt == '/auto':
        user_mode[message.chat.id] = 'AUTO'
        bot.send_message(message.chat.id, "🤖 GorodTradeBot v4 готов!\n/otc /real /auto\nКидай скрин или используй LIVE!", reply_markup=get_main_keyboard())
    elif txt == '/help':
        help_text = """
📖 Помощь:
/otc - режим OTC (19 паттернов)
/real - режим REAL (34 паттерна)
/auto - автоматический выбор
/help - эта справка

LIVE команды (быстрый анализ без скрина):
/eurusd_otc - EURUSD OTC
/eurusd - EURUSD REAL
/btc - Bitcoin
/btc_otc - Bitcoin OTC

Кидай скрин графика или используй LIVE команды!
"""
        bot.send_message(message.chat.id, help_text, reply_markup=get_main_keyboard())
    else:
        user_mode[message.chat.id] = 'AUTO'
        bot.send_message(message.chat.id, "🤖 GorodTradeBot v4 готов!\n/otc /real /auto\nКидай скрин или используй LIVE!", reply_markup=get_main_keyboard())

# --- НОВЫЙ ХЕНДЛЕР: LIVE-РЕЖИМ ---
@bot.message_handler(commands=['test', 'eurusd_otc', 'eurusd', 'btc', 'btc_otc'])
def handle_live_test(message):
    """Тест LIVE-режима: тянет данные из PO и присылает в Telegram."""
    bot.send_message(message.chat.id, "⏳ Подключаюсь к Pocket Option...", reply_markup=get_main_keyboard())
    
    # Определяем актив по команде
    cmd = message.text.split()[0].lower()
    if 'eurusd' in cmd and 'otc' in cmd:
        asset = "EURUSD_otc"
    elif 'eurusd' in cmd:
        asset = "EURUSD"  # REAL рынок
    elif 'btc' in cmd and 'otc' in cmd:
        asset = "BTCUSD_otc"
    elif 'btc' in cmd:
        asset = "BTCUSD"
    else:
        asset = "EURUSD_otc"  # по умолчанию
    
    try:
        result = get_market_data(asset)
        bot.send_message(message.chat.id, result, reply_markup=get_main_keyboard())
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", reply_markup=get_main_keyboard())

# --- ХЕНДЛЕР ФОТО (PHOTO-РЕЖИМ) ---
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    mode = user_mode.get(message.chat.id, 'AUTO')
    bot.send_message(message.chat.id, f"⏳ Анализирую {mode}...", reply_markup=get_main_keyboard())
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        base = OTC_BASE if mode == 'OTC' else REAL_BASE
        
        prompt = f"""{MASTER}

        ВНИМАНИЕ: Это бинарные опционы Pocket Option, M1 таймфрейм!
        На скрине внизу ищи:
        - Поле Time (например 04:00:00) - это время экспирации которое выбрал трейдер, ОБЯЗАТЕЛЬНО учитывай!
        - Таймер до закрытия свечи (например 00:12 или 23:56)
        - Давай сигнал именно на это время экспирации, а не на час вперед!

        БАЗА: {mode} {len(base)} карт
        {json.dumps(base, ensure_ascii=False)}

        Проанализируй скрин как скальпер бинарок M1 на следующие {mode} минут, укажи рекомендуемую экспирацию!
        """
        
        # Здесь должен быть код отправки в Gemini (как было в v2.8)
        text = "📸 PHOTO-режим: скрин получен. Анализ через Gemini..."
        
        if len(text) > 3500:
            text = text[:3500] + "\n...обрезано"
        bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())
    except Exception as e:
        print(f"Error in handle_photo: {e}")
        import traceback
        traceback.print_exc()
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", reply_markup=get_main_keyboard())

# --- ХЕНДЛЕР ТЕКСТОВЫХ СООБЩЕНИЙ (ИСПРАВЛЕН) ---
@bot.message_handler(func=lambda m: True)
def handle_text(message):
    if message.text and message.text.startswith("/"):
        cmd = message.text.split()[0].lower()
        # Если это LIVE-команда - отправляем в handle_live_test
        if cmd in ['/eurusd_otc', '/eurusd', '/btc', '/btc_otc', '/test']:
            handle_live_test(message)
            return
        # Иначе - в handle_mode (для кнопок клавиатуры)
        handle_mode(message)
        return
    bot.send_message(message.chat.id, "Кидай скрин графика 📸\nИли используй LIVE команды:\n/eurusd_otc - быстрый анализ\n/eurusd - REAL рынок", reply_markup=get_main_keyboard())

# --- ЗАПУСК БОТА ---
if __name__ == "__main__":
    print("🤖 GorodTradeBot v4 HYBRID запущен!")
    bot.polling(none_stop=True, interval=1, timeout=60)

