import time
import math
import requests
from typing import Optional, Tuple, List

# ===========================
# CONFIG
# ===========================
BASE_URL = "http://localhost:10003/v1"
API_KEY  = "G3PE3PRN" 

TICKER = "ALGO"

# Quote behavior
IMPROVE = 0.01
MIN_BOOK_SPREAD = 0.02
REFRESH_SEC = 0.25

# “Desired” spread control (used only when book is wide)
TARGET_HALF_SPREAD = 0.01

# Requote control
REQUOTE_EPS = 0.01
MAX_QUOTE_AGE = 1.0

# ===========================
# SIZE / RISK (UPDATED: bigger but stable)
# ===========================
BASE_SIZE = 5000
MAX_SIZE  = 15000
MIN_SIZE  = 1000

MAX_ABS_POS = 30000

# Skew (inventory control) — slightly weaker so bigger size doesn't self-disable
SKEW_PER_1K = 0.006
ASYM_SKEW = 0.5

# Vol widening
VOL_LOOKBACK = 25
VOL_MULTIPLIER = 10.0

# API safety
HTTP_TIMEOUT = 5
MIN_REQUEST_INTERVAL = 0.08

HEADERS = {"X-API-Key": API_KEY}

# ===========================
# Rate limiter + session
# ===========================
class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = float(min_interval)
        self.last = 0.0

    def wait(self):
        now = time.time()
        dt = now - self.last
        if dt < self.min_interval:
            time.sleep(self.min_interval - dt)
        self.last = time.time()

rl = RateLimiter(MIN_REQUEST_INTERVAL)
sess = requests.Session()
sess.headers.update(HEADERS)

