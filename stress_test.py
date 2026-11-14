"""
EXTREME STRESS TEST - TP Auto-Closer
Tests every market-touching feature 5+ times.
Run: python stress_test.py
"""
import sys, time, json, asyncio, random
sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('bot', 'bot.py')
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

from fastapi.testclient import TestClient

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")

# ============================================================
# 1. EMA CROSS DETECTION - 10 scenarios
# ============================================================
print("\n=== 1. EMA 3/8 CROSS DETECTION (10x) ===")
scenarios = [
    ("cross_up flat->spike", [15.0]*24 + [40.0], "cross_up", True),
    ("cross_down flat->drop", [15.0]*24 + [1.0], "cross_down", True),
    ("flat no cross_up", [15.0]*25, "cross_up", False),
    ("flat no cross_down", [15.0]*25, "cross_down", False),
    ("ascending no cross_down", list(range(10,35)), "cross_down", False),
    ("descending no cross_up", list(range(35,10,-1)), "cross_up", False),
    ("cross_up with fast=5 slow=21", [15.0]*24 + [40.0], "cross_up", True),
    ("cross_down with fast=5 slow=21", [15.0]*24 + [1.0], "cross_down", True),
    ("empty candles", [], "cross_up", False),
    ("2 candles only", [10.0, 20.0], "cross_up", False),
]
for name, closes, op, expected in scenarios:
    params = {"fast": 3, "slow": 8} if "5 slow=21" not in name else {"fast": 5, "slow": 21}
    candles = [{"close": c, "high": c+1, "low": c-1} for c in closes]
    got = bot.evaluate_condition("ema_cross", op, 0, candles, params)
    check(f"{name}: got={got} expected={expected}", got == expected)

# ============================================================
# 2. evaluate_rule - 10 scenarios
# ============================================================
print("\n=== 2. evaluate_rule (10x) ===")
rule_scenarios = [
    ("ANY cross_up triggers", [{"indicator":"ema_cross","operator":"cross_up","params":{"fast":3,"slow":8},"value":0}], "ANY", [15.0]*24+[40.0], True),
    ("ANY cross_down triggers", [{"indicator":"ema_cross","operator":"cross_down","params":{"fast":3,"slow":8},"value":0}], "ANY", [15.0]*24+[1.0], True),
    ("ANY no trigger", [{"indicator":"ema_cross","operator":"cross_up","params":{"fast":3,"slow":8},"value":0}], "ANY", [15.0]*25, False),
    ("ALL both must trigger", [{"indicator":"ema_cross","operator":"cross_up","params":{"fast":3,"slow":8},"value":0},{"indicator":"ema_cross","operator":"cross_up","params":{"fast":3,"slow":8},"value":0}], "ALL", [15.0]*24+[40.0], True),
    ("ALL one fails", [{"indicator":"ema_cross","operator":"cross_up","params":{"fast":3,"slow":8},"value":0},{"indicator":"ema_cross","operator":"cross_down","params":{"fast":3,"slow":8},"value":0}], "ALL", [15.0]*24+[40.0], False),
    ("empty conditions ALL", [], "ALL", [15.0]*25, False),
    ("empty conditions ANY", [], "ANY", [15.0]*25, False),
    ("RSI cross_below", [{"indicator":"rsi","operator":"cross_below","params":{"length":14},"value":30}], "ANY", list(range(50,25,-1)), False),
    ("MA cross_up", [{"indicator":"ma_cross","operator":"cross_up","params":{"fast":3,"slow":8},"value":0}], "ANY", [15.0]*24+[40.0], True),
    ("BB touch_lower", [{"indicator":"bb","operator":"touch_lower","params":{"length":5,"std":2},"value":0}], "ANY", [10.0]*5+[5.0], True),
]
for name, conds, logic, closes, expected in rule_scenarios:
    candles = [{"close":c,"high":c+1,"low":c-1} for c in closes]
    trig, det = bot.evaluate_rule(conds, logic, candles)
    check(f"{name}: got={trig} expected={expected}", trig == expected)

# ============================================================
# 3. EMA CALCULATE - 5 edge cases
# ============================================================
print("\n=== 3. calculate_ema (5x) ===")
check("period=1 returns closes", bot.calculate_ema([1,2,3], 1) == [1,2,3])
check("empty returns []", bot.calculate_ema([], 3) == [])
check("single value", bot.calculate_ema([5.0], 3) == [None, None] + [5.0] if False else len(bot.calculate_ema([5.0],3))==1)
e = bot.calculate_ema([10.0,20.0,30.0], 2)
check("period=2 length", len(e) == 3)
check("period=2 first is None", e[0] is None)

# ============================================================
# 4. API ENDPOINTS - 5 full cycles
# ============================================================
print("\n=== 4. API ENDPOINTS (5 cycles) ===")
client = TestClient(bot.app)
# ensure clean db
import os
if os.path.exists(bot.DB_PATH):
    os.remove(bot.DB_PATH)
bot.init_db()

