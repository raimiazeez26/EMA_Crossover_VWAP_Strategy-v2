import argparse
import datetime as dt
import logging
import os
import time
from dataclasses import dataclass

import MetaTrader5 as mt5
import numpy as np
import pandas as pd
import talib


DEFAULT_SYMBOLS = [
    "XAUUSD", "XAGUSD", "EURUSD","GBPUSD", "USDJPY",
    "USDCAD", "NZDUSD", "EURGBP", "EURJPY", "EURCHF",
    "EURAUD", "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD",
    "GBPNZD", "AUDJPY", "AUDNZD", "AUDCAD", "AUDCHF",
    "CADJPY", "CHFJPY", "NZDJPY", "NZDCAD", "NZDCHF",
    "EURCAD", "USDCHF", "AUDUSD", "US30", "US500",
    "JP225", "NDAQ.NAS"
]


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOGGER = logging.getLogger(__name__)
SLOW_FETCH_THRESHOLD_SECONDS = 5.0


@dataclass(frozen=True)
class StrategyParams:
    ema_fast: int = 8
    ema_mid: int = 13
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_overbought: float = 70
    rsi_oversold: float = 30
    roc_period: int = 3
    atr_period: int = 5
    swing_lookback: int = 21
    atr_multiplier: float = 2.0
    rr_ratio: float = 2.0
    pip_buffer: int = 3
    entry_timeframe: int = mt5.TIMEFRAME_M5
    trend_timeframe: int = mt5.TIMEFRAME_H1


@dataclass
class MT5DataCache:
    rates: dict[tuple[str, int, str, str], tuple[pd.DataFrame, bool]]

    def __init__(self) -> None:
        self.rates = {}

    def get(self, symbol: str, timeframe: int, start: dt.datetime, end: dt.datetime) -> tuple[pd.DataFrame, bool] | None:
        key = (symbol, timeframe, start.isoformat(), end.isoformat())
        return self.rates.get(key)

    def set(
        self,
        symbol: str,
        timeframe: int,
        start: dt.datetime,
        end: dt.datetime,
        value: tuple[pd.DataFrame, bool],
    ) -> None:
        key = (symbol, timeframe, start.isoformat(), end.isoformat())
        self.rates[key] = value


def initialize_mt5() -> None:
    if not mt5.initialize():
        raise ConnectionError("Failed to connect to MT5")


def shutdown_mt5() -> None:
    mt5.shutdown()


def get_pip_value(symbol: str) -> float:
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return 0.0001
    return 10 * symbol_info.point if symbol_info.digits == 5 else symbol_info.point


def timeframe_seconds(timeframe: int) -> int:
    mapping = {
        mt5.TIMEFRAME_M1: 60,
        mt5.TIMEFRAME_M2: 120,
        mt5.TIMEFRAME_M3: 180,
        mt5.TIMEFRAME_M4: 240,
        mt5.TIMEFRAME_M5: 300,
        mt5.TIMEFRAME_M6: 360,
        mt5.TIMEFRAME_M10: 600,
        mt5.TIMEFRAME_M12: 720,
        mt5.TIMEFRAME_M15: 900,
        mt5.TIMEFRAME_M20: 1200,
        mt5.TIMEFRAME_M30: 1800,
        mt5.TIMEFRAME_H1: 3600,
        mt5.TIMEFRAME_H2: 7200,
        mt5.TIMEFRAME_H3: 10800,
        mt5.TIMEFRAME_H4: 14400,
        mt5.TIMEFRAME_H6: 21600,
        mt5.TIMEFRAME_H8: 28800,
        mt5.TIMEFRAME_H12: 43200,
        mt5.TIMEFRAME_D1: 86400,
        mt5.TIMEFRAME_W1: 604800,
        mt5.TIMEFRAME_MN1: 2592000,
    }
    return mapping.get(timeframe, 60)


def infer_max_bars(symbol: str, timeframe: int, end: dt.datetime, probe: int = 100000) -> int:
    rates = mt5.copy_rates_from(symbol, timeframe, end, probe)
    if rates is None or len(rates) == 0:
        return 0
    return len(rates)


