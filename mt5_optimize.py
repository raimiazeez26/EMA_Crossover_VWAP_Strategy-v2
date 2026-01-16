import argparse
import datetime as dt
import itertools
import json
import os
from dataclasses import replace

import pandas as pd

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional dependency
    tqdm = None

import mt5_backtest


def parse_timeframes(value: str) -> list[int]:
    timeframe_map = {name: val for name, val in vars(mt5_backtest.mt5).items() if name.startswith("TIMEFRAME_")}
    timeframes = []
    for entry in value.split(","):
        label = entry.strip().upper()
        if not label:
            continue
        timeframes.append(timeframe_map.get(f"TIMEFRAME_{label}", mt5_backtest.mt5.TIMEFRAME_M5))
    return timeframes


def build_param_grid(args: argparse.Namespace) -> list[mt5_backtest.StrategyParams]:
    atr_values = [float(v) for v in args.atr_multipliers.split(",") if v]
    rr_values = [float(v) for v in args.rr_ratios.split(",") if v]
    rsi_overbought_values = [float(v) for v in args.rsi_overbought.split(",") if v]
    rsi_oversold_values = [float(v) for v in args.rsi_oversold.split(",") if v]
    pip_buffer_values = [int(v) for v in args.pip_buffers.split(",") if v]
    ema_fast_values = [int(v) for v in args.ema_fast.split(",") if v]
    ema_mid_values = [int(v) for v in args.ema_mid.split(",") if v]
    ema_slow_values = [int(v) for v in args.ema_slow.split(",") if v]
    entry_timeframes = parse_timeframes(args.entry_timeframes)
    trend_timeframes = parse_timeframes(args.trend_timeframes)

    param_sets = []
    base_params = mt5_backtest.StrategyParams()
    for atr, rr, rsi_ob, rsi_os, pip_buffer, ema_fast, ema_mid, ema_slow, entry_tf, trend_tf in itertools.product(
        atr_values,
        rr_values,
        rsi_overbought_values,
        rsi_oversold_values,
        pip_buffer_values,
        ema_fast_values,
        ema_mid_values,
        ema_slow_values,
        entry_timeframes,
        trend_timeframes,
    ):
        param_sets.append(
            replace(
                base_params,
                atr_multiplier=atr,
                rr_ratio=rr,
                rsi_overbought=rsi_ob,
                rsi_oversold=rsi_os,
                pip_buffer=pip_buffer,
                ema_fast=ema_fast,
                ema_mid=ema_mid,
                ema_slow=ema_slow,
                entry_timeframe=entry_tf,
                trend_timeframe=trend_tf,
            )
        )
    return param_sets


def score_results(results: pd.DataFrame) -> float:
    if results.empty:
        return float("-inf")
    return results["net_r"].sum()


