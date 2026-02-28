# Developer README (Working Files Map)

This document is a developer-oriented index of **working files** in this repository, grouped by purpose.

---

## 1) Root-level operational files

- `README.md`  
  Main operator-facing guide for strategy behavior, setup, backtesting, and OANDA live execution.
- `DEV_README.md`  
  This developer map of active/working files.
- `Procfile`  
  Deployment process definitions:
  - web dashboard: `gunicorn live_trading.oanda_dashboard:server`
  - worker bot: `python live_trading/oanda_trading_fixed.py`
- `requirements.txt`  
  Python dependency list.
- `trading_settings.json`  
  Runtime settings consumed by legacy/helper components.
- `temp.py`  
  Temporary utility script (not part of the main production workflow).
- `mt5_trading.log`  
  Runtime log output file.

---

## 2) Backtest package (`backtest/`)

Primary research and optimization area.

### Core scripts

- `backtest/mt5_backtest.py`  
  Main MT5 backtesting engine:
  - MT5 initialization/shutdown
  - historical rate fetching
  - indicator calculation
  - strategy simulation and result aggregation
- `backtest/mt5_optimize.py`  
  Optimization wrapper around `mt5_backtest.py` for parameter sweeps and best-parameter selection.

### Research notebooks

- `backtest/Backtest.ipynb`  
  Notebook-based experimentation and exploratory analysis.
- `backtest/Untitled.ipynb`  
  Additional notebook scratchpad.

### Package marker

- `backtest/__init__.py`  
  Marks the folder as a Python package.

---

## 3) Live trading package (`live_trading/`)

Production/live execution area (OANDA-focused plus legacy support modules).

### OANDA production path (primary)

- `live_trading/oanda_trading_fixed.py`  
  Primary live OANDA bot:
  - signal generation
  - order placement
  - order/trade synchronization
  - EOD PnL reporting
  - weekend pause/resume logic
  - Telegram notifications
- `live_trading/oanda_dashboard.py`  
  Monitoring/dashboard app served by `gunicorn`.
- `live_trading/oanda_order_test.py`  
  OANDA order placement/testing helper script.

### Supporting/legacy live modules

- `live_trading/trading_engine.py`  
  Shared trading engine logic (legacy Alpaca-oriented components are referenced by some scripts).
- `live_trading/trading_ui.py`  
  UI layer for trading interactions.
- `live_trading/test_telegram.py`  
  Telegram notification test helper.
- `live_trading/trading_mt5.py`  
  MT5-related trading runtime script.
- `live_trading/trader_mt5.py`  
  MT5 trader helper/runtime script.
- `live_trading/mt5_advanced_bot_dynamic_pip.py`  
  Advanced MT5 bot variant.
- `live_trading/mt5_bot_advanced_dynamic_updated.py`  
  Updated advanced MT5 bot variant.

### Package marker

- `live_trading/__init__.py`  
  Marks the folder as a Python package.

---

## 4) Data/artifact folder (`backtest_outputs/`)

Generated outputs from backtests and optimization runs.

- `backtest_outputs/symbol_params_latest.json`  
  Latest selected symbol parameters consumed by live OANDA bot.
- `backtest_outputs/symbol_params_*.json`  
  Timestamped parameter snapshots.
- `backtest_outputs/optimization_results*.csv`  
  Optimization result tables.
- `backtest_outputs/best_assets_M5_H4.csv`  
  Selected best asset view.
- `backtest_outputs/backtest_results.xlsx`  
  Spreadsheet output for review.

---

## 5) Binary/reference files

- `TA_Lib-0.4.24-cp310-cp310-win_amd64.whl`
- `TA_Lib-0.4.28-cp312-cp312-win_amd64.whl`
- `Forex_Trading_Project_Description.docx`

These are reference/install artifacts and are not typical runtime Python sources.

---

## 6) Recommended developer workflow

1. Run/validate backtests in `backtest/` and update `backtest_outputs/symbol_params_latest.json`.
2. Validate live configuration (`OANDA_TOKEN`, `OANDA_ACCOUNT_ID`, Telegram env vars).
3. Start live bot via:
   - `python live_trading/oanda_trading_fixed.py`
4. For deployment, use `Procfile` web+worker processes.