def ensure_symbol(symbol: str) -> bool:
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        logging.warning("Symbol %s not found in MT5 terminal.", symbol)
        return False
    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            logging.warning("Failed to select symbol %s for MT5 history.", symbol)
            return False
    return True


def fetch_rates(symbol: str, timeframe: int, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    rates, _ = fetch_rates_iterative(symbol, timeframe, start, end)
    return rates


def fetch_rates_iterative(
    symbol: str,
    timeframe: int,
    start: dt.datetime,
    end: dt.datetime,
    data_cache: MT5DataCache | None = None,
) -> tuple[pd.DataFrame, bool]:
    cached = data_cache.get(symbol, timeframe, start, end) if data_cache else None
    print(f"Cached Data Loaded for: {symbol} {timeframe}")

    if cached is not None:
        return cached
    if not ensure_symbol(symbol):
        return pd.DataFrame(), False

    start_time = time.perf_counter()
    inferred_max = infer_max_bars(symbol, timeframe, end)
    if inferred_max == 0:
        logging.warning("No rates returned for %s in timeframe %s.", symbol, timeframe)
        return pd.DataFrame(), False

    end_cursor = end
    frames = []
    iteration = 0
    previous_cursor = None
    while end_cursor > start:
        iteration += 1
        loop_start = time.perf_counter()
        rates = mt5.copy_rates_from(symbol, timeframe, end_cursor, inferred_max)
        loop_elapsed = time.perf_counter() - loop_start
        if loop_elapsed > SLOW_FETCH_THRESHOLD_SECONDS:
            LOGGER.debug(
                "Slow MT5 fetch for %s timeframe %s (iteration %s): %.2fs",
                symbol,
                timeframe,
                iteration,
                loop_elapsed,
            )
        if rates is None or len(rates) == 0:
            break
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        frames.append(df)
        oldest = df.index.min()
        if previous_cursor is not None and oldest >= previous_cursor:
            LOGGER.warning(
                "MT5 fetch stalled for %s timeframe %s at %s (iteration %s).",
                symbol,
                timeframe,
                oldest,
                iteration,
            )
            break
        previous_cursor = oldest
        end_cursor = oldest - dt.timedelta(seconds=1)
        if iteration % 10 == 0:
            LOGGER.info(
                "Progress %s timeframe %s: %s bars, oldest %s",
                symbol,
                timeframe,
                len(df),
                oldest,
            )

    if not frames:
        return pd.DataFrame(), False

    combined = pd.concat(frames).sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]
    combined = combined.loc[(combined.index >= start) & (combined.index <= end)]
    elapsed = time.perf_counter() - start_time
    LOGGER.info(
        "Fetched %s bars for %s timeframe %s in %.2fs",
        len(combined),
        symbol,
        timeframe,
        elapsed,
    )
    if elapsed > SLOW_FETCH_THRESHOLD_SECONDS:
        LOGGER.warning(
            "Slow history fetch for %s timeframe %s: %.2fs",
            symbol,
            timeframe,
            elapsed,
        )

    bar_seconds = timeframe_seconds(timeframe)
    if combined.empty:
        return combined, False

    expected_start = start + dt.timedelta(seconds=bar_seconds)
    expected_end = end - dt.timedelta(seconds=bar_seconds)
    is_complete = combined.index.min() <= expected_start and combined.index.max() >= expected_end
    if not is_complete:
        logging.warning(
            "Incomplete history for %s timeframe %s. Range %s to %s returned %s to %s.",
            symbol,
            timeframe,
            start,
            end,
            combined.index.min(),
            combined.index.max(),
        )
    result = (combined, is_complete)
    if data_cache is not None:
        data_cache.set(symbol, timeframe, start, end, result)
    return result