def get_json(path: str, params=None):
    rl.wait()
    r = sess.get(BASE_URL + path, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

def post_params(path: str, params: dict):
    rl.wait()
    r = sess.post(BASE_URL + path, params=params, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        print("❌ ORDER REJECTED", r.status_code, r.text)
        print("Params:", params)
    r.raise_for_status()
    return r.json()

def delete_json(path: str):
    rl.wait()
    r = sess.delete(BASE_URL + path, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json() if r.text else {}

# ===========================
# RIT endpoints
# ===========================
def case_active() -> bool:
    c = get_json("/case")
    return c.get("status") == "ACTIVE"

def best_bid_ask(ticker: str) -> Tuple[Optional[float], Optional[float]]:
    book = get_json("/securities/book", params={"ticker": ticker})
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None
    return float(bids[0]["price"]), float(asks[0]["price"])

def get_position(ticker: str) -> int:
    secs = get_json("/securities")
    for s in secs:
        if s.get("ticker") == ticker:
            return int(s.get("position", 0))
    return 0

def open_orders() -> List[dict]:
    return get_json("/orders", params={"status": "OPEN"})

def cancel_order(order_id: int):
    delete_json(f"/orders/{order_id}")

def cancel_my_quotes(ticker: str):
    for o in open_orders():
        if o.get("ticker") != ticker:
            continue
        oid = o.get("order_id", o.get("id"))
        if oid is None:
            continue
        try:
            cancel_order(int(oid))
        except Exception:
            pass

def place_limit(ticker: str, action: str, qty: int, price: float):
    params = {
        "ticker": ticker,
        "type": "LIMIT",
        "quantity": int(qty),
        "price": round(float(price), 2),
        "action": action,
    }
    return post_params("/orders", params)

def place_market(ticker: str, action: str, qty: int):
    params = {
        "ticker": ticker,
        "type": "MARKET",
        "quantity": int(qty),
        "action": action,
    }
    return post_params("/orders", params)

# ===========================
# Helpers
# ===========================
def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def compute_vol_proxy(mids: List[float]) -> float:
    if len(mids) < 5:
        return 0.0
    rets = []
    for i in range(1, len(mids)):
        if mids[i - 1] > 0:
            rets.append((mids[i] - mids[i - 1]) / mids[i - 1])
    if len(rets) < 3:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)

def compute_size(pos: int) -> int:
    """
    Bigger sizing + slower decay with inventory.
    """
    room_frac = max(0.0, (MAX_ABS_POS - abs(pos)) / MAX_ABS_POS)
    size = BASE_SIZE * (0.5 + 0.5 * room_frac)   # slower decay than before
    return int(clamp(size, MIN_SIZE, MAX_SIZE))

def maybe_flatten(pos: int):
    if abs(pos) < MAX_ABS_POS:
        return
    qty = min(20_000, max(2_000, int(abs(pos) * 0.4)))
    action = "SELL" if pos > 0 else "BUY"
    try:
        place_market(TICKER, action, qty)
        print(f"🧯 FLATTEN {action} {qty} (pos={pos})")
    except Exception:
        pass

def compute_quotes(bid: float, ask: float, pos: int, vol: float) -> Tuple[Optional[float], Optional[float]]:
    spread = ask - bid
    if spread < MIN_BOOK_SPREAD:
        return None, None

    mid = 0.5 * (bid + ask)

    # Vol-adjust half spread
    half = TARGET_HALF_SPREAD + VOL_MULTIPLIER * vol * mid
    half = max(0.01, half)

    # Inventory skew (asymmetric)
    skew_units = (pos / 1000.0) * SKEW_PER_1K

    raw_bid = mid - half
    raw_ask = mid + half

    # Asymmetric skew
    bid_skew = +(-skew_units) * (1.0 + ASYM_SKEW)
    ask_skew = +(-skew_units) * (1.0 - ASYM_SKEW)

    q_bid = raw_bid + bid_skew
    q_ask = raw_ask + ask_skew

    # Improve top-of-book
    q_bid = max(q_bid, bid + IMPROVE)
    q_ask = min(q_ask, ask - IMPROVE)

    # Never cross
    q_bid = min(q_bid, ask - 0.01)
    q_ask = max(q_ask, bid + 0.01)

    q_bid = round(q_bid, 2)
    q_ask = round(q_ask, 2)

    if q_bid >= q_ask:
        return None, None

    return q_bid, q_ask

# ===========================
# MAIN
# ===========================
def main():
    print("\n🚀 ALGO2 Enhanced Market Maker (Bigger size) started")
    print(f"TICKER={TICKER}  BASE_URL={BASE_URL}")
    print(f"SIZE: BASE={BASE_SIZE}, MAX={MAX_SIZE}, MIN={MIN_SIZE}, MAX_ABS_POS={MAX_ABS_POS}")

    mids: List[float] = []
    last_bid = None
    last_ask = None
    last_quote_time = 0.0

    while True:
        try:
            if not case_active():
                print("⏹ Case not ACTIVE. Stop.")
                break

            bid, ask = best_bid_ask(TICKER)
            if bid is None or ask is None:
                time.sleep(REFRESH_SEC)
                continue

            mid = 0.5 * (bid + ask)
            mids.append(mid)
            if len(mids) > VOL_LOOKBACK:
                mids.pop(0)

            vol = compute_vol_proxy(mids)
            pos = get_position(TICKER)

            if abs(pos) >= MAX_ABS_POS:
                print(f"🛑 RISK STOP pos={pos}. Cancel quotes & flatten.")
                cancel_my_quotes(TICKER)
                maybe_flatten(pos)
                time.sleep(0.5)
                continue

            q_bid, q_ask = compute_quotes(bid, ask, pos, vol)
            if q_bid is None or q_ask is None:
                cancel_my_quotes(TICKER)
                time.sleep(REFRESH_SEC)
                continue

            now = time.time()
            must_refresh = (now - last_quote_time) > MAX_QUOTE_AGE
            moved = (last_bid is None or last_ask is None or
                     abs(q_bid - last_bid) >= REQUOTE_EPS or abs(q_ask - last_ask) >= REQUOTE_EPS)

            if moved or must_refresh:
                cancel_my_quotes(TICKER)
                size = compute_size(pos)
                place_limit(TICKER, "BUY", size, q_bid)
                place_limit(TICKER, "SELL", size, q_ask)
                last_bid, last_ask = q_bid, q_ask
                last_quote_time = now

            print(f"[QUOTE] pos={pos:>6} mid={mid:>7.2f}  "
                  f"mkt={bid:>6.2f}/{ask:>6.2f}  quote={q_bid:>6.2f}/{q_ask:>6.2f}  "
                  f"size={compute_size(pos):>5} vol={vol:.5f}")

            time.sleep(REFRESH_SEC)

        except KeyboardInterrupt:
            print("\nStopping (CTRL+C). Canceling open quotes…")
            cancel_my_quotes(TICKER)
            break
        except requests.HTTPError as e:
            print(f"HTTP error: {e}. Backing off.")
            time.sleep(0.5)
        except Exception as e:
            print("Error:", repr(e))
            time.sleep(0.5)

if __name__ == "__main__":
    main()
