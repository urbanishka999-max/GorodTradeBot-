import asyncio
import json
import os
import websockets

# --- КОНФИГУРАЦИЯ ---
SESSION_ID = os.getenv("PO_SESSION_ID", "27e00c0d741a50fd60ec48ab785fd9ed")
WS_URL = "wss://ws2.pocketoption.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Origin": "https://pocketoption.com",
    "Sec-WebSocket-Protocol": "binary"
}

async def _connect_and_auth():
    ws = await websockets.connect(WS_URL, extra_headers=HEADERS, ping_interval=None, open_timeout=10)
    auth_payload = {
        "name": "auth",
        "version": "1.0",
        "body": {"session": SESSION_ID}
    }
    await ws.send(json.dumps(auth_payload))
    response = await asyncio.wait_for(ws.recv(), timeout=5)
    if "error" in response.lower() or "false" in response.lower():
        await ws.close()
        raise Exception(f"Ошибка авторизации: {response[:200]}")
    return ws

async def _get_candles(ws, asset, period, count):
    payload = {
        "name": "candles",
        "version": "1.0",
        "body": {"asset": asset, "period": period, "count": count}
    }
    await ws.send(json.dumps(payload))
    response = await asyncio.wait_for(ws.recv(), timeout=10)
    data = json.loads(response)
    if "body" in data and "candles" in data["body"]:
        return data["body"]["candles"]
    else:
        raise Exception(f"Неожиданный ответ свечей: {response[:200]}")

def get_market_data(asset="EURUSD_otc"):
    loop = asyncio.new_event_loop()
    try:
        ws = loop.run_until_complete(_connect_and_auth())
        m1_candles = loop.run_until_complete(_get_candles(ws, asset, 60, 20))
        m5_candles = loop.run_until_complete(_get_candles(ws, asset, 300, 20))
        m15_candles = loop.run_until_complete(_get_candles(ws, asset, 900, 20))
        ws.close()
        
        last_m1 = m1_candles[-1] if m1_candles else {}
        last_m5 = m5_candles[-1] if m5_candles else {}
        last_m15 = m15_candles[-1] if m15_candles else {}
        
        result = f"""📊 LIVE данные: {asset}

🕯️ M1 (последняя свеча): Open: {last_m1.get('open', 'N/A')}, Close: {last_m1.get('close', 'N/A')} (всего: {len(m1_candles)})
🕯️ M5 (последняя свеча): Open: {last_m5.get('open', 'N/A')}, Close: {last_m5.get('close', 'N/A')} (всего: {len(m5_candles)})
🕯️ M15 (последняя свеча): Open: {last_m15.get('open', 'N/A')}, Close: {last_m15.get('close', 'N/A')} (всего: {len(m15_candles)})

✅ Коннектор работает! Данные получены напрямую из Pocket Option.
"""
        return result
    except asyncio.TimeoutError:
        return "❌ Таймаут: Pocket Option не ответил за 10 секунд."
    except Exception as e:
        return f"❌ Ошибка: {str(e)}"
    finally:
        loop.close()

if __name__ == "__main__":
    print(get_market_data("EURUSD_otc"))