def calculate_indicators(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    df = df.copy()
    df["EMA_FAST"] = talib.EMA(df["close"], params.ema_fast)
    df["EMA_MID"] = talib.EMA(df["close"], params.ema_mid)
    df["EMA_SLOW"] = talib.EMA(df["close"], params.ema_slow)

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_tp = (typical_price * df["tick_volume"]).cumsum()
    cumulative_volume = df["tick_volume"].cumsum()
    df["VWAP"] = cumulative_tp / cumulative_volume

    df["RSI"] = talib.RSI(df["close"], params.rsi_period)
    df["ROC"] = talib.ROC(df["close"], params.roc_period)
    df["ATR"] = talib.ATR(df["high"], df["low"], df["close"], timeperiod=params.atr_period)

    df["swing_high"] = df["high"].rolling(params.swing_lookback).max()
    df["swing_low"] = df["low"].rolling(params.swing_lookback).min()
    return df.dropna()


def build_signal_frame(m5_df: pd.DataFrame, h1_df: pd.DataFrame) -> pd.DataFrame:
    h1_df = h1_df.rename(columns={
        "EMA_FAST": "EMA_FAST_H1",
        "EMA_MID": "EMA_MID_H1",
        "EMA_SLOW": "EMA_SLOW_H1",
    })
    merged = pd.merge_asof(
        m5_df.sort_index(),
        h1_df[["EMA_FAST_H1", "EMA_MID_H1", "EMA_SLOW_H1"]].sort_index(),
        left_index=True,
        right_index=True,
        direction="backward",
    )
    return merged.dropna()


def generate_signal(row: pd.Series, params: StrategyParams) -> str:
    bearish_trend = row["EMA_SLOW_H1"] > row["EMA_MID_H1"] > row["EMA_FAST_H1"]
    bullish_trend = row["EMA_SLOW_H1"] < row["EMA_MID_H1"] < row["EMA_FAST_H1"]

    bearish_trend_m5 = row["EMA_SLOW"] > row["EMA_MID"] > row["EMA_FAST"]
    bullish_trend_m5 = row["EMA_SLOW"] < row["EMA_MID"] < row["EMA_FAST"]

    if (
        bullish_trend
        and bullish_trend_m5
        and row["close"] > row["VWAP"]
        and row["ROC"] > 0
        and row["RSI"] < params.rsi_overbought
    ):
        return "BUY"
    if (
        bearish_trend
        and bearish_trend_m5
        and row["close"] < row["VWAP"]
        and row["ROC"] < 0
        and row["RSI"] > params.rsi_oversold
    ):
        return "SELL"
    return "NEUTRAL"


def apply_trailing_stop(direction: str, entry: float, initial_sl: float, current_sl: float, close: float) -> float:
    initial_risk = abs(entry - initial_sl)
    if initial_risk == 0:
        return current_sl

    if direction == "BUY":
        profit_distance = close - entry
    else:
        profit_distance = entry - close

    rr_level = profit_distance / initial_risk
    if rr_level < 1.0:
        return current_sl

    new_sl = current_sl
    trailing_levels = [1.0 + 0.5 * n for n in range(1, 18)]
    for level in trailing_levels:
        if rr_level >= level:
            steps = (level - 1.0) / 0.5
            candidate_sl = entry + (initial_risk * 0.5 * steps) if direction == "BUY" \
                else entry - (initial_risk * 0.5 * steps)
            if direction == "BUY":
                new_sl = max(new_sl, candidate_sl)
            else:
                new_sl = min(new_sl, candidate_sl)
    return new_sl


def backtest_symbol(
    symbol: str,
    years: int,
    params: StrategyParams,
    require_complete: bool = True,
    data_cache: MT5DataCache | None = None,
    end_time: dt.datetime | None = None,
) -> dict:
    symbol_start = time.perf_counter()
    end = end_time or dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=365 * years)

    entry_raw, entry_complete = fetch_rates_iterative(
        symbol,
        params.entry_timeframe,
        start,
        end,
        data_cache=data_cache,
    )
    trend_raw, trend_complete = fetch_rates_iterative(
        symbol,
        params.trend_timeframe,
        start,
        end,
        data_cache=data_cache,
    )
    if entry_raw.empty or trend_raw.empty:
        return {"symbol": symbol, "trades": [], "metrics": {"total_trades": 0}}
    if require_complete and (not entry_complete or not trend_complete):
        return {"symbol": symbol, "trades": [], "metrics": {"total_trades": 0}}

    entry_df = calculate_indicators(entry_raw, params)
    trend_df = calculate_indicators(trend_raw, params)
    if entry_df.empty or trend_df.empty:
        return {"symbol": symbol, "trades": [], "metrics": {"total_trades": 0}}

    merged = build_signal_frame(entry_df, trend_df)
    if merged.empty:
        return {"symbol": symbol, "trades": [], "metrics": {"total_trades": 0}}

    pip_value = get_pip_value(symbol)
    trades = []
    pending_order = None
    position = None

    for timestamp, row in merged.iterrows():
        if position is not None:
            position["sl"] = apply_trailing_stop(
                position["direction"],
                position["entry"],
                position["initial_sl"],
                position["sl"],
                row["close"],
            )

            exit_price = None
            exit_reason = None
            if position["direction"] == "BUY":
                if row["low"] <= position["sl"]:
                    exit_price = position["sl"]
                    exit_reason = "SL"
                elif row["high"] >= position["tp"]:
                    exit_price = position["tp"]
                    exit_reason = "TP"
            else:
                if row["high"] >= position["sl"]:
                    exit_price = position["sl"]
                    exit_reason = "SL"
                elif row["low"] <= position["tp"]:
                    exit_price = position["tp"]
                    exit_reason = "TP"

            if exit_price is not None:
                risk = abs(position["entry"] - position["initial_sl"])
                pnl = exit_price - position["entry"] if position["direction"] == "BUY" \
                    else position["entry"] - exit_price
                trades.append({
                    "symbol": symbol,
                    "direction": position["direction"],
                    "entry_time": position["entry_time"],
                    "exit_time": timestamp,
                    "entry": position["entry"],
                    "exit": exit_price,
                    "sl": position["sl"],
                    "tp": position["tp"],
                    "rr": pnl / risk if risk else 0,
                    "exit_reason": exit_reason,
                })
                position = None
                continue

        if position is None and pending_order is not None:
            price_distance = abs(row["close"] - pending_order["entry"])
            if price_distance >= pending_order["initial_risk"] * 0.5:
                pending_order = None
            else:
                if pending_order["direction"] == "BUY" and row["high"] >= pending_order["entry"]:
                    position = {
                        "direction": "BUY",
                        "entry": pending_order["entry"],
                        "entry_time": timestamp,
                        "initial_sl": pending_order["sl"],
                        "sl": pending_order["sl"],
                        "tp": pending_order["tp"],
                    }
                    pending_order = None
                elif pending_order["direction"] == "SELL" and row["low"] <= pending_order["entry"]:
                    position = {
                        "direction": "SELL",
                        "entry": pending_order["entry"],
                        "entry_time": timestamp,
                        "initial_sl": pending_order["sl"],
                        "sl": pending_order["sl"],
                        "tp": pending_order["tp"],
                    }
                    pending_order = None

        if position is None and pending_order is None:
            signal = generate_signal(row, params)
            if signal in {"BUY", "SELL"}:
                if signal == "BUY":
                    entry_price = row["swing_high"] + params.pip_buffer * pip_value
                    stop_loss = row["swing_low"] - (row["ATR"] * params.atr_multiplier)
                else:
                    entry_price = row["swing_low"] - params.pip_buffer * pip_value
                    stop_loss = row["swing_high"] + (row["ATR"] * params.atr_multiplier)
                if np.isfinite(entry_price) and np.isfinite(stop_loss):
                    take_profit = entry_price + (entry_price - stop_loss) * params.rr_ratio if signal == "BUY" \
                        else entry_price - (stop_loss - entry_price) * params.rr_ratio
                    pending_order = {
                        "direction": signal,
                        "entry": entry_price,
                        "sl": stop_loss,
                        "tp": take_profit,
                        "initial_risk": abs(entry_price - stop_loss),
                    }

    if position is not None:
        risk = abs(position["entry"] - position["initial_sl"])
        last_close = merged.iloc[-1]["close"]
        pnl = last_close - position["entry"] if position["direction"] == "BUY" \
            else position["entry"] - last_close
        trades.append({
            "symbol": symbol,
            "direction": position["direction"],
            "entry_time": position["entry_time"],
            "exit_time": merged.index[-1],
            "entry": position["entry"],
            "exit": last_close,
            "sl": position["sl"],
            "tp": position["tp"],
            "rr": pnl / risk if risk else 0,
            "exit_reason": "EOD",
        })

    metrics = summarize_trades(trades)
    elapsed = time.perf_counter() - symbol_start
    LOGGER.info("Completed backtest for %s in %.2fs", symbol, elapsed)
    return {"symbol": symbol, "trades": trades, "metrics": metrics}


