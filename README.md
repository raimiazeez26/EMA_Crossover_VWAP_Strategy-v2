# EMA Crossover + VWAP Trading System (Backtest + OANDA Live)

This repository contains:

- A **research/backtesting pipeline** (primarily MT5-based) for optimizing EMA/VWAP strategy parameters.
- A production-oriented **OANDA live trading bot** with Telegram notifications, state persistence, EOD reporting, and weekend handling.

---

## 1) Strategy Summary

Core directional logic combines:

- EMA fast / mid / slow structure
- VWAP alignment
- RSI and ROC filters
- ATR + swing points for stop placement
- Risk-based position sizing

The strategy is evaluated in backtests and then deployed via OANDA for live execution.

## Strategy definition (entry/exit logic, indicators, requirements)

### Indicators used

- **EMA8, EMA13, EMA21**: trend stack and alignment filters on both trend and entry timeframes.
- **VWAP**: directional confirmation (price location vs VWAP).
- **RSI(period)**: momentum filter to avoid overextended entries.
- **ROC(period)**: directional momentum confirmation.
- **ATR(period)**: volatility-aware stop buffer and order sanity fallback.
- **Swing High/Low(lookback)**: structural breakout trigger and stop placement anchors.

### Timeframes

- **Trend timeframe** (`trend_timeframe`, default H1): defines higher-timeframe direction.
- **Entry timeframe** (`entry_timeframe`, default M5): evaluates trigger conditions and prices.

### Entry conditions

#### BUY setup

All conditions must be true:

1. Trend timeframe EMA alignment is bullish: `EMA21 < EMA13 < EMA8`.
2. Entry timeframe EMA alignment is bullish: `EMA21 < EMA13 < EMA8`.
3. Entry timeframe close is above VWAP.
4. Entry timeframe ROC is positive (`ROC > 0`).
5. Entry timeframe RSI is below overbought threshold (`RSI < rsi_overbought`).

Order placement and prices:

- Pending **STOP** order.
- `entry_price = swing_high + min_entry_distance`.
- `stop_loss = swing_low - ATR * atr_multiplier`.
- `take_profit = entry_price + (entry_price - stop_loss) * rr_ratio`.

#### SELL setup

All conditions must be true:

1. Trend timeframe EMA alignment is bearish: `EMA21 > EMA13 > EMA8`.
2. Entry timeframe EMA alignment is bearish: `EMA21 > EMA13 > EMA8`.
3. Entry timeframe close is below VWAP.
4. Entry timeframe ROC is negative (`ROC < 0`).
5. Entry timeframe RSI is above oversold threshold (`RSI > rsi_oversold`).

Order placement and prices:

- Pending **STOP** order.
- `entry_price = swing_low - min_entry_distance`.
- `stop_loss = swing_high + ATR * atr_multiplier`.
- `take_profit = entry_price - (stop_loss - entry_price) * rr_ratio`.

### Exit conditions

- Primary exits are broker-managed via attached **Stop Loss** and **Take Profit** on order fill.
- Pending orders may be canceled if live price moves **50% against planned entry risk distance** before trigger.
- Position sizing is risk-based from account balance and stop distance (`risk_percent`).

### Strategy requirements / constraints

- One active strategy position/order flow per symbol (bot skips new order when strategy trade/order already exists).
- Symbol must be supported FX pair or configured metals mapping for OANDA.
- Valid market data on both timeframes is required; otherwise signal is `NEUTRAL`.
- Parameter source should be refreshed from backtests (`backtest_outputs/symbol_params_latest.json`).

---

## 2) Repository Structure

- `mt5_backtest.py` – historical data fetch + indicator calculation + backtest execution and optimization workflow.
- `oanda_trading_fixed.py` – OANDA live bot (signal generation, order placement, trade/order sync, EOD reporting, Telegram).
- `backtest_outputs/` – generated optimization artifacts (`symbol_params_latest.json`, result CSV/XLSX files).
- `requirements.txt` – Python dependencies.
- `Procfile` – process start command for deployment environments like Heroku.

---

## 3) Setup

## Python

Use Python 3.10+ (3.11/3.12 recommended).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> Notes:
> - `talib` must be available for both backtest and live bot.
> - MT5 backtests require a running MetaTrader 5 terminal with symbols enabled.

---

## 4) Backtesting Guide (`mt5_backtest.py`)

## What it does

