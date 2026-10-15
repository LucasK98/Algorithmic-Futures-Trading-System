"""
bot.py — Standalone TopstepX Trading Bot (corrected against real ProjectX API spec)

Opening Range Trend Scalp | 15 tick TP / 30 tick SL | ES

KEY CHANGES vs earlier versions (all verified against ProjectX docs):
  - Uses NATIVE bracket orders (stopLossBracket + takeProfitBracket on the
    entry order) — TP and SL are attached atomically, no naked-position gap.
  - retrieveBars uses the REAL parameter names (unit/unitNumber/startTime/etc).
  - Bars are sorted oldest-first (API returns newest-first).
  - Positions read from /Position/searchOpen (correct endpoint).
  - Flatten uses /Position/closeContract.
  - Win/loss detected from /Trade/search profitAndLoss (reliable), not by
    guessing which order cancelled.
  - Token re-auth safety net if a call ever returns 401.

Run with:  python bot.py
"""

import time
import sys
import logging
import requests
import os
import csv
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# Force UTF-8 output so emoji/box characters never crash Windows terminals (cp1252)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bot")

ET  = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# ─────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────
USERNAME    = os.getenv("PROJECTX_USERNAME", "YOUR_EMAIL_HERE")
API_KEY     = os.getenv("PROJECTX_API_KEY", "YOUR_API_KEY_HERE")
CONTRACT_ID = os.getenv("CONTRACT_ID", "")

API_BASE    = "https://api.topstepx.com/api"

# LIVE vs SIM data + trading. Set to False to trade the simulated account
# while you test. Flip to True only when you're ready for the real account.
USE_LIVE    = False

TICK_SIZE       = 0.25
TICK_VALUE      = 12.50
PROFIT_TICKS    = 15       # $183.30 after commission — clears $150 XFA threshold
STOP_TICKS      = 30       # $379.20 after commission
MAX_TRADES_DAY  = 10        # shared budget across BOTH sessions combined
MAX_LOSSES_DAY  = 1        # shared budget across BOTH sessions combined
COOLDOWN_MINS   = 5
EMA_PERIOD      = 9
POLL_INTERVAL   = 30

# ── TWO TRADING SESSIONS ──
# Each session builds its OWN opening range in its first 30 minutes,
# then trades the rest of the window. Times are ET as (hour, minute).
#
# Session AM = morning (VALIDATED — the 69% backtested window)
# Session PM = evening (EXPERIMENTAL — NOT backtested, unknown edge)
SESSIONS = [
    {   # Morning — validated
        "name":    "AM",
        "start":   (9, 30),
        "or_end":  (10, 0),
        "end":     (12, 0),
        "flatten": (12, 5),
    },
    {   # Evening — EXPERIMENTAL, unvalidated
        "name":    "PM",
        "start":   (19, 0),    # 7:00 PM ET
        "or_end":  (19, 30),   # OR locks 7:30 PM
        "end":     (21, 0),    # last entry 9:00 PM
        "flatten": (21, 5),    # flatten 9:05 PM
    },
]

# Order type / side / status enums (from ProjectX docs)
ORDER_LIMIT  = 1
ORDER_MARKET = 2
ORDER_STOP   = 4
SIDE_BUY     = 0   # Bid
SIDE_SELL    = 1   # Ask
STATUS_FILLED = 2