def summarize_trades(trades: list[dict], years: int | None = None) -> dict:
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0,
            "net_r": 0,
            "profit_factor": 0,
            "max_drawdown": 0,
            "sharpe": 0,
            "sortino": 0,
            "cagr": 0,
            "yearly_returns": {},
        }

    r_values = np.array([trade["rr"] for trade in trades], dtype=float)
    wins = r_values[r_values > 0]
    losses = r_values[r_values <= 0]
    profit_factor = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    win_rate = float((r_values > 0).mean())
    equity = 1.0
    equity_curve = []
    for r_value in r_values:
        equity *= 1 + r_value
        equity_curve.append(equity)
    equity_series = np.array(equity_curve, dtype=float)
    running_max = np.maximum.accumulate(equity_series)
    safe_running_max = np.where(running_max <= 0, np.nan, running_max)
    drawdowns = (equity_series - safe_running_max) / safe_running_max
    max_drawdown = float(np.nanmin(drawdowns)) if np.isfinite(drawdowns).any() else 0

    returns = r_values
    mean_return = returns.mean() if len(returns) else 0
    std_return = returns.std(ddof=1) if len(returns) > 1 else 0
    sharpe = (mean_return / std_return) * np.sqrt(len(returns)) if std_return else 0
    downside = returns[returns < 0]
    downside_std = downside.std(ddof=1) if len(downside) > 1 else 0
    sortino = (mean_return / downside_std) * np.sqrt(len(returns)) if downside_std else 0

    trade_times = pd.to_datetime([trade["exit_time"] for trade in trades])
    yearly_returns = (
        pd.Series(returns, index=trade_times)
        .groupby(trade_times.year)
        .sum()
        .to_dict()
    )

    if years is None:
        duration_years = (
            (trade_times.max() - trade_times.min()).days / 365 if len(trade_times) > 1 else 0
        )
    else:
        duration_years = years
    cagr = (equity ** (1 / duration_years) - 1) if duration_years else 0

    return {
        "total_trades": len(trades),
        "win_rate": win_rate,
        "net_r": r_values.sum(),
        "avg_r": r_values.mean(),
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "sortino": sortino,
        "cagr": cagr,
        "yearly_returns": yearly_returns,
    }