- Connects to MT5
- Pulls iterative historical bars (with caching support)
- Computes indicators (EMA/VWAP/RSI/ROC/ATR)
- Evaluates trades and risk/reward outcomes
- Produces optimization and symbol parameter output files

## Typical usage

Run with your preferred CLI arguments (see script help):

```bash
python mt5_backtest.py --help
```

Then run a full optimization/backtest pass and confirm new files in `backtest_outputs/`.

## Backtest outputs used by live bot

The live bot reads:

- `backtest_outputs/symbol_params_latest.json`

This file should contain selected assets and parameter sets (`assets` list with positive `net_r` expected by the live loader).

## Backtest quality checks (recommended)

Before promoting params to live:

- Verify symbol history completeness warnings are minimal.
- Ensure optimized params are not based on very small sample sizes.
- Check spread-sensitive symbols and session behavior.
- Confirm final selected assets are tradable on OANDA (FX + supported metals).

---

## 5) OANDA Live Trading Guide (`oanda_trading_fixed.py`)

## Environment variables

Required:

- `OANDA_TOKEN`
- `OANDA_ACCOUNT_ID`

Optional / recommended:

- `OANDA_API_BASE_URL` (default: practice endpoint)
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_STATE_PATH` (default: `backtest_outputs/telegram_state.json`)
- `HEROKU_KEEPALIVE_URL`

Example:

```bash
export OANDA_TOKEN="..."
export OANDA_ACCOUNT_ID="..."
export OANDA_API_BASE_URL="https://api-fxpractice.oanda.com"
export TELEGRAM_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

## Start live bot

```bash
python oanda_trading_fixed.py
```

At startup, the bot:

- Loads latest symbol params.
- Syncs open positions and tracked IDs.
- Enters continuous cycle for signal generation/execution and status sync.

## Live safety behavior

- Tracks known/pending order IDs.
- Cancels pending orders if price moves 50% against planned entry risk.
- Syncs closed trades and sends Telegram exit updates.
- Persists notification/tracking state to survive restarts.

---

## 6) Weekend Sleep / Monday Auto-Resume (Fixed)

The weekend logic now computes an exact next resume time and sleeps until market-week start rather than using coarse repeated 6-hour sleeps.

Result:

- Cleaner weekend pause behavior.
- Automatic resume right at the next Monday UTC boundary.
- No drift caused by repeated long sleep windows.

---

## 7) EOD PnL Calculation (Closed Trades Fix)

EOD PnL now derives from **transaction-level closed trade events** (`ORDER_FILL` with `tradeClosed`/`tradesClosed`/`tradeReduced`) for the target day.

This improves correctness by:

- Capturing realized PnL from actual close events.
- Including financing/commission-related delta from transaction fields.
- Avoiding reliance on potentially incomplete “recent closed trades” snapshots.

Daily Telegram output includes:

- EOD PnL amount
- EOD PnL percent
- Closed trade count
- Start and end balances for the day estimate

Friday additionally emits weekly summary message.

---

## 8) Deployment Notes (OANDA-focused)

- Run one bot instance only (avoid duplicate orders).
- Persist `TELEGRAM_STATE_PATH` on durable storage where possible.
- Use OANDA practice environment for burn-in.
- Move to live account only after validating fills, sizing, and notifications.

For Heroku-like platforms:

- Ensure worker dyno does not terminate unexpectedly.
- Use `Procfile` to run the bot process.
- Keep env vars configured in platform secrets.

---

## 9) Operational Checklist

Before market open:

- `symbol_params_latest.json` refreshed from latest backtests.
- OANDA credentials valid.
- Telegram notifications working.
- Bot logs clean at startup.

After deployment/restart:

- Confirm startup sync succeeded.
- Confirm per-symbol processing logs appear.
- Confirm weekend pause/resume logs are correct.
- Confirm next EOD report is delivered.

---

## 10) Troubleshooting

- **No symbols loaded**: verify `backtest_outputs/symbol_params_latest.json` exists and contains eligible assets.
- **No Telegram messages**: check token/chat ID and network egress.
- **No trades placed**: inspect signal conditions and instrument metadata retrieval.
- **EOD mismatch concerns**: inspect transaction history around day boundary and confirm closed events fall within UTC day.

---

## 11) Recommended Improvement Path

- Add unit tests for transaction parsing and EOD computation edge cases.
- Add explicit market-hours calendar for OANDA instruments.
- Add structured metrics output (CSV/DB) for external monitoring dashboards.