def run_optimization(
    symbols: list[str],
    years: int,
    param_grid: list[mt5_backtest.StrategyParams],
    require_complete: bool = False,
    data_cache: mt5_backtest.MT5DataCache | None = None,
    end_time: dt.datetime | None = None,
) -> pd.DataFrame:
    records = []
    if tqdm:
        progress_iter = tqdm(param_grid, desc="Optimizing", unit="config", dynamic_ncols=True)
    else:
        progress_iter = param_grid
    for index, params in enumerate(progress_iter, start=1):
        summary, _, _ = mt5_backtest.run_backtest(
            symbols,
            years,
            params,
            require_complete=require_complete,
            data_cache=data_cache,
            end_time=end_time,
        )
        total_net_r = score_results(summary)
        total_trades = summary["total_trades"].sum()
        win_rate = (summary["win_rate"] * summary["total_trades"]).sum()
        win_rate = win_rate / total_trades if total_trades else 0
        records.append(
            {
                "atr_multiplier": params.atr_multiplier,
                "rr_ratio": params.rr_ratio,
                "rsi_overbought": params.rsi_overbought,
                "rsi_oversold": params.rsi_oversold,
                "pip_buffer": params.pip_buffer,
                "ema_fast": params.ema_fast,
                "ema_mid": params.ema_mid,
                "ema_slow": params.ema_slow,
                "entry_timeframe": params.entry_timeframe,
                "trend_timeframe": params.trend_timeframe,
                "net_r": total_net_r,
                "total_trades": total_trades,
                "win_rate": win_rate,
            }
        )
        if tqdm is None and index % 5 == 0:
            print(f"Optimization progress: {index}/{len(param_grid)} configs")
    return pd.DataFrame(records).sort_values(by="net_r", ascending=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Optimize EMA+VWAP strategy using MT5 data.")
    parser.add_argument("--years", type=int, default=1, help="Number of years of data to backtest.")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated list of symbols.")
    parser.add_argument("--atr-multipliers", type=str, default="1.5")
    parser.add_argument("--rr-ratios", type=str, default="1.5")
    parser.add_argument("--rsi-overbought", type=str, default="80,90")
    parser.add_argument("--rsi-oversold", type=str, default="10,20")
    parser.add_argument("--pip-buffers", type=str, default="5")
    parser.add_argument("--ema-fast", type=str, default="5,8")
    parser.add_argument("--ema-mid", type=str, default="10,13")
    parser.add_argument("--ema-slow", type=str, default="21,50")
    parser.add_argument("--entry-timeframes", type=str, default="M5") #,M15
    parser.add_argument("--trend-timeframes", type=str, default="H4") #H1,
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Require complete history; otherwise allow incomplete data.",
    )
    parser.add_argument("--top-assets", type=int, default=10, help="Number of top assets to report.")
    args = parser.parse_args()

    symbols = mt5_backtest.parse_symbols(args.symbols)
    param_grid = build_param_grid(args)

    mt5_backtest.initialize_mt5()
    try:
        data_cache = mt5_backtest.MT5DataCache()
        end_time = dt.datetime.now(dt.timezone.utc)
        optimization_results = run_optimization(
            symbols,
            args.years,
            param_grid,
            require_complete=args.require_complete,
            data_cache=data_cache,
            end_time=end_time,
        )
        per_asset_records = []
        for params in param_grid:
            summary, _, _ = mt5_backtest.run_backtest(
                symbols,
                args.years,
                params,
                require_complete=args.require_complete,
                data_cache=data_cache,
                end_time=end_time,
            )
            summary = summary.assign(
                atr_multiplier=params.atr_multiplier,
                rr_ratio=params.rr_ratio,
                rsi_overbought=params.rsi_overbought,
                rsi_oversold=params.rsi_oversold,
                pip_buffer=params.pip_buffer,
                ema_fast=params.ema_fast,
                ema_mid=params.ema_mid,
                ema_slow=params.ema_slow,
                entry_timeframe=params.entry_timeframe,
                trend_timeframe=params.trend_timeframe,
            )
            per_asset_records.append(summary)
        per_asset_results = pd.concat(per_asset_records, ignore_index=True) if per_asset_records else pd.DataFrame()
        best_params = optimization_results.iloc[0]
        params = mt5_backtest.StrategyParams(
            atr_multiplier=best_params["atr_multiplier"],
            rr_ratio=best_params["rr_ratio"],
            rsi_overbought=best_params["rsi_overbought"],
            rsi_oversold=best_params["rsi_oversold"],
            pip_buffer=int(best_params.get("pip_buffer", 3)),
            ema_fast=int(best_params.get("ema_fast", 8)),
            ema_mid=int(best_params.get("ema_mid", 13)),
            ema_slow=int(best_params.get("ema_slow", 21)),
            entry_timeframe=int(best_params["entry_timeframe"]),
            trend_timeframe=int(best_params["trend_timeframe"]),
        )
        all_assets, _, _ = mt5_backtest.run_backtest(
            symbols,
            args.years,
            params,
            require_complete=args.require_complete,
            data_cache=data_cache,
            end_time=end_time,
        )
        all_assets = all_assets.sort_values(by="net_r", ascending=False)
        best_assets = all_assets.head(args.top_assets)
    finally:
        mt5_backtest.shutdown_mt5()

    output_dir = "backtest_outputs"
    os.makedirs(output_dir, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    optimization_results.to_csv(f"{output_dir}/optimization_results_{timestamp}.csv", index=False)
    if not per_asset_results.empty:
        per_asset_results.to_csv(f"{output_dir}/optimization_results_by_asset_{timestamp}.csv", index=False)
    entry_label = args.entry_timeframes.split(",")[0].strip().upper() or "ENTRY"
    trend_label = args.trend_timeframes.split(",")[0].strip().upper() or "TREND"
    best_assets.to_csv(f"{output_dir}/best_assets_{entry_label}_{trend_label}.csv", index=False)

    symbol_params_payload = {
        "generated_at": timestamp,
        "assets": [
            {
                "symbol": row["symbol"],
                "net_r": row["net_r"],
                "params": {
                    "atr_multiplier": params.atr_multiplier,
                    "rr_ratio": params.rr_ratio,
                    "rsi_overbought": params.rsi_overbought,
                    "rsi_oversold": params.rsi_oversold,
                    "pip_buffer": params.pip_buffer,
                    "ema_fast": params.ema_fast,
                    "ema_mid": params.ema_mid,
                    "ema_slow": params.ema_slow,
                    "entry_timeframe": params.entry_timeframe,
                    "trend_timeframe": params.trend_timeframe,
                },
            }
            for _, row in all_assets.iterrows()
        ],
    }
    symbol_params_path = f"{output_dir}/symbol_params_{timestamp}.json"
    with open(symbol_params_path, "w", encoding="utf-8") as handle:
        json.dump(symbol_params_payload, handle, indent=2)
    latest_params_path = f"{output_dir}/symbol_params_latest_{timestamp}.json"
    with open(latest_params_path, "w", encoding="utf-8") as handle:
        json.dump(symbol_params_payload, handle, indent=2)

    print(f"Optimization results saved to backtest_outputs/optimization_results_{timestamp}.csv")
    if not per_asset_results.empty:
        print(f"Per-asset optimization saved to backtest_outputs/optimization_results_by_asset_{timestamp}.csv")
    print(f"Best assets saved to backtest_outputs/best_assets_{entry_label}_{trend_label}.csv")
    print(f"Symbol params saved to {symbol_params_path}")
    print(f"Latest symbol params saved to {latest_params_path}")


if __name__ == "__main__":
    main()