def run_backtest(
    symbols: list[str],
    years: int,
    params: StrategyParams,
    require_complete: bool = False,
    data_cache: MT5DataCache | None = None,
    end_time: dt.datetime | None = None,
) -> tuple[pd.DataFrame, list[dict], dict]:
    results = []
    all_trades = []
    for symbol in symbols:
        result = backtest_symbol(
            symbol,
            years,
            params,
            require_complete=require_complete,
            data_cache=data_cache,
            end_time=end_time,
        )
        metrics = summarize_trades(result["trades"], years=years)
        all_trades.extend(result["trades"])
        metrics["symbol"] = symbol
        results.append(metrics)
    overall_metrics = summarize_trades(all_trades, years=years)
    overall_metrics["symbol"] = "ALL"
    return pd.DataFrame(results), all_trades, overall_metrics


def expand_yearly_returns(df: pd.DataFrame) -> pd.DataFrame:
    if "yearly_returns" not in df.columns:
        return df
    years = set()
    for value in df["yearly_returns"]:
        if isinstance(value, dict):
            years.update(value.keys())
    if not years:
        return df
    for year in sorted(years):
        column = f"year_return_{year}"
        df[column] = df["yearly_returns"].apply(
            lambda yearly: yearly.get(year, 0) if isinstance(yearly, dict) else 0
        )
    df = df.drop(columns=["yearly_returns"])
    return df