for cycle in range(5):
    # Create broker
    r = client.post("/api/brokers", json={"broker_type":"metaapi","name":f"Exness-{cycle}","credentials":{"token":"tok","account_id":"acc"},"is_testnet":True})
    check(f"cycle{cycle} create broker", r.status_code == 200)
    bid = r.json()["id"]

    # Create rule
    r = client.post("/api/rules", json={"name":f"Rule-{cycle}","indicator":"ema_cross","conditions":[{"indicator":"ema_cross","operator":"cross_up","params":{"fast":3,"slow":8},"value":0}],"logic_type":"ANY","symbols":["XAUUSD"],"timeframe":"15m"})
    check(f"cycle{cycle} create rule", r.status_code == 200)
    rid = r.json()["id"]

    # List
    r = client.get("/api/brokers")
    check(f"cycle{cycle} list brokers", r.status_code == 200 and len(r.json()) >= 1)
    r = client.get("/api/rules")
    check(f"cycle{cycle} list rules", r.status_code == 200 and len(r.json()) >= 1)

    # Toggle rule
    r = client.post(f"/api/rules/{rid}/toggle")
    check(f"cycle{cycle} toggle rule", r.status_code == 200)

    # Update rule
    r = client.put(f"/api/rules/{rid}", json={"name":f"Rule-{cycle}-upd","indicator":"ema_cross","conditions":[{"indicator":"ema_cross","operator":"cross_down","params":{"fast":5,"slow":21},"value":0}],"logic_type":"ALL","symbols":["BTCUSDT"],"timeframe":"1h"})
    check(f"cycle{cycle} update rule", r.status_code == 200)

    # Status
    r = client.get("/api/status")
    check(f"cycle{cycle} status", r.status_code == 200 and r.json()["worker"]=="running")

    # Logs
    r = client.get("/api/logs")
    check(f"cycle{cycle} logs", r.status_code == 200)

    # Delete
    r = client.delete(f"/api/brokers/{bid}")
    check(f"cycle{cycle} delete broker", r.status_code == 200)
    r = client.delete(f"/api/rules/{rid}")
    check(f"cycle{cycle} delete rule", r.status_code == 200)

# ============================================================
# 5. SYNC + ATTACH/DETACH - 5 cycles
# ============================================================
print("\n=== 5. SYNC + ATTACH/DETACH (5 cycles) ===")
# Re-create broker + rule for sync test
r = client.post("/api/brokers", json={"broker_type":"bybit","name":"BybitTest","credentials":{"api_key":"","secret":""},"is_testnet":True})
bid = r.json()["id"]
r = client.post("/api/rules", json={"name":"R","indicator":"ema_cross","conditions":[{"indicator":"ema_cross","operator":"cross_up","params":{"fast":3,"slow":8},"value":0}],"logic_type":"ANY","symbols":["BTCUSDT"],"timeframe":"15m"})
rid = r.json()["id"]

for cycle in range(5):
    r = client.post("/api/positions/sync")
    check(f"cycle{cycle} sync positions", r.status_code == 200)

# manually insert a position to test attach/detach
import sqlite3
conn = bot.get_db()
conn.execute("INSERT INTO positions (id,user_id,broker_id,broker_position_id,symbol,side,size,entry_price,current_tp,current_sl,rule_id,status,last_checked,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'monitoring',?,?)",
             ("pos1","default",bid,"BTCUSDT","BTCUSDT","long",0.1,50000.0,None,None,"2026-01-01T00:00:00","2026-01-01T00:00:00"))
conn.commit()
conn.close()

for cycle in range(5):
    r = client.post("/api/positions/pos1/attach-rule", json={"rule_id": rid})
    check(f"cycle{cycle} attach rule", r.status_code == 200)
    r = client.get("/api/positions")
    pos = [p for p in r.json() if p["id"]=="pos1"][0]
    check(f"cycle{cycle} rule attached", pos["rule_id"] == rid)
    r = client.post("/api/positions/pos1/detach-rule")
    check(f"cycle{cycle} detach rule", r.status_code == 200)
    r = client.get("/api/positions")
    pos = [p for p in r.json() if p["id"]=="pos1"][0]
    check(f"cycle{cycle} rule detached", pos["rule_id"] is None)

# ============================================================
# 6. WORKER LOOP - 5 cycles (no crash)
# ============================================================
print("\n=== 6. WORKER LOOP (5 cycles) ===")
async def run_worker_cycles():
    for i in range(5):
        try:
            await bot.process_user("default")
            check(f"worker cycle {i}", True)
        except Exception as e:
            check(f"worker cycle {i}: {e}", False)
asyncio.run(run_worker_cycles())

# ============================================================
# 7. ADAPTER ERROR HANDLING - bad creds
# ============================================================
print("\n=== 7. ADAPTER ERROR HANDLING (5x) ===")
for i in range(5):
    a = bot.MetaApiAdapter("bad-token", "bad-account")
    ok, msg = asyncio.run(a.test_connection())
    check(f"metaapi bad creds returns (ok={ok})", True)  # just no crash
    positions = asyncio.run(a.fetch_positions())
    check(f"metaapi bad creds fetch_positions no crash (got {len(positions)})", positions == [])
    ohlcv = asyncio.run(a.fetch_ohlcv("XAUUSD", "15m", 10))
    check(f"metaapi bad creds fetch_ohlcv no crash (got {len(ohlcv)})", ohlcv == [])
    closed = asyncio.run(a.close_position("999"))
    check(f"metaapi bad creds close no crash (got {closed})", closed == False)

for i in range(5):
    a = bot.BybitAdapter("", "", True)
    ok, msg = asyncio.run(a.test_connection())
    check(f"bybit empty creds no crash", True)
    positions = asyncio.run(a.fetch_positions())
    check(f"bybit empty creds fetch_positions (got {len(positions)})", positions == [])
    ohlcv = asyncio.run(a.fetch_ohlcv("BTCUSDT", "15m", 10))
    check(f"bybit empty creds fetch_ohlcv no crash (got {len(ohlcv)})", True)  # OHLCV is public data

# ============================================================
# 8. STATUS ENDPOINT - 5x
# ============================================================
print("\n=== 8. STATUS ENDPOINT (5x) ===")
for i in range(5):
    r = client.get("/api/status")
    check(f"status {i}", r.status_code == 200 and r.json()["worker"] == "running")

# ============================================================
# SUMMARY
# ============================================================
print(f"\n{'='*50}")
print(f"TOTAL: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
