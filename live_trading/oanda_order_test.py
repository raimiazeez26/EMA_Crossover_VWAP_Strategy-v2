import argparse
import logging
import os
from datetime import datetime

from oanda_trading import OandaAdvancedTradingBot, load_symbol_params, _format_price

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)


def get_mid_price(bot, symbol):
    oanda_symbol = bot.normalize_symbol(symbol)
    payload = bot._request(
        "GET",
        f"/v3/accounts/{bot.oanda_account_id}/pricing",
        params={"instruments": oanda_symbol},
    )
    prices = payload.get("prices", [])
    if not prices:
        return None
    bids = prices[0].get("bids", [])
    asks = prices[0].get("asks", [])
    if not bids or not asks:
        return None
    return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2


def build_pending_order(bot, symbol, signal, entry_price, stop_loss, take_profit):
    precision = bot.get_price_precision(symbol)
    oanda_symbol = bot.normalize_symbol(symbol)
    return {
        "order": {
            "units": "1" if signal == "BUY" else "-1",
            "instrument": oanda_symbol,
            "timeInForce": "GTC",
            "type": "STOP",
            "price": _format_price(entry_price, precision),
            "positionFill": "DEFAULT",
            "clientExtensions": {"comment": bot.comment},
            "takeProfitOnFill": {"price": _format_price(take_profit, precision)},
            "stopLossOnFill": {"price": _format_price(stop_loss, precision)},
        }
    }


def confirm_order(bot, order_id):
    try:
        details = bot._request("GET", f"/v3/accounts/{bot.oanda_account_id}/orders/{order_id}")
    except Exception as exc:
        logging.warning("Failed to fetch order %s details: %s", order_id, exc)
        return None
    order = details.get("order", {})
    state = order.get("state", "UNKNOWN")
    reason = order.get("reason")
    logging.info("Order %s state: %s (reason=%s)", order_id, state, reason)
    return order


def main():
    parser = argparse.ArgumentParser(description="Test Oanda pending orders per symbol")
    parser.add_argument("--symbol-params", default="backtest_outputs/symbol_params_latest.json")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="Do not submit orders")
    args = parser.parse_args()

    symbols, params = load_symbol_params(args.symbol_params, args.top_n)
    if not symbols:
        logging.error("No FX symbols loaded. Update symbol params before testing.")
        return

    bot = OandaAdvancedTradingBot(symbols=symbols, risk_percent=1, symbol_params=params)

    symbols = ["XAU_USD"]
    for symbol in symbols:
        if not bot.is_fx_pair(symbol):
            logging.info("Skipping non-FX symbol %s", symbol)
            continue
        if bot.has_existing_trade(symbol):
            logging.info("Existing trade/order for %s. Skipping order test.", symbol)
            continue
        mid_price = get_mid_price(bot, symbol)
        if mid_price is None:
            logging.warning("No pricing data for %s", symbol)
            continue
        pip_size = bot.get_pip_size(symbol)
        print(f"Pip size for {symbol}: {pip_size}")
        pip_buffer = params.get(symbol, {}).get("pip_buffer", 3)
        pip_distance = pip_size * pip_buffer
        entry_price = mid_price + pip_distance
        stop_loss = mid_price - pip_distance
        take_profit = mid_price + (pip_distance * 2)
        order_payload = build_pending_order(bot, symbol, "SELL", entry_price, stop_loss, take_profit)
        logging.info("Order payload for %s: %s", symbol, order_payload)
        if args.dry_run:
            continue
        try:
            response = bot._request("POST", f"/v3/accounts/{bot.oanda_account_id}/orders", payload=order_payload)
            order_id = response.get("orderCreateTransaction", {}).get("id")
            logging.info("Submitted pending order for %s (order_id=%s)", symbol, order_id)
            if order_id:
                confirm_order(bot, order_id)
            else:
                logging.warning("No order id returned for %s; response=%s", symbol, response)
        except Exception as exc:
            logging.warning("Failed to submit pending order for %s: %s", symbol, exc)


if __name__ == "__main__":
    main()