# ─────────────────────────────────────────────────────────────────
#  API CLIENT
# ─────────────────────────────────────────────────────────────────
class APIClient:

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.token = None
        self.account_id = None

    def authenticate(self) -> bool:
        resp = self.session.post(
            f"{API_BASE}/Auth/loginKey",
            json={"userName": USERNAME, "apiKey": API_KEY},
            timeout=10,
        )
        data = resp.json()
        if not data.get("success"):
            log.error("Auth failed: %s", data.get("errorMessage"))
            return False
        self.token = data["token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        log.info("Authenticated")
        return True

    def _post(self, path: str, body: dict, retry_auth: bool = True):
        """POST with one automatic re-auth retry on 401 (token expiry safety net)."""
        resp = self.session.post(f"{API_BASE}{path}", json=body, timeout=10)
        if resp.status_code == 401 and retry_auth:
            log.warning("401 - token may have expired, re-authenticating...")
            if self.authenticate():
                resp = self.session.post(f"{API_BASE}{path}", json=body, timeout=10)
        return resp

    def get_account(self) -> bool:
        resp = self._post("/Account/search", {"onlyActiveAccounts": True})
        accounts = resp.json().get("accounts", [])
        if not accounts:
            log.error("No active accounts found")
            return False
        acct = accounts[0]
        self.account_id = acct["id"]
        log.info("Account: %s (id=%s, balance=$%.2f, canTrade=%s)",
                 acct.get("name"), self.account_id,
                 acct.get("balance", 0), acct.get("canTrade"))
        if not acct.get("canTrade", False):
            log.warning("Account canTrade=False - orders will be rejected")
        return True

    def get_bars(self, contract_id: str, count: int = 120) -> list:
        now_utc   = datetime.now(UTC)
        start_utc = now_utc - timedelta(minutes=count + 15)
        resp = self._post("/History/retrieveBars", {
            "contractId":        contract_id,
            "live":              USE_LIVE,
            "startTime":         start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime":           now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit":              2,   # Minute
            "unitNumber":        1,
            "limit":             count,
            "includePartialBar": False,
        })
        data = resp.json()
        if not data.get("success"):
            log.error("retrieveBars failed: %s", data.get("errorMessage"))
            return []
        return data.get("bars", [])

    def place_bracket_order(self, side: int, size: int,
                            tp_ticks: int, sl_ticks: int) -> Optional[int]:
        """
        Single atomic order with native TP + SL brackets attached.

        Topstep expects bracket ticks SIGNED BY DIRECTION relative to entry:
          LONG  (side=0): TP is above entry (+),  SL is below entry (-)
          SHORT (side=1): TP is below entry (-),  SL is above entry (+)
        (Confirmed from API error: "Ticks should be less than zero when longing.")
        """
        tp_ticks = abs(tp_ticks)
        sl_ticks = abs(sl_ticks)

        if side == SIDE_BUY:        # LONG
            tp_signed = +tp_ticks   # take profit above
            sl_signed = -sl_ticks   # stop loss below
        else:                       # SHORT
            tp_signed = -tp_ticks   # take profit below
            sl_signed = +sl_ticks   # stop loss above

        body = {
            "accountId":  self.account_id,
            "contractId": CONTRACT_ID,
            "type":       ORDER_MARKET,
            "side":       side,
            "size":       size,
            "takeProfitBracket": {"ticks": tp_signed, "type": ORDER_LIMIT},
            "stopLossBracket":   {"ticks": sl_signed, "type": ORDER_STOP},
        }
        resp = self._post("/Order/place", body)
        data = resp.json()
        if data.get("success"):
            return data["orderId"]
        log.error("Bracket order failed: %s", data.get("errorMessage"))
        return None

    def search_open_orders(self) -> list:
        resp = self._post("/Order/searchOpen", {"accountId": self.account_id})
        return resp.json().get("orders", [])

    def cancel_order(self, order_id: int):
        self._post("/Order/cancel", {"accountId": self.account_id, "orderId": order_id})

    def cancel_all_open_orders(self):
        for o in self.search_open_orders():
            self.cancel_order(o["id"])

    def get_open_positions(self) -> list:
        resp = self._post("/Position/searchOpen", {"accountId": self.account_id})
        return resp.json().get("positions", [])

    def is_flat(self) -> bool:
        return len(self.get_open_positions()) == 0

    def close_position(self, contract_id: str):
        self._post("/Position/closeContract",
                   {"accountId": self.account_id, "contractId": contract_id})

    def flatten(self):
        """Close any open position on our contract, then cancel leftover orders."""
        for pos in self.get_open_positions():
            self.close_position(pos["contractId"])
        self.cancel_all_open_orders()
        log.warning("Flattened - position closed, open orders cancelled")

    def get_recent_trades(self, minutes_back: int = 30) -> list:
        now_utc   = datetime.now(UTC)
        start_utc = now_utc - timedelta(minutes=minutes_back)
        resp = self._post("/Trade/search", {
            "accountId":      self.account_id,
            "startTimestamp": start_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "endTimestamp":   now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        })
        return resp.json().get("trades", [])

    def get_trades_since(self, start_dt_utc) -> list:
        """Fetch trades from a specific UTC datetime until now. Used to isolate
        the P&L of the CURRENT position only (avoids summing in earlier trades)."""
        now_utc = datetime.now(UTC)
        # back up 5 seconds to avoid edge-of-second cutoff missing the entry fill
        start_utc = start_dt_utc - timedelta(seconds=5)
        resp = self._post("/Trade/search", {
            "accountId":      self.account_id,
            "startTimestamp": start_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "endTimestamp":   now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        })
        return resp.json().get("trades", [])


# ─────────────────────────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────────────────────────
def calculate_ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def calculate_vwap(bars: list) -> float:
    total_pv = total_vol = 0.0
    for bar in bars:
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3
        total_pv  += typical * bar["volume"]
        total_vol += bar["volume"]
    if total_vol == 0:
        return bars[-1]["close"] if bars else 0
    return total_pv / total_vol


def parse_bars(raw_bars: list) -> list:
    """Convert API bars to clean dicts, sorted OLDEST-FIRST (API sends newest-first)."""
    bars = []
    for b in raw_bars:
        bars.append({
            "time":   b.get("t") or b.get("time"),
            "open":   float(b["o"]) if "o" in b else float(b.get("open", 0)),
            "high":   float(b["h"]) if "h" in b else float(b.get("high", 0)),
            "low":    float(b["l"]) if "l" in b else float(b.get("low", 0)),
            "close":  float(b["c"]) if "c" in b else float(b.get("close", 0)),
            "volume": float(b["v"]) if "v" in b else float(b.get("volume", 0)),
        })
    bars.sort(key=lambda x: str(x["time"]))
    return bars


# ─────────────────────────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────────────────────────
class TrendScalpBot:

    def __init__(self):
        self.api = APIClient()
        self.trades_today    = 0
        self.losses_today    = 0
        self.last_trade_day  = None
        self.last_trade_time = None
        # Per-session opening-range state, keyed by session name ("AM"/"PM").
        # Each entry: {"high":, "low":, "trend":, "established": bool}
        self.session_state  = {}
        self.in_trade    = False
        self.entry_side  = None
        self.entry_time  = None
        self.active_session = None   # which session the current open trade belongs to

    def reset_daily(self):
        log.info("New day - resetting daily counters (shared across sessions)")
        self.trades_today = 0
        self.losses_today = 0
        self.last_trade_time = None
        # Per-session OR state resets too
        self.session_state = {}

    def _mins(self, hm):
        """(hour, minute) -> minutes since midnight."""
        return hm[0] * 60 + hm[1]

    def current_session(self, now):
        """Return the session dict we're currently inside, or None."""
        now_m = now.hour * 60 + now.minute
        for s in SESSIONS:
            if self._mins(s["start"]) <= now_m < self._mins(s["end"]):
                return s
        return None

    def session_in_flatten_window(self, now):
        """Return a session whose flatten time has arrived (within a minute)."""
        now_m = now.hour * 60 + now.minute
        for s in SESSIONS:
            if self._mins(s["flatten"]) <= now_m < self._mins(s["flatten"]) + 2:
                return s
        return None

    def past_or_window(self, now, sess):
        now_m = now.hour * 60 + now.minute
        return now_m >= self._mins(sess["or_end"])

    def cooldown_ok(self, now):
        if not self.last_trade_time:
            return True
        return (now - self.last_trade_time).total_seconds() / 60 >= COOLDOWN_MINS

    def check_trade_closed(self) -> Optional[str]:
        """
        If we're in a trade and now flat, read /Trade/search to find the
        realized P&L of the closing trade. Returns 'WIN', 'LOSS', or None.
        """
        if not self.in_trade:
            return None
        if not self.api.is_flat():
            return None

        # Position closed - cancel any leftover bracket leg, then read P&L.
        # Only look at trades since THIS position's entry, so an earlier
        # trade's P&L can't get summed in and flip a win into a loss.
        self.api.cancel_all_open_orders()

        if self.entry_time is not None:
            start_utc = self.entry_time.astimezone(UTC)
            trades = self.api.get_trades_since(start_utc)
        else:
            trades = self.api.get_recent_trades(minutes_back=10)

        realized = 0.0
        found = False
        for t in trades:
            pnl = t.get("profitAndLoss")
            if pnl is not None:   # null = half-turn (entry), skip
                realized += pnl
                found = True

        if not found:
            return "FLAT"
        return "WIN" if realized > 0 else "LOSS"

    def place_trade(self, signal: str):
        side = SIDE_BUY if signal == "LONG" else SIDE_SELL
        order_id = self.api.place_bracket_order(
            side=side, size=1, tp_ticks=PROFIT_TICKS, sl_ticks=STOP_TICKS
        )
        if not order_id:
            log.error("Order rejected - not entering")
            return False

        self.in_trade   = True
        self.entry_side = signal
        self.entry_time = datetime.now(ET)
        self.trades_today   += 1
        self.last_trade_time = datetime.now(ET)
        log.info("%s entered w/ native bracket | TP=%d ticks SL=%d ticks | "
                 "trade#%d losses=%d",
                 signal, PROFIT_TICKS, STOP_TICKS, self.trades_today, self.losses_today)
        return True

    def reset_trade(self):
        self.in_trade   = False
        self.entry_side = None
        self.entry_time = None

    def log_result(self, result: str):
        fp = f"logs/trades_{date.today()}.csv"
        exists = os.path.exists(fp)
        with open(fp, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["time", "side", "result",
                                              "trades_today", "losses_today"])
            if not exists:
                w.writeheader()
            w.writerow({
                "time": datetime.now(ET).strftime("%H:%M:%S"),
                "side": self.entry_side,
                "result": result,
                "trades_today": self.trades_today,
                "losses_today": self.losses_today,
            })

    def run(self):
        log.info("Starting TrendScalp Bot | mode=%s", "LIVE" if USE_LIVE else "SIM")
        if not self.api.authenticate():
            raise SystemExit("Authentication failed")
        if not self.api.get_account():
            raise SystemExit("No active account")
        if not CONTRACT_ID:
            raise SystemExit("CONTRACT_ID not set — run find_contract.py first")

        log.info("Contract: %s | TP=%d ($%.2f)  SL=%d ($%.2f)",
                 CONTRACT_ID,
                 PROFIT_TICKS, PROFIT_TICKS * TICK_VALUE - 4.20,
                 STOP_TICKS, STOP_TICKS * TICK_VALUE + 4.20)
        for s in SESSIONS:
            log.info("Session %s: %02d:%02d start, OR locks %02d:%02d, "
                     "last entry %02d:%02d, flatten %02d:%02d ET",
                     s["name"], s["start"][0], s["start"][1],
                     s["or_end"][0], s["or_end"][1],
                     s["end"][0], s["end"][1],
                     s["flatten"][0], s["flatten"][1])
        log.info("-" * 60)

        while True:
            try:
                now   = datetime.now(ET)
                today = now.date()

                if today != self.last_trade_day:
                    self.reset_daily()
                    self.last_trade_day = today

                # Flatten if any session's flatten time arrived and we're in a trade
                fl_sess = self.session_in_flatten_window(now)
                if fl_sess and self.in_trade:
                    log.warning("Session %s end - flattening open position", fl_sess["name"])
                    self.api.flatten()
                    self.log_result("SESSION_FLAT")
                    self.reset_trade()

                # Manage open trade
                if self.in_trade:
                    result = self.check_trade_closed()
                    if result == "WIN":
                        log.info("WIN")
                        self.log_result("WIN")
                        self.reset_trade()
                    elif result == "LOSS":
                        log.warning("LOSS")
                        self.losses_today += 1
                        self.log_result("LOSS")
                        self.reset_trade()
                    elif result == "FLAT":
                        log.info("Closed (no P&L found)")
                        self.reset_trade()
                    time.sleep(POLL_INTERVAL)
                    continue

                # Which session are we in right now?
                sess = self.current_session(now)
                if sess is None:
                    time.sleep(POLL_INTERVAL)
                    continue

                # Shared daily risk budget across both sessions
                if self.trades_today >= MAX_TRADES_DAY:
                    time.sleep(POLL_INTERVAL); continue
                if self.losses_today >= MAX_LOSSES_DAY:
                    time.sleep(POLL_INTERVAL); continue
                if not self.cooldown_ok(now):
                    time.sleep(POLL_INTERVAL); continue

                raw = self.api.get_bars(CONTRACT_ID, count=120)
                if not raw or len(raw) < EMA_PERIOD + 5:
                    log.warning("Not enough bars - waiting")
                    time.sleep(POLL_INTERVAL); continue

                bars   = parse_bars(raw)
                closes = [b["close"] for b in bars]
                today_bars = [b for b in bars if b.get("time")]

                # Per-session state (its own opening range)
                st = self.session_state.setdefault(
                    sess["name"],
                    {"high": None, "low": None, "trend": None, "established": False},
                )

                # Build THIS session's opening range from bars in its
                # start -> or_end window (ET).
                s_start_m = self._mins(sess["start"])
                s_orend_m = self._mins(sess["or_end"])
                or_bars = []
                for b in today_bars:
                    ts = str(b.get("time", ""))
                    try:
                        bt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        bt_et  = bt_utc.astimezone(ET)
                        if bt_et.date() == now.date():
                            bm = bt_et.hour * 60 + bt_et.minute
                            if s_start_m <= bm < s_orend_m:
                                or_bars.append(b)
                    except Exception:
                        pass

                if not st["established"] and self.past_or_window(now, sess) and or_bars:
                    st["high"] = max(b["high"] for b in or_bars)
                    st["low"]  = min(b["low"]  for b in or_bars)
                    or_mid = (st["high"] + st["low"]) / 2
                    st["trend"] = "LONG" if or_bars[-1]["close"] > or_mid else "SHORT"
                    st["established"] = True
                    log.info("[%s] OR set | high=%.2f low=%.2f trend=%s",
                             sess["name"], st["high"], st["low"], st["trend"])

                if not st["established"]:
                    log.info("[%s] Building OR... (%s ET)", sess["name"], now.strftime("%H:%M"))
                    time.sleep(POLL_INTERVAL); continue

                cur   = bars[-1]
                price = cur["close"]
                ema   = calculate_ema(closes, EMA_PERIOD)
                vwap  = calculate_vwap(today_bars) if today_bars else price

                long_sig = (st["trend"] == "LONG" and price > vwap
                            and cur["low"] <= ema and cur["close"] > ema
                            and cur["close"] > cur["open"])
                short_sig = (st["trend"] == "SHORT" and price < vwap
                             and cur["high"] >= ema and cur["close"] < ema
                             and cur["close"] < cur["open"])

                log.info("[%s] price=%.2f ema=%.2f vwap=%.2f trend=%s L=%s S=%s",
                         sess["name"], price, ema, vwap, st["trend"],
                         "✅" if long_sig else "no", "✅" if short_sig else "no")

                if long_sig:
                    self.active_session = sess["name"]
                    self.place_trade("LONG")
                elif short_sig:
                    self.active_session = sess["name"]
                    self.place_trade("SHORT")

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                log.info("Stopped by user")
                if self.in_trade:
                    log.warning("Flattening before exit...")
                    self.api.flatten()
                break
            except Exception as e:
                log.exception("❌ Loop error: %s", e)
                time.sleep(30)


if __name__ == "__main__":
    TrendScalpBot().run()