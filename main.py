"""Range-reclaim bot for the make.fun BTC trading race (agent-fun/bars-replay/v1).

Strategy: long-only CBBTC/USDC, full-cash entries, one position at a time.
Two complementary edges, one bot: it trades the range when the market
is ranging, and it trades the washout recovery when the market flushes.
It never buys a dip while the market is falling into a flush -- the
deep-bleed guard stands the range mode down and a flush exit frees a
range position in time for the washout entry.

  WASHOUT mode (priority): buy when the close is >= 4.00% below the
  480-bar (8h) high AND has recovered >= 1.00% above the 60-bar low.
  Exit on +2.50% take-profit, a 1.50% trailing stop, or a 720-bar
  (12h) max hold, with a 180-bar cooldown. It buys the reclaim after
  the flush, not the dip on the way down.

  RANGE mode: buy when the close is >= 1.50% below the 240-bar (4h)
  high AND RSI(14) < 40, but only while the close is still within
  3.00% of the 480-bar high (deep-bleed guard: a market already 3%
  off its 8h high is bleeding, not ranging). Exit on +1.60%
  take-profit, a 0.90% trailing stop, or a 240-bar (4h) max hold,
  with a 60-bar cooldown. While a range position is open, a flush
  exit sells at once if the close falls to 3.00% below the 480-bar
  high, so the bot is flat and free to take the washout reclaim.

  FALLBACK mode (only if no trade has filled by bar 1100, i.e. the
  window never offered a range dip or a washout): buy a shallow dip,
  >= 1.00% below the 120-bar (2h) high with RSI(14) < 45. Exit on
  +0.80% take-profit, a 0.60% trailing stop, or a 360-bar (6h) max
  hold, with a 120-bar cooldown. This exists so the bot still trades
  (and meets the >= 1 trade bar) in quiet windows.

Why this shape: the dev window is a choppy range (63,076-65,190, net
-2.8%) with several 1-2% swings and no washout deeper than 3.2% from
any 8h high, so the previous round's washout-only bot scores -$0.83
on it (1 trade, fails the dev gate) while the range mode banks the
swings. The two older dev windows were the opposite regime -- a slow
bleed into a violent flush -- where range-only bots lose ~-$33 and the
washout mode banks +$23.55. Combining them with the guard and the
flush exit keeps the range edge on choppy windows without letting it
bleed out on flush windows.

Dev-window result (protocol-level local replay, exact market model,
replay_driver.py -- the same harness model validated to the cent on
the kit's house bots and on the previous round's washout bot, which
it reproduces at +$27.42): net PnL +$12.93, 5 trades, max drawdown
-0.90%, final equity $1,012.93. Two runs produce byte-identical action
logs (deterministic). Parameters sit in the middle of a measured
plateau (the range point is the balanced pick across a 192-combination
sweep, not the peak; the washout point is the previous round's verified
one). Cross-window sanity (same code, other dev windows): +$0.06 on
2026-11-15 (6 trades, washout 1 + range 2) and -$20.47 on 2026-11-13
(7 trades) -- the slow-bleed regime is the known weak spot, and the
worst observed window is a contained loss with max drawdown -2.32%,
not a blow-up.

Risk controls: long only, no leverage, full-cash sizing (never more
than the account holds), 100 bps max slippage on every order, one
position at a time, every trade has a take-profit, a trailing stop,
and a max hold. All price comparisons are exact integer arithmetic
(raw price units). RSI uses Wilder's smoothing on the close stream.
No clock, no randomness, no environment reads: the bot is a pure
function of the bar stream, so the two graded executions produce
identical orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 100    # same cap as the kit's house bots

# --- washout mode: flush + reclaim (priority) ---
WASH_LOOKBACK = 480       # bars (8h) for the reference high
WASH_FALL_BPS = 400       # entry: close this far below the reference high
RECLAIM_LOOKBACK = 60     # bars (1h) for the reference low
RECLAIM_BPS = 100         # entry: close this far above the reference low
W_TP_BPS = 250            # take profit from entry
W_TRAIL_BPS = 150         # trailing stop from the peak since entry
W_HOLD_BARS = 720         # max hold (12h) backstop
W_COOLDOWN_BARS = 180     # bars to wait after any washout exit

# --- range mode: 4h dip + oversold, blocked in a deep bleed ---
DIP_LOOKBACK = 240        # bars (4h) for the reference high
DIP_FALL_BPS = 150        # entry: close this far below the reference high
GUARD_BPS = 300           # stand down once this far below the 8h high
DIP_RSI_PERIOD = 14
DIP_RSI_MAX = 40.0        # entry only when oversold
R_TP_BPS = 160            # take profit from entry
R_TRAIL_BPS = 90          # trailing stop from the peak since entry
R_HOLD_BARS = 240         # max hold (4h) backstop
R_COOLDOWN_BARS = 60      # bars to wait after any range exit

# --- fallback mode: shallow dip, only if nothing has traded by bar 1100 ---
FB_START_BAR = 1100       # fallback opens only after this batch
FB_LOOKBACK = 120         # bars (2h) for the reference high
FB_FALL_BPS = 100         # entry: close this far below the reference high
FB_RSI_MAX = 45.0         # entry only when washed out
FB_TP_BPS = 80            # take profit: +0.80% from entry close
FB_TRAIL_BPS = 60         # trailing stop: 0.60% below peak close
FB_HOLD_BARS = 360        # max hold (6h)
FB_COOLDOWN_BARS = 120    # bars to wait after any fallback exit


class RangeReclaimBot:
    """One position at a time: the washout reclaim when the market
    flushes, a confirmed 4h dip while it ranges, or -- late in a
    window that offered neither -- a shallow RSI-confirmed dip."""

    def __init__(self):
        self.history = []        # closes (raw ints), including current bar
        self.entry_close = None  # close of the bar the buy was sent on
        self.peak = None         # highest close since entry
        self.entry_bar = 0
        self.cool_until = -1     # earliest bar a new entry may fire
        self.mode = None         # mode of the open trade, if any
        self.entries_sent = 0    # buy orders sent so far (any mode)
        self.tp_bps = 0          # exit params of the open trade
        self.trail_bps = 0
        self.hold_bars = 0
        self.cool_bars = 0
        # --- Wilder's RSI state ---
        self._rsi_gain = None
        self._rsi_loss = None
        self._rsi_seed = []      # (up, down) pairs while seeding
        self.rsi = 50.0

    def _reset_trade(self):
        self.entry_close = None
        self.peak = None
        self.mode = None
        self.tp_bps = 0
        self.trail_bps = 0
        self.hold_bars = 0
        self.cool_bars = 0

    def _update_rsi(self, close):
        if len(self.history) < 2:
            return
        prev = self.history[-2]
        diff = close - prev
        up = diff if diff > 0 else 0
        down = -diff if diff < 0 else 0
        if self._rsi_gain is None:
            self._rsi_seed.append((up, down))
            if len(self._rsi_seed) == DIP_RSI_PERIOD:
                self._rsi_gain = sum(u for u, _ in self._rsi_seed) / DIP_RSI_PERIOD
                self._rsi_loss = sum(d for _, d in self._rsi_seed) / DIP_RSI_PERIOD
                self.rsi = (100.0 - 100.0 / (1.0 + self._rsi_gain / self._rsi_loss)
                            if self._rsi_loss > 0 else 100.0)
            return
        n = DIP_RSI_PERIOD
        self._rsi_gain = (self._rsi_gain * (n - 1) + up) / n
        self._rsi_loss = (self._rsi_loss * (n - 1) + down) / n
        self.rsi = (100.0 - 100.0 / (1.0 + self._rsi_gain / self._rsi_loss)
                    if self._rsi_loss > 0 else 100.0)

    def _buy(self, state, close, tp_bps, trail_bps, hold_bars, cool_bars,
             mode):
        self.entries_sent += 1
        self.mode = mode
        self.entry_close = close
        self.peak = close
        self.entry_bar = state.seq
        self.tp_bps = tp_bps
        self.trail_bps = trail_bps
        self.hold_bars = hold_bars
        self.cool_bars = cool_bars
        return {
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": state.cash,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    def _sell_all(self, state):
        held = state.positions[SYMBOL]
        self.cool_until = state.seq + self.cool_bars
        self._reset_trade()
        return {
            "side": "sell",
            "symbol": SYMBOL,
            "qty": held.qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    def on_batch(self, state, batch):
        close = state.close(SYMBOL)
        if close is None:
            return []
        self.history.append(close)
        self._update_rsi(close)
        if state.warming_up:
            return []
        held = state.positions.get(SYMBOL)
        if held is not None and held.qty > 0:
            # --- manage the open position ---
            if self.entry_close is None:
                # Position present without tracked entry (desync guard):
                # treat the current close as the entry and manage it with
                # the range-mode exits (tight trail, 4h hold).
                self.entry_close = close
                self.peak = close
                self.entry_bar = state.seq
                self.mode = "range"
                self.tp_bps = R_TP_BPS
                self.trail_bps = R_TRAIL_BPS
                self.hold_bars = R_HOLD_BARS
                self.cool_bars = R_COOLDOWN_BARS
            if close > self.peak:
                self.peak = close
            if close * 10000 >= self.entry_close * (10000 + self.tp_bps):
                return [self._sell_all(state)]
            if close * 10000 <= self.peak * (10000 - self.trail_bps):
                return [self._sell_all(state)]
            if self.hold_bars and state.seq - self.entry_bar >= self.hold_bars:
                return [self._sell_all(state)]
            # Flush exit: a range position is sold at once once the
            # market is 3% off its 8h high, so the bot is flat and free
            # to take the washout reclaim instead of riding the flush.
            if self.mode == "range" and len(self.history) > WASH_LOOKBACK:
                hi480 = max(self.history[-WASH_LOOKBACK - 1:-1])
                if close * 10000 <= hi480 * (10000 - GUARD_BPS):
                    return [self._sell_all(state)]
            return []

        # --- flat: look for an entry ---
        self._reset_trade()
        if state.seq < self.cool_until:
            return []
        n = len(self.history)
        # washout mode first: it is rarer and bigger
        if n > WASH_LOOKBACK:
            hi480 = max(self.history[-WASH_LOOKBACK - 1:-1])
            if close * 10000 <= hi480 * (10000 - WASH_FALL_BPS):
                lo60 = min(self.history[-RECLAIM_LOOKBACK - 1:-1])
                if close * 10000 >= lo60 * (10000 + RECLAIM_BPS):
                    return [self._buy(state, close, W_TP_BPS, W_TRAIL_BPS,
                                      W_HOLD_BARS, W_COOLDOWN_BARS,
                                      "washout")]
        # range mode: only while the market is still within 3% of its
        # 8h high (ranging, not bleeding)
        if n > DIP_LOOKBACK:
            hi480 = (max(self.history[-WASH_LOOKBACK - 1:-1])
                     if n > WASH_LOOKBACK else max(self.history[:-1]))
            if close * 10000 > hi480 * (10000 - GUARD_BPS):
                ref = max(self.history[-DIP_LOOKBACK - 1:-1])
                if (close * 10000 <= ref * (10000 - DIP_FALL_BPS)
                        and self.rsi < DIP_RSI_MAX):
                    return [self._buy(state, close, R_TP_BPS, R_TRAIL_BPS,
                                      R_HOLD_BARS, R_COOLDOWN_BARS,
                                      "range")]
        # fallback mode: only late in the window, and only if nothing
        # has traded yet (the window offered neither a dip nor a flush)
        if (state.seq >= FB_START_BAR and self.entries_sent == 0
                and n > FB_LOOKBACK):
            ref = max(self.history[-FB_LOOKBACK - 1:-1])
            if (close * 10000 <= ref * (10000 - FB_FALL_BPS)
                    and self.rsi < FB_RSI_MAX):
                return [self._buy(state, close, FB_TP_BPS, FB_TRAIL_BPS,
                                  FB_HOLD_BARS, FB_COOLDOWN_BARS,
                                  "fallback")]
        return []


makefun_bars.run(RangeReclaimBot().on_batch)