def build_column_definitions(year_columns: list[str]) -> pd.DataFrame:
    definitions = [
        ("total_trades", "Total number of completed trades for the symbol."),
        ("win_rate", "Proportion of trades with positive R values."),
        ("net_r", "Sum of R values across all trades."),
        ("avg_r", "Average R value per trade."),
        ("profit_factor", "Gross wins divided by gross losses."),
        ("max_drawdown", "Maximum drawdown of the equity curve (fraction)."),
        ("sharpe", "Sharpe ratio computed on trade R values."),
        ("sortino", "Sortino ratio computed on trade R values."),
        ("cagr", "Compound annual growth rate based on the equity curve."),
        ("symbol", "Instrument symbol or ALL for the portfolio summary."),
    ]
    for column in year_columns:
        year = column.replace("year_return_", "")
        definitions.append((column, f"Summed R values for trades closed in {year}."))
    return pd.DataFrame(definitions, columns=["column", "definition"])


def parse_symbols(value: str) -> list[str]:
    if not value:
        return DEFAULT_SYMBOLS
    return [symbol.strip() for symbol in value.split(",") if symbol.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest EMA+VWAP strategy using MT5 data.")
    parser.add_argument("--years", type=int, default=1, help="Number of years of data to backtest.")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated list of symbols.")
    parser.add_argument("--atr-multiplier", type=float, default=2.0)
    parser.add_argument("--rr-ratio", type=float, default=2.0)
    parser.add_argument("--entry-timeframe", type=str, default="M5")
    parser.add_argument("--trend-timeframe", type=str, default="H1")
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Require complete history; otherwise allow incomplete data.",
    )
    args = parser.parse_args()

    timeframe_map = {name: value for name, value in vars(mt5).items() if name.startswith("TIMEFRAME_")}
    entry_tf = timeframe_map.get(f"TIMEFRAME_{args.entry_timeframe.upper()}", mt5.TIMEFRAME_M5)
    trend_tf = timeframe_map.get(f"TIMEFRAME_{args.trend_timeframe.upper()}", mt5.TIMEFRAME_H1)
    params = StrategyParams(
        atr_multiplier=args.atr_multiplier,
        rr_ratio=args.rr_ratio,
        entry_timeframe=entry_tf,
        trend_timeframe=trend_tf,
    )
    symbols = parse_symbols(args.symbols)

    initialize_mt5()
    try:
        summary, _, overall_metrics = run_backtest(
            symbols,
            args.years,
            params,
            require_complete=args.require_complete,
        )
    finally:
        shutdown_mt5()

    output_dir = "backtest_outputs"
    os.makedirs(output_dir, exist_ok=True)
    summary = expand_yearly_returns(summary)
    overall_df = expand_yearly_returns(pd.DataFrame([overall_metrics]))

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    summary_path = f"{output_dir}/backtest_summary_{timestamp}.csv"
    summary.to_csv(summary_path, index=False)
    overall_path = f"{output_dir}/backtest_overall_metrics_{timestamp}.csv"
    overall_df.to_csv(overall_path, index=False)

    year_columns = [col for col in summary.columns if col.startswith("year_return_")]
    definitions = build_column_definitions(year_columns)
    definitions_path = f"{output_dir}/backtest_column_definitions_{timestamp}.csv"
    definitions.to_csv(definitions_path, index=False)
    print(f"Backtest summary saved to {summary_path}")
    print(f"Backtest overall metrics saved to {overall_path}")
    print(f"Backtest column definitions saved to {definitions_path}")


if __name__ == "__main__":
    main()
