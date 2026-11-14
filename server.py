# Auto TP Closer - Oracle Free Tier Bot
# Runs 24/7 on Oracle Cloud Free Tier (ARM Ubuntu)
# Close positions 100% when EMA 3 crosses EMA 8 (or any rule you set)
#
# Run:  python bot.py            (API + worker)
#       python bot.py worker     (worker only)
#
# Env:  see .env.example

import os, json, sqlite3, secrets, asyncio, time, hmac, hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

# ---- Load .env (optional) ----
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import uvicorn
import httpx

# ============= CONFIG =============
DB_PATH = os.environ.get("DB_PATH", "bot.db")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # single-user password; empty = no auth (local dev)
META_API_TOKEN = os.environ.get("META_API_TOKEN", "")
META_ACCOUNT_ID = os.environ.get("META_ACCOUNT_ID", "")
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_SECRET = os.environ.get("BYBIT_SECRET", "")
BYBIT_TESTNET = os.environ.get("BYBIT_TESTNET", "true").lower() in ("1", "true", "yes")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "15"))

META_BASE = "https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai"

# ============= DATABASE =============
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def db_transaction():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db_transaction() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, created_at TEXT);
        CREATE TABLE IF NOT EXISTS brokers (id TEXT PRIMARY KEY, user_id TEXT, broker_type TEXT, name TEXT, credentials TEXT, is_testnet INTEGER, is_active INTEGER, created_at TEXT);
        CREATE TABLE IF NOT EXISTS tp_rules (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, indicator TEXT, conditions TEXT, logic_type TEXT, symbols TEXT, timeframe TEXT, is_active INTEGER, created_at TEXT);
        CREATE TABLE IF NOT EXISTS positions (id TEXT PRIMARY KEY, user_id TEXT, broker_id TEXT, broker_position_id TEXT, symbol TEXT, side TEXT, size REAL, entry_price REAL, current_tp REAL, current_sl REAL, rule_id TEXT, status TEXT, last_checked TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS tp_logs (id TEXT PRIMARY KEY, position_id TEXT, rule_id TEXT, trigger_condition TEXT, tp_price REAL, sl_price REAL, success INTEGER, error_message TEXT, created_at TEXT);
        """)
        # ensure default user
        if not conn.execute("SELECT 1 FROM users WHERE id='default'").fetchone():
            conn.execute("INSERT INTO users (id, created_at) VALUES ('default', ?)", (datetime.utcnow().isoformat(),))

init_db()

# ============= AUTH (single-user password) =============
def get_current_user(request: Request) -> str:
    if not APP_PASSWORD:
        return "default"
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    # also accept ?token= query for simple clients
    if not token:
        token = request.query_params.get("token", "")
    if token != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized: bad password")
    return "default"

# ============= EMA / INDICATORS =============
def calculate_ema(closes: List[float], period: int) -> List[Optional[float]]:
    if not closes:
        return []
    k = 2 / (period + 1)
    ema = [closes[0]]
    for i in range(1, len(closes)):
        ema.append(closes[i] * k + ema[-1] * (1 - k))
    if period <= 1:
        return ema
    pad = min(period - 1, len(closes))
    return [None] * pad + ema[pad:]

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi = [None] * period
    for i in range(period, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i-1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i-1]) / period
        rs = avg_gain / avg_loss if avg_loss else 100
        rsi.append(100 - (100 / (1 + rs)))
    return rsi

def calculate_sma(closes, period):
    out = []
    for i in range(len(closes)):
        if i >= period - 1:
            out.append(sum(closes[i-period+1:i+1]) / period)
        else:
            out.append(None)
    return out

def calculate_bb(closes, period=20, std=2.0):
    sma = calculate_sma(closes, period)
    upper, lower = [], []
    for i in range(len(closes)):
        if i >= period - 1:
            window = closes[i-period+1:i+1]
            m = sum(window) / period
            var = sum((x - m) ** 2 for x in window) / period
            sd = var ** 0.5
            upper.append(m + std * sd)
            lower.append(m - std * sd)
        else:
            upper.append(None)
            lower.append(None)
    return upper, sma, lower

def calculate_williams_r(highs, lows, closes, period=14):
    out = []
    for i in range(len(closes)):
        if i >= period - 1:
            hh = max(highs[i-period+1:i+1])
            ll = min(lows[i-period+1:i+1])
            out.append((hh - closes[i]) / (hh - ll) * -100 if hh != ll else -50)
        else:
            out.append(None)
    return out

def evaluate_condition(indicator, operator, value, candles, params=None):
    if len(candles) < 3:
        return False
    params = params or {}
    closes = [c["close"] for c in candles]
    last = candles[-1]
    prev = candles[-2]

    if indicator == "ema_cross":
        fast_p = int(params.get("fast", 3))
        slow_p = int(params.get("slow", 8))
        if len(closes) < slow_p + 1:
            return False
        ema_fast = calculate_ema(closes, fast_p)
        ema_slow = calculate_ema(closes, slow_p)
        cur_f, cur_s = ema_fast[-1], ema_slow[-1]
        pre_f, pre_s = ema_fast[-2], ema_slow[-2]
        if None in (cur_f, cur_s, pre_f, pre_s):
            return False
        eps = 1e-9
        if operator in ("cross_up", "cross_above"):
            return pre_f <= pre_s + eps and cur_f > cur_s + eps
        if operator in ("cross_down", "cross_below"):
            return pre_f >= pre_s - eps and cur_f < cur_s - eps
        return False

    if indicator == "rsi":
        period = int(params.get("length", 14))
        rsi = calculate_rsi(closes, period)
        cur, pre = rsi[-1], rsi[-2]
        if cur is None or pre is None:
            return False
        if operator == "cross_below":
            return pre > value >= cur
        if operator == "cross_above":
            return pre < value <= cur
        if operator == "<":
            return cur < value
        if operator == ">":
            return cur > value
        return False

    if indicator == "ma_cross":
        fast_p = int(params.get("fast", 9))
        slow_p = int(params.get("slow", 21))
        if len(candles) < slow_p + 1:
            return False
        fast = sum(c["close"] for c in candles[-fast_p:]) / fast_p
        slow = sum(c["close"] for c in candles[-slow_p:]) / slow_p
        fast_pre = sum(c["close"] for c in candles[-(fast_p+1):-1]) / fast_p
        slow_pre = sum(c["close"] for c in candles[-(slow_p+1):-1]) / slow_p
        if operator == "cross_up":
            return fast_pre <= slow_pre and fast > slow
        if operator == "cross_down":
            return fast_pre >= slow_pre and fast < slow
        return False

    if indicator == "williams_r":
        period = int(params.get("length", 14))
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        wr = calculate_williams_r(highs, lows, closes, period)
        cur, pre = wr[-1], wr[-2]
        if cur is None or pre is None:
            return False
        if operator == "cross_below":
            return pre > value >= cur
        if operator == "cross_above":
            return pre < value <= cur
        if operator == "<":
            return cur < value
        if operator == ">":
            return cur > value
        return False

    if indicator == "bb":
        period = int(params.get("length", 20))
        std = float(params.get("std", 2))
        upper, mid, lower = calculate_bb(closes, period, std)
        if lower[-1] is None or upper[-1] is None:
            return False
        cur_close = last["close"]
        pre_close = prev["close"]
        if operator == "touch_lower":
            return cur_close <= lower[-1]
        if operator == "touch_upper":
            return cur_close >= upper[-1]
        if operator == "price_cross_below":
            if lower[-2] is None:
                return False
            return pre_close >= lower[-2] and cur_close < lower[-1]
        if operator == "price_cross_above":
            if upper[-2] is None:
                return False
            return pre_close <= upper[-2] and cur_close > upper[-1]
        return False

    if indicator == "stoch_rsi":
        period = int(params.get("length", 14))
        rsi = calculate_rsi(closes, period)
        cur, pre = rsi[-1], rsi[-2]
        if cur is None or pre is None:
            return False
        if operator == "cross_below":
            return pre > value >= cur
        if operator == "cross_above":
            return pre < value <= cur
        if operator == "cross_up":
            return pre <= 50 and cur > 50
        if operator == "cross_down":
            return pre >= 50 and cur < 50
        return False

    if indicator == "macd":
        fast_p = int(params.get("fast", 12))
        slow_p = int(params.get("slow", 26))
        signal_p = int(params.get("signal", 9))
        if len(closes) < slow_p + signal_p + 5:
            return False
        ema_fast = calculate_ema(closes, fast_p)
        ema_slow = calculate_ema(closes, slow_p)
        macd = [a - b if a is not None and b is not None else None for a, b in zip(ema_fast, ema_slow)]
        macd_clean = [x for x in macd if x is not None]
        if len(macd_clean) < signal_p + 2:
            return False
        signal = calculate_ema(macd_clean, signal_p)
        cur_macd, pre_macd = macd_clean[-1], macd_clean[-2]
        cur_sig, pre_sig = signal[-1], signal[-2]
        if None in (cur_macd, pre_macd, cur_sig, pre_sig):
            return False
        if operator == "cross_up":
            return pre_macd <= pre_sig + 1e-9 and cur_macd > cur_sig + 1e-9
        if operator == "cross_down":
            return pre_macd >= pre_sig - 1e-9 and cur_macd < cur_sig - 1e-9
        return False

    if indicator == "ema":
        period = int(params.get("length", 200))
        ema = calculate_ema(closes, period)
        cur_ema, pre_ema = ema[-1], ema[-2]
        if cur_ema is None or pre_ema is None:
            return False
        cur_close, pre_close = last["close"], prev["close"]
        if operator == "price_cross_above":
            return pre_close <= pre_ema and cur_close > cur_ema
        if operator == "price_cross_below":
            return pre_close >= pre_ema and cur_close < cur_ema
        if operator == ">":
            return cur_close > cur_ema
        if operator == "<":
            return cur_close < cur_ema
        return False

    return False

def evaluate_rule(conditions, logic, candles):
    results = []
    triggered = {}
    for i, c in enumerate(conditions):
        res = evaluate_condition(c["indicator"], c["operator"], c.get("value", 0), candles, c.get("params", {}))
        results.append(res)
        if res:
            triggered[f"cond_{i}"] = c
    if logic == "ALL":
        return all(results) if results else False, triggered
    return any(results) if results else False, triggered

# ============= BROKER ADAPTERS =============
TF_MAP_META = {
    "1m": "M1", "3m": "M3", "5m": "M5", "15m": "M15",
    "30m": "M30", "1h": "H1", "2h": "H2", "4h": "H4",
    "6h": "H6", "12h": "H12", "1d": "D1",
}

class MetaApiAdapter:
    """Exness/XM MT5 via MetaApi cloud (no Windows VPS needed)."""
    def __init__(self, token: str, account_id: str):
        self.token = token
        self.account_id = account_id

    def _headers(self):
        return {"auth-token": self.token, "Content-Type": "application/json"}

    async def test_connection(self) -> tuple:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{META_BASE}/users/current/accounts/{self.account_id}/account-information", headers=self._headers())
                if r.status_code == 200:
                    return True, "Connected"
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)

    async def fetch_positions(self) -> List[Dict]:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{META_BASE}/users/current/accounts/{self.account_id}/positions", headers=self._headers())
                if r.status_code != 200:
                    return []
                out = []
                for p in r.json():
                    ptype = p.get("type", "")
                    side = "long" if "BUY" in ptype else "short"
                    out.append({
                        "broker_position_id": str(p.get("id")),
                        "symbol": p.get("symbol"),
                        "side": side,
                        "size": float(p.get("volume", 0)),
                        "entry_price": float(p.get("openPrice", 0)),
                        "current_tp": float(p.get("takeProfit")) if p.get("takeProfit") else None,
                        "current_sl": float(p.get("stopLoss")) if p.get("stopLoss") else None,
                    })
                return out
        except Exception as e:
            print(f"MetaApi fetch_positions error: {e}")
            return []

    async def fetch_ohlcv(self, symbol, timeframe, limit) -> List[Dict]:
        try:
            tf = TF_MAP_META.get(timeframe, "M15")
            start = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            url = f"{META_BASE}/users/current/accounts/{self.account_id}/historical-market-data/symbols/{symbol}/timeframes/{tf}/bars"
            params = {"startTime": start, "limit": limit}
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(url, headers=self._headers(), params=params)
                if r.status_code != 200:
                    return []
                out = []
                for b in r.json():
                    out.append({
                        "timestamp": b.get("time"),
                        "open": float(b.get("open", 0)),
                        "high": float(b.get("high", 0)),
                        "low": float(b.get("low", 0)),
                        "close": float(b.get("close", 0)),
                        "volume": float(b.get("tickVolume", 0)),
                    })
                return out[-limit:]
        except Exception as e:
            print(f"MetaApi fetch_ohlcv error: {e}")
            return []

    async def close_position(self, position_id: str, volume: Optional[float] = None) -> bool:
        try:
            payload = {"actionType": "POSITION_CLOSE_ID", "positionId": str(position_id)}
            if volume:
                payload["volume"] = volume
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{META_BASE}/users/current/accounts/{self.account_id}/trade", headers=self._headers(), json=payload)
                if r.status_code == 200:
                    res = r.json()
                    # MetaApi returns stringId / numericCode
                    if res.get("stringCode") == "TRADE_RETCODE_DONE" or res.get("numericCode") == 10009:
                        return True
                    # some brokers return success with different code; treat 2xx as ok if no error
                    if "error" not in str(res).lower():
                        return True
                print(f"MetaApi close response: {r.status_code} {r.text[:300]}")
                return False
        except Exception as e:
            print(f"MetaApi close error: {e}")
            return False

    async def modify_tp_sl(self, position_id, tp, sl):
        return await self.close_position(position_id)


class BybitAdapter:
    def __init__(self, api_key, secret, testnet=True):
        self.api_key = api_key
        self.secret = secret
        self.testnet = testnet

    def _ex(self):
        import ccxt
        ex = ccxt.bybit({"apiKey": self.api_key, "secret": self.secret, "enableRateLimit": True})
        if self.testnet:
            ex.set_sandbox_mode(True)
        return ex

    async def test_connection(self):
        if not self.api_key or not self.secret:
            return False, "API key/secret kosong"
        try:
            ex = self._ex()
            ex.fetch_balance()
            return True, "Connected"
        except Exception as e:
            return False, str(e)

    async def fetch_positions(self):
        try:
            ex = self._ex()
            poss = ex.fetch_positions()
            out = []
            for p in poss:
                size = float(p.get("contracts") or p.get("info", {}).get("size") or 0)
                if size == 0:
                    continue
                out.append({
                    "broker_position_id": p["symbol"],
                    "symbol": p["symbol"],
                    "side": p["side"],
                    "size": size,
                    "entry_price": float(p.get("entryPrice") or p.get("info", {}).get("avgPrice") or 0),
                    "current_tp": float(p["info"].get("takeProfit")) if p.get("info", {}).get("takeProfit") else None,
                    "current_sl": float(p["info"].get("stopLoss")) if p.get("info", {}).get("stopLoss") else None,
                })
            return out
        except Exception as e:
            print(f"Bybit fetch_positions error: {e}")
            return []

    async def fetch_ohlcv(self, symbol, timeframe, limit):
        TF = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720", "1d": "D"}
        try:
            ex = self._ex()
            ohlcv = ex.fetch_ohlcv(symbol, TF.get(timeframe, "15"), limit=limit)
            return [{"timestamp": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]} for c in ohlcv]
        except Exception as e:
            print(f"Bybit fetch_ohlcv error: {e}")
            return []

    async def close_position(self, symbol, side, size):
        try:
            ex = self._ex()
            opposite = "sell" if side == "long" else "buy"
            ex.create_order(symbol, "Market", opposite, size, None, {"reduceOnly": True})
            return True
        except Exception as e:
            print(f"Bybit close error: {e}")
            return False

    async def modify_tp_sl(self, position_id, tp, sl):
        return await self.close_position(position_id, "long", 0)

# ============= HELPERS =============
def get_adapter(broker_row):
    creds = json.loads(broker_row["credentials"])
    if broker_row["broker_type"] == "bybit":
        return BybitAdapter(creds["api_key"], creds["secret"], bool(broker_row["is_testnet"]))
    # metaapi / mt5
    return MetaApiAdapter(creds.get("token") or creds.get("api_token", ""), creds.get("account_id", ""))

# ============= FASTAPI =============
app = FastAPI(title="TP Auto-Closer")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return HTMLResponse("<h1>TP Auto-Closer running. Put index.html next to bot.py.</h1>")

@app.get("/manifest.json")
def manifest():
    if os.path.exists("manifest.json"):
        return FileResponse("manifest.json", media_type="application/manifest+json")
    raise HTTPException(404, "manifest not found")

@app.get("/sw.js")
def service_worker():
    if os.path.exists("sw.js"):
        return FileResponse("sw.js", media_type="application/javascript")

@app.get("/icon.svg")
def icon():
    if os.path.exists("icon.svg"):
        return FileResponse("icon.svg", media_type="image/svg+xml")

@app.get("/api/status")
def status(user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        brokers = conn.execute("SELECT COUNT(*) FROM brokers WHERE user_id=? AND is_active=1", (user_id,)).fetchone()[0]
        rules = conn.execute("SELECT COUNT(*) FROM tp_rules WHERE user_id=? AND is_active=1", (user_id,)).fetchone()[0]
        monitoring = conn.execute("SELECT COUNT(*) FROM positions WHERE user_id=? AND status='monitoring' AND rule_id IS NOT NULL", (user_id,)).fetchone()[0]
    return {"worker": "running", "brokers": brokers, "rules": rules, "monitoring": monitoring, "time": datetime.utcnow().isoformat(), "interval_sec": POLL_INTERVAL}

# ----- Brokers -----
class BrokerIn(BaseModel):
    broker_type: str
    name: str
    credentials: Dict[str, Any]
    is_testnet: bool = True

@app.get("/api/brokers")
def list_brokers(user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        rows = conn.execute("SELECT * FROM brokers WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    return [{"id": r["id"], "broker_type": r["broker_type"], "name": r["name"], "is_testnet": bool(r["is_testnet"]), "is_active": bool(r["is_active"]), "created_at": r["created_at"]} for r in rows]

@app.post("/api/brokers")
def create_broker(b: BrokerIn, user_id: str = Depends(get_current_user)):
    bid = secrets.token_hex(8)
    with db_transaction() as conn:
        conn.execute("INSERT INTO brokers (id,user_id,broker_type,name,credentials,is_testnet,is_active,created_at) VALUES (?,?,?,?,?,?,1,?)",
                     (bid, user_id, b.broker_type, b.name, json.dumps(b.credentials), int(b.is_testnet), datetime.utcnow().isoformat()))
    return {"id": bid, "ok": True}

@app.delete("/api/brokers/{bid}")
def delete_broker(bid: str, user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        conn.execute("DELETE FROM brokers WHERE id=? AND user_id=?", (bid, user_id))
    return {"ok": True}

@app.post("/api/brokers/test")
async def test_broker_inline(req: Request, user_id: str = Depends(get_current_user)):
    body = await req.json()
    btype = body.get("broker_type")
    creds = body.get("credentials", {})
    testnet = body.get("is_testnet", True)
    if btype == "bybit":
        a = BybitAdapter(creds.get("api_key", ""), creds.get("secret", ""), testnet)
        ok, msg = await a.test_connection()
    else:
        a = MetaApiAdapter(creds.get("token") or creds.get("api_token", ""), creds.get("account_id", ""))
        ok, msg = await a.test_connection()
    return {"success": ok, "message": msg}

@app.post("/api/brokers/{bid}/test")
async def test_broker_saved(bid: str, user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        r = conn.execute("SELECT * FROM brokers WHERE id=? AND user_id=?", (bid, user_id)).fetchone()
    if not r:
        raise HTTPException(404, "broker not found")
    a = get_adapter(r)
    if isinstance(a, BybitAdapter):
        ok, msg = await a.test_connection()
    else:
        ok, msg = await a.test_connection()
    # update active flag
    with db_transaction() as conn:
        conn.execute("UPDATE brokers SET is_active=? WHERE id=?", (1 if ok else 0, bid))
    return {"success": ok, "message": msg}

# ----- Rules -----
class RuleIn(BaseModel):
    name: str
    indicator: str = "ema_cross"
    conditions: List[Dict[str, Any]]
    logic_type: str = "ANY"
    symbols: List[str]
    timeframe: str = "15m"

@app.get("/api/rules")
def list_rules(user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        rows = conn.execute("SELECT * FROM tp_rules WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    return [{"id": r["id"], "name": r["name"], "indicator": r["indicator"], "conditions": json.loads(r["conditions"]), "logic_type": r["logic_type"], "symbols": json.loads(r["symbols"]), "timeframe": r["timeframe"], "is_active": bool(r["is_active"]), "created_at": r["created_at"]} for r in rows]

@app.post("/api/rules")
def create_rule(r: RuleIn, user_id: str = Depends(get_current_user)):
    rid = secrets.token_hex(8)
    with db_transaction() as conn:
        conn.execute("INSERT INTO tp_rules (id,user_id,name,indicator,conditions,logic_type,symbols,timeframe,is_active,created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
                     (rid, user_id, r.name, r.indicator, json.dumps(r.conditions), r.logic_type, json.dumps(r.symbols), r.timeframe, datetime.utcnow().isoformat()))
    return {"id": rid, "ok": True}

@app.put("/api/rules/{rid}")
def update_rule(rid: str, r: RuleIn, user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        conn.execute("UPDATE tp_rules SET name=?,indicator=?,conditions=?,logic_type=?,symbols=?,timeframe=? WHERE id=? AND user_id=?",
                     (r.name, r.indicator, json.dumps(r.conditions), r.logic_type, json.dumps(r.symbols), r.timeframe, rid, user_id))
    return {"ok": True}

@app.delete("/api/rules/{rid}")
def delete_rule(rid: str, user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        conn.execute("DELETE FROM tp_rules WHERE id=? AND user_id=?", (rid, user_id))
    return {"ok": True}

@app.post("/api/rules/{rid}/toggle")
def toggle_rule(rid: str, user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        conn.execute("UPDATE tp_rules SET is_active = 1 - is_active WHERE id=? AND user_id=?", (rid, user_id))
    return {"ok": True}

# ----- Positions -----
@app.get("/api/positions")
def list_positions(status: Optional[str] = None, user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        q = "SELECT p.*, r.name as rule_name FROM positions p LEFT JOIN tp_rules r ON p.rule_id=r.id WHERE p.user_id=?"
        params = [user_id]
        if status:
            q += " AND p.status=?"
            params.append(status)
        rows = conn.execute(q + " ORDER BY p.created_at DESC", params).fetchall()
    return [{"id": r["id"], "broker_id": r["broker_id"], "broker_position_id": r["broker_position_id"], "symbol": r["symbol"], "side": r["side"], "size": r["size"], "entry_price": r["entry_price"], "current_tp": r["current_tp"], "current_sl": r["current_sl"], "rule_id": r["rule_id"], "rule_name": r["rule_name"], "status": r["status"], "last_checked": r["last_checked"]} for r in rows]

@app.post("/api/positions/sync")
async def sync_positions(user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        brokers = conn.execute("SELECT * FROM brokers WHERE user_id=? AND is_active=1", (user_id,)).fetchall()
    total = 0
    for b in brokers:
        a = get_adapter(b)
        poss = await (a.fetch_positions() if hasattr(a, "fetch_positions") else [])
        with db_transaction() as conn:
            for p in poss:
                exists = conn.execute("SELECT id FROM positions WHERE user_id=? AND broker_id=? AND broker_position_id=? AND status='monitoring'", (user_id, b["id"], p["broker_position_id"])).fetchone()
                if not exists:
                    conn.execute("INSERT INTO positions (id,user_id,broker_id,broker_position_id,symbol,side,size,entry_price,current_tp,current_sl,rule_id,status,last_checked,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'monitoring',?,?)",
                                 (secrets.token_hex(8), user_id, b["id"], p["broker_position_id"], p["symbol"], p["side"], p["size"], p["entry_price"], p["current_tp"], p["current_sl"], datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
                    total += 1
    return {"synced": total}

@app.post("/api/positions/{pid}/attach-rule")
async def attach_rule(pid: str, request: Request, user_id: str = Depends(get_current_user)):
    body = await request.json()
    rid = body.get("rule_id")
    with db_transaction() as conn:
        conn.execute("UPDATE positions SET rule_id=? WHERE id=? AND user_id=?", (rid, pid, user_id))
    return {"ok": True}

@app.post("/api/positions/{pid}/detach-rule")
def detach_rule(pid: str, user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        conn.execute("UPDATE positions SET rule_id=NULL WHERE id=? AND user_id=?", (pid, user_id))
    return {"ok": True}

# ----- Logs -----
@app.get("/api/logs")
def list_logs(limit: int = 100, user_id: str = Depends(get_current_user)):
    with db_transaction() as conn:
        rows = conn.execute("SELECT l.*, p.symbol, p.side FROM tp_logs l LEFT JOIN positions p ON l.position_id=p.id WHERE p.user_id=? ORDER BY l.created_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [{"id": r["id"], "position_id": r["position_id"], "symbol": r["symbol"], "side": r["side"], "rule_id": r["rule_id"], "trigger": json.loads(r["trigger_condition"]) if r["trigger_condition"] else None, "tp_price": r["tp_price"], "sl_price": r["sl_price"], "success": bool(r["success"]), "error": r["error_message"], "created_at": r["created_at"]} for r in rows]

# ============= WORKER LOOP =============
async def worker_loop():
    print(f"[worker] started, poll every {POLL_INTERVAL}s")
    while True:
        try:
            with db_transaction() as conn:
                users = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM positions WHERE status='monitoring' AND rule_id IS NOT NULL").fetchall()]
            for uid in users:
                await process_user(uid)
        except Exception as e:
            print(f"[worker] error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

async def process_user(user_id):
    with db_transaction() as conn:
        positions = conn.execute("""SELECT p.*, r.name as rule_name, r.conditions, r.logic_type, r.timeframe, r.symbols, r.is_active as rule_active
                                   FROM positions p JOIN tp_rules r ON p.rule_id=r.id
                                   WHERE p.user_id=? AND p.status='monitoring' AND r.is_active=1""", (user_id,)).fetchall()
        brokers = {r["id"]: r for r in conn.execute("SELECT * FROM brokers WHERE user_id=? AND is_active=1", (user_id,)).fetchall()}

    for pos in positions:
        broker = brokers.get(pos["broker_id"])
        if not broker:
            continue
        adapter = get_adapter(broker)
        rule = {"conditions": json.loads(pos["conditions"]), "logic_type": pos["logic_type"], "timeframe": pos["timeframe"], "symbols": json.loads(pos["symbols"])}
        # fetch candles
        candles = await adapter.fetch_ohlcv(pos["symbol"], rule["timeframe"], 100)
        if not candles or len(candles) < 10:
            with db_transaction() as conn:
                conn.execute("UPDATE positions SET last_checked=? WHERE id=?", (datetime.utcnow().isoformat(), pos["id"]))
            continue
        triggered, details = evaluate_rule(rule["conditions"], rule["logic_type"], candles)
        if triggered:
            print(f"[worker] TRIGGERED close for {pos['symbol']} (pos {pos['broker_position_id']})")
            ok = await adapter.close_position(pos["broker_position_id"], pos["side"], float(pos["size"]))
            tp_price = candles[-1]["close"]
            with db_transaction() as conn:
                conn.execute("""INSERT INTO tp_logs (id,position_id,rule_id,trigger_condition,tp_price,sl_price,success,error_message,created_at)
                                VALUES (?,?,?,?,?,?,?,?,?)""",
                             (secrets.token_hex(16), pos["id"], pos["rule_id"], json.dumps(details), tp_price, 0, 1 if ok else 0, None if ok else "close failed", datetime.utcnow().isoformat()))
                if ok:
                    conn.execute("UPDATE positions SET status='tp_hit', last_checked=? WHERE id=?", (datetime.utcnow().isoformat(), pos["id"]))
                else:
                    conn.execute("UPDATE positions SET last_checked=? WHERE id=?", (datetime.utcnow().isoformat(), pos["id"]))
        else:
            with db_transaction() as conn:
                conn.execute("UPDATE positions SET last_checked=? WHERE id=?", (datetime.utcnow().isoformat(), pos["id"]))

# ============= MAIN =============
@app.on_event("startup")
async def startup():
    asyncio.create_task(worker_loop())

def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        # worker-only mode (for systemd)
        async def run():
            await worker_loop()
        asyncio.run(run())
    else:
        init_db()
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

if __name__ == "__main__":
    main()
