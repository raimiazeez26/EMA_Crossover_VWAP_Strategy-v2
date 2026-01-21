import json
import logging
import os
import time
import warnings
from datetime import datetime
from uuid import uuid4

import numpy as np
import pandas as pd
import requests
import talib
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("oanda_trading.log"), logging.StreamHandler()],
)

load_dotenv()
warnings.filterwarnings("ignore")


def _to_decimal(value):
    return float(f"{value:.5f}")


def _format_price(value, precision):
    return f"{value:.{precision}f}"


def record_trade(entry, path="backtest_outputs/oanda_trade_history.csv"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fieldnames = [
        "timestamp",
        "symbol",
        "signal",
        "units",
        "entry_price",
        "stop_loss",
        "take_profit",
        "strategy",
    ]
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8") as handle:
        if not exists:
            handle.write(",".join(fieldnames) + "\n")
        row = [str(entry.get(field, "")) for field in fieldnames]
        handle.write(",".join(row) + "\n")


class OandaAdvancedTradingBot:
    def __init__(self, symbols, risk_percent=1, symbol_params=None):
        self.symbols = symbols
        self.risk_percent = risk_percent
        self.symbol_params = symbol_params or {}
        self.strategy_name = "EMA_VWAP"
        self.active_symbols = set()
        self.last_eod_date = None
        self.last_nav = None
        self.last_weekly_update_date = None
        self.instrument_meta = {}
        self.order_message_map = {}
        self.trade_message_map = {}
        self.closed_trade_notified = set()
        self.closed_order_notified = set()
        self.pending_orders = {}

        self.oanda_token = os.getenv("OANDA_TOKEN")
        self.oanda_account_id = os.getenv("OANDA_ACCOUNT_ID")
        self.oanda_base_url = os.getenv("OANDA_API_BASE_URL", "https://api-fxpractice.oanda.com")

        self.telegram_token = os.getenv("TELEGRAM_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.keepalive_url = os.getenv("HEROKU_KEEPALIVE_URL")

        if not self.oanda_token or not self.oanda_account_id:
            raise ValueError("Missing OANDA_TOKEN or OANDA_ACCOUNT_ID in environment")

        self.sync_existing_positions()

    def _request(self, method, path, payload=None, params=None):
        url = f"{self.oanda_base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.oanda_token}",
            "Content-Type": "application/json",
        }
        logging.debug("Oanda request %s %s params=%s payload=%s", method, url, params, payload)
        response = requests.request(method, url, headers=headers, json=payload, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def normalize_symbol(self, symbol):
        if "_" in symbol:
            return symbol
        if len(symbol) == 6:
            return f"{symbol[:3]}_{symbol[3:]}"
        if symbol.startswith("XAU") and len(symbol) == 6:
            return f"{symbol[:3]}_{symbol[3:]}"
        if symbol.startswith("XAG") and len(symbol) == 6:
            return f"{symbol[:3]}_{symbol[3:]}"
        return symbol

    def get_instrument_meta(self, symbol):
        oanda_symbol = self.normalize_symbol(symbol)
        if oanda_symbol in self.instrument_meta:
            return self.instrument_meta[oanda_symbol]
        try:
            payload = self._request(
                "GET",
                f"/v3/accounts/{self.oanda_account_id}/instruments",
                params={"instruments": oanda_symbol},
            )
        except requests.RequestException as exc:
            logging.warning("Instrument lookup failed for %s: %s", oanda_symbol, exc)
            return None
        instruments = payload.get("instruments", [])
        if not instruments:
            logging.warning("Instrument metadata missing for %s", oanda_symbol)
            return None
        meta = instruments[0]
        self.instrument_meta[oanda_symbol] = meta
        return meta

    def get_pip_size(self, symbol):
        meta = self.get_instrument_meta(symbol)
        if meta and "pipLocation" in meta:
            return 10 ** int(meta["pipLocation"])
        normalized = self.normalize_symbol(symbol)
        if normalized.endswith("_JPY") or normalized.endswith("JPY"):
            return 0.01
        if normalized.startswith("XAU") or normalized.startswith("XAG"):
            return 0.1
        return 0.0001

    def get_price_precision(self, symbol):
        meta = self.get_instrument_meta(symbol)
        if meta and "displayPrecision" in meta:
            return int(meta["displayPrecision"])
        normalized = self.normalize_symbol(symbol)
        if normalized.endswith("_JPY") or normalized.endswith("JPY"):
            return 3
        if normalized.startswith("XAU") or normalized.startswith("XAG"):
            return 2
        return 5

    def get_quote_to_usd_rate(self, symbol, price_hint=None):
        normalized = self.normalize_symbol(symbol)
        if "_" not in normalized:
            return 1.0
        _, quote = normalized.split("_", maxsplit=1)
        if quote == "USD":
            return 1.0
        if normalized.startswith("USD_"):
            if price_hint:
                return 1 / float(price_hint)
            pricing = self.fetch_pricing(normalized)
            if pricing:
                return 1 / float(pricing["bid"])
        pair_usd_quote = f"USD_{quote}"
        pricing = self.fetch_pricing(pair_usd_quote)
        if pricing:
            return 1 / float(pricing["bid"])
        pair_quote_usd = f"{quote}_USD"
        pricing = self.fetch_pricing(pair_quote_usd)
        if pricing:
            return float(pricing["bid"])
        logging.warning("Conversion rate unavailable for quote %s; assuming USD.", quote)
        return 1.0

    def is_fx_pair(self, symbol):
        normalized = self.normalize_symbol(symbol)
        if "_" not in normalized:
            return False
        base, quote = normalized.split("_", maxsplit=1)
        return len(base) == 3 and len(quote) == 3 and base.isalpha() and quote.isalpha()

    def resolve_granularity(self, timeframe_value):
        mapping = {
            1: "M1",
            5: "M5",
            15: "M15",
            30: "M30",
            60: "H1",
            16385: "H1",
            16386: "H2",
            16387: "H3",
            16388: "H4",
        }
        if isinstance(timeframe_value, str):
            return timeframe_value
        return mapping.get(int(timeframe_value), "M5")

    def send_telegram(self, message, reply_to_message_id=None):
        if not self.telegram_token or not self.telegram_chat_id:
            return None
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat_id, "text": message}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        try:
            response = requests.post(url, data=payload, timeout=10)
            response.raise_for_status()
            return response.json().get("result", {}).get("message_id")
        except requests.RequestException as exc:
            logging.warning("Telegram notification failed: %s", exc)
        return None

    def fetch_pricing(self, symbol):
        """Fetch live pricing (bid/ask) for a single instrument."""
        oanda_symbol = self.normalize_symbol(symbol)
        try:
            payload = self._request(
                "GET",
                f"/v3/accounts/{self.oanda_account_id}/pricing",
                params={"instruments": oanda_symbol},
            )
        except requests.RequestException as exc:
            logging.warning("Pricing request failed for %s: %s", oanda_symbol, exc)
            return None

        prices = payload.get("prices", [])
        if not prices:
            return None

        p0 = prices[0]
        bids = p0.get("bids", [])
        asks = p0.get("asks", [])
        if not bids or not asks:
            return None

        try:
            bid = float(bids[0]["price"])
            ask = float(asks[0]["price"])
        except (KeyError, ValueError, TypeError):
            return None

        return {"bid": bid, "ask": ask, "instrument": oanda_symbol}

    def _order_extensions_match(self, extensions):
        if not extensions:
            return False
        if extensions.get("comment") == self.strategy_name:
            return True
        return extensions.get("tag") == self.strategy_name

    def _should_cancel_order(self, order, pricing):
        if not pricing:
            return False
        try:
            entry_price = float(order.get("price"))
            stop_price = float(order.get("stopLossOnFill", {}).get("price"))
        except (TypeError, ValueError):
            return False
        if entry_price == stop_price:
            return False
        distance = abs(entry_price - stop_price)
        if distance <= 0:
            return False
        direction = order.get("units")
        if direction is None:
            return False
        try:
            units = float(direction)
        except (TypeError, ValueError):
            return False
        if units > 0:
            threshold = entry_price - 0.5 * distance
            return pricing["bid"] <= threshold
        threshold = entry_price + 0.5 * distance
        return pricing["ask"] >= threshold

    def _cancel_order(self, order_id):
        return self._request("PUT", f"/v3/accounts/{self.oanda_account_id}/orders/{order_id}/cancel")

    def sync_order_updates(self):
        try:
            orders_payload = self._request("GET", f"/v3/accounts/{self.oanda_account_id}/pendingOrders")
        except requests.RequestException as exc:
            logging.warning("Pending order sync failed: %s", exc)
            return

        current_orders = {}
        for order in orders_payload.get("orders", []):
            extensions = order.get("clientExtensions") or order.get("orderClientExtensions") or {}
            if not self._order_extensions_match(extensions):
                continue
            order_id = order.get("id")
            if not order_id:
                continue
            current_orders[order_id] = order

        for order_id, order in current_orders.items():
            pricing = self.fetch_pricing(order.get("instrument"))
            if not pricing:
                continue
            if self._should_cancel_order(order, pricing):
                try:
                    self._cancel_order(order_id)
                    message = "\n".join(
                        [
                            f"Order canceled: {self.strategy_name}",
                            f"Symbol: {order.get('instrument')}",
                            f"Order ID: {order_id}",
                            f"Reason: price moved 50% against entry",
                            f"Entry: {order.get('price')}",
                            f"Stop Loss: {order.get('stopLossOnFill', {}).get('price')}",
                            f"Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}",
                        ]
                    )
                    reply_to = self.order_message_map.get(order_id)
                    self.send_telegram(message, reply_to_message_id=reply_to)
                    self.closed_order_notified.add(order_id)
                except requests.RequestException as exc:
                    logging.warning("Failed to cancel order %s: %s", order_id, exc)

        missing_orders = set(self.pending_orders) - set(current_orders)
        for order_id in missing_orders:
            if order_id in self.closed_order_notified:
                continue
            try:
                order_payload = self._request(
                    "GET", f"/v3/accounts/{self.oanda_account_id}/orders/{order_id}"
                )
            except requests.RequestException as exc:
                logging.warning("Failed to fetch order %s details: %s", order_id, exc)
                continue
            order = order_payload.get("order", {})
            state = order.get("state", "UNKNOWN")
            if str(state).upper() == "CANCELLED":
                message = "\n".join(
                    [
                        f"Order canceled: {self.strategy_name}",
                        f"Order ID: {order_id}",
                        f"Symbol: {order.get('instrument')}",
                        f"State: {state}",
                        f"Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}",
                    ]
                )
                reply_to = self.order_message_map.get(order_id)
                self.send_telegram(message, reply_to_message_id=reply_to)
                self.closed_order_notified.add(order_id)

        self.pending_orders = current_orders

    def send_keepalive(self):
        if not self.keepalive_url:
            return
        try:
            response = requests.get(self.keepalive_url, timeout=10)
            response.raise_for_status()
            logging.info("Keepalive ping succeeded (%s)", response.status_code)
        except requests.RequestException as exc:
            logging.warning("Keepalive ping failed: %s", exc)

    @staticmethod
    def interpret_order_response(resp):
        """Return a compact verdict for an OrderCreate response."""
        summary = {
            "accepted": False,
            "rejected": False,
            "immediately_canceled": False,
            "filled_immediately": False,
            "order_id": None,
            "reason": None,
        }

        if not isinstance(resp, dict):
            summary["rejected"] = True
            summary["reason"] = "invalid_response"
            return summary

        if "orderRejectTransaction" in resp:
            summary["rejected"] = True
            summary["reason"] = resp["orderRejectTransaction"].get("rejectReason")
            return summary

        create_txn = resp.get("orderCreateTransaction")
        if create_txn:
            summary["accepted"] = True
            summary["order_id"] = create_txn.get("orderID")

        if resp.get("orderFillTransaction"):
            summary["filled_immediately"] = True

        cancel_txn = resp.get("orderCancelTransaction")
        if cancel_txn:
            summary["immediately_canceled"] = True
            summary["reason"] = cancel_txn.get("reason")

        return summary

    def _min_entry_distance_abs(self, symbol, distance_abs):
        """
        XAU/XAG often have wider spreads. If your STOP is inside the spread,
        OANDA may reject/cancel it immediately.
        We enforce a practical minimum absolute distance for metals.
        """
        # Optional override from params
        user_min = self.symbol_params.get(symbol, {}).get("min_entry_distance_abs")
        if user_min is not None:
            return max(float(user_min), float(distance_abs))

        normalized = self.normalize_symbol(symbol)
        if normalized.startswith("XAU"):
            return max(1.0, float(distance_abs))    # $1 minimum for gold
        if normalized.startswith("XAG"):
            return max(0.10, float(distance_abs))   # 10c minimum for silver
        return float(distance_abs)

    def _ensure_order_sanity(self, signal, entry_price, stop_loss, take_profit, atr_value, atr_multiplier, rr_ratio):
        """
        Safety checks:
        - BUY: SL < entry < TP
        - SELL: TP < entry < SL
        If violated (rare), adjust using ATR-based distances.
        """
        if signal == "BUY":
            if stop_loss >= entry_price:
                stop_loss = entry_price - abs(float(atr_value) * float(atr_multiplier))
            if take_profit <= entry_price:
                take_profit = entry_price + abs(entry_price - stop_loss) * float(rr_ratio)
        else:
            if stop_loss <= entry_price:
                stop_loss = entry_price + abs(float(atr_value) * float(atr_multiplier))
            if take_profit >= entry_price:
                take_profit = entry_price - abs(stop_loss - entry_price) * float(rr_ratio)
        return entry_price, stop_loss, take_profit

    def fetch_data(self, symbol, granularity, count=300):
        oanda_symbol = self.normalize_symbol(symbol)
        try:
            logging.info("Fetching %s candles for %s", granularity, oanda_symbol)
            payload = self._request(
                "GET",
                f"/v3/instruments/{oanda_symbol}/candles",
                params={"granularity": granularity, "count": count, "price": "M"},
            )
        except requests.RequestException as exc:
            logging.warning("Oanda candle request failed for %s: %s", oanda_symbol, exc)
            return None
        candles = payload.get("candles", [])
        if not candles:
            logging.warning("No candles returned for %s", oanda_symbol)
            return None
        rows = []
        for candle in candles:
            if not candle.get("complete"):
                continue
            mid = candle.get("mid", {})
            rows.append(
                {
                    "time": candle.get("time"),
                    "open": float(mid.get("o")),
                    "high": float(mid.get("h")),
                    "low": float(mid.get("l")),
                    "close": float(mid.get("c")),
                    "volume": float(candle.get("volume", 0)),
                }
            )
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        return df

    def calculate_indicators(self, df, params):
        logging.info("Calculating indicators")
        df = df.copy()
        df["EMA21"] = talib.EMA(df["close"], params.get("ema_slow", 21))
        df["EMA13"] = talib.EMA(df["close"], params.get("ema_mid", 13))
        df["EMA8"] = talib.EMA(df["close"], params.get("ema_fast", 8))

        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        cumulative_tp = (typical_price * df["volume"]).cumsum()
        cumulative_volume = df["volume"].cumsum()
        df["VWAP"] = cumulative_tp / cumulative_volume

        df["RSI"] = talib.RSI(df["close"], params.get("rsi_period", 14))
        df["ROC"] = talib.ROC(df["close"], params.get("roc_period", 3))
        df["ATR"] = talib.ATR(df["high"], df["low"], df["close"], timeperiod=params.get("atr_period", 5))
        df["swing_high"] = df["high"].rolling(params.get("swing_lookback", 21)).max()
        df["swing_low"] = df["low"].rolling(params.get("swing_lookback", 21)).min()
        return df.dropna()

    def has_existing_trade(self, symbol):
        oanda_symbol = self.normalize_symbol(symbol)
        if oanda_symbol in self.active_symbols:
            return True
        try:
            logging.info("Checking existing EMA_VWAP trades for %s", symbol)
            trades = self._request("GET", f"/v3/accounts/{self.oanda_account_id}/openTrades")
            orders = self._request("GET", f"/v3/accounts/{self.oanda_account_id}/pendingOrders")
        except requests.RequestException as exc:
            logging.warning("Trade lookup failed: %s", exc)
            return False
        for trade in trades.get("trades", []):
            if trade.get("instrument") != oanda_symbol:
                continue
            extensions = trade.get("clientExtensions") or trade.get("tradeClientExtensions") or {}
            if self._order_extensions_match(extensions):
                return True
        for order in orders.get("orders", []):
            if order.get("instrument") != oanda_symbol:
                continue
            extensions = order.get("clientExtensions") or order.get("orderClientExtensions") or {}
            if self._order_extensions_match(extensions):
                return True
        return False

    def generate_signal(self, symbol):
        params = self.symbol_params.get(symbol, {})
        entry_tf = self.resolve_granularity(params.get("entry_timeframe", "M5"))
        trend_tf = self.resolve_granularity(params.get("trend_timeframe", "H1"))

        logging.info("Generating signal for %s using %s/%s", symbol, entry_tf, trend_tf)
        m5_raw = self.fetch_data(symbol, entry_tf)
        h1_raw = self.fetch_data(symbol, trend_tf)
        if m5_raw is None or h1_raw is None:
            logging.warning("Missing data for %s signal generation", symbol)
            return "NEUTRAL", None

        m5_df = self.calculate_indicators(m5_raw, params)
        h1_df = self.calculate_indicators(h1_raw, params)
        if m5_df.empty or h1_df.empty:
            logging.warning("Insufficient indicator data for %s", symbol)
            return "NEUTRAL", None

        latest_h1 = h1_df.iloc[-1]
        latest_m5 = m5_df.iloc[-1]

        bearish_trend = latest_h1["EMA21"] > latest_h1["EMA13"] > latest_h1["EMA8"]
        bullish_trend = latest_h1["EMA21"] < latest_h1["EMA13"] < latest_h1["EMA8"]
        bearish_trend_m5 = latest_m5["EMA21"] > latest_m5["EMA13"] > latest_m5["EMA8"]
        bullish_trend_m5 = latest_m5["EMA21"] < latest_m5["EMA13"] < latest_m5["EMA8"]

        rsi_overbought = params.get("rsi_overbought", 70)
        rsi_oversold = params.get("rsi_oversold", 30)

        if (
            bullish_trend
            and bullish_trend_m5
            and latest_m5["close"] > latest_m5["VWAP"]
            and latest_m5["ROC"] > 0
            and latest_m5["RSI"] < rsi_overbought
        ):
            return "BUY", latest_m5
        if (
            bearish_trend
            and bearish_trend_m5
            and latest_m5["close"] < latest_m5["VWAP"]
            and latest_m5["ROC"] < 0
            and latest_m5["RSI"] > rsi_oversold
        ):
            return "SELL", latest_m5
        return "NEUTRAL", latest_m5

    def calculate_position_size(self, entry_price, stop_loss, symbol=None):
        logging.info("Calculating position size")
        account = self._request("GET", f"/v3/accounts/{self.oanda_account_id}")
        balance = float(account["account"]["balance"])
        risk_amount = balance * (self.risk_percent / 100)
        risk_per_unit = abs(entry_price - stop_loss)
        if symbol and self.is_fx_pair(symbol):
            quote_to_usd = self.get_quote_to_usd_rate(symbol, price_hint=entry_price)
            risk_per_unit *= quote_to_usd
        if risk_per_unit <= 0:
            return 0
        return max(int(risk_amount / risk_per_unit), 1)

    def place_order(self, symbol, signal, latest):
        if self.has_existing_trade(symbol):
            logging.info("Existing EMA_VWAP trade found for %s, skipping new order", symbol)
            return
        if latest is None:
            return

        params = self.symbol_params.get(symbol, {})
        atr_multiplier = params.get("atr_multiplier", 2)
        rr_ratio = params.get("rr_ratio", 2)
        pip_buffer = params.get("pip_buffer", 3)
        atr_value = latest["ATR"]
        oanda_symbol = self.normalize_symbol(symbol)
        price_precision = self.get_price_precision(symbol)
        pip_size = self.get_pip_size(symbol)
        raw_distance = pip_buffer * pip_size
        distance = self._min_entry_distance_abs(symbol, raw_distance)

        if signal == "BUY":
            entry_price = latest["swing_high"] + distance
            stop_loss = latest["swing_low"] - (atr_value * atr_multiplier)
            order_type = "STOP"
        else:
            entry_price = latest["swing_low"] - distance
            stop_loss = latest["swing_high"] + (atr_value * atr_multiplier)
            order_type = "STOP"

        take_profit = (
            entry_price + (entry_price - stop_loss) * rr_ratio
            if signal == "BUY"
            else entry_price - (stop_loss - entry_price) * rr_ratio
        )

        entry_price, stop_loss, take_profit = self._ensure_order_sanity(
            signal=signal,
            entry_price=float(entry_price),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit),
            atr_value=float(atr_value),
            atr_multiplier=float(atr_multiplier),
            rr_ratio=float(rr_ratio),
        )

        units = self.calculate_position_size(entry_price, stop_loss, symbol=symbol)
        if units <= 0:
            return
        if signal == "SELL":
            units = -units

        client_extension_id = f"{self.strategy_name}-{uuid4().hex[:10]}"
        order = {
            "order": {
                "units": str(units),
                "instrument": oanda_symbol,
                "timeInForce": "GTC",
                "type": order_type,
                "price": _format_price(entry_price, price_precision),
                "positionFill": "DEFAULT",
                "clientExtensions": {
                    "comment": self.strategy_name,
                    "tag": self.strategy_name,
                    "id": client_extension_id,
                },
                "takeProfitOnFill": {"price": _format_price(take_profit, price_precision)},
                "stopLossOnFill": {"price": _format_price(stop_loss, price_precision)},
            }
        }
        try:
            logging.info(
                "Placing %s order for %s (units=%s, entry=%.5f, sl=%.5f, tp=%.5f, pip_size=%s)",
                signal,
                oanda_symbol,
                units,
                entry_price,
                stop_loss,
                take_profit,
                pip_size,
            )
            resp = self._request("POST", f"/v3/accounts/{self.oanda_account_id}/orders", payload=order)
            summary = self.interpret_order_response(resp)

            if summary["rejected"]:
                logging.warning("Order rejected for %s. reason=%s response=%s", oanda_symbol, summary["reason"], resp)
                return

            if summary["immediately_canceled"]:
                logging.warning("Order canceled immediately for %s. reason=%s response=%s", oanda_symbol, summary["reason"], resp)
                return

            self.active_symbols.add(oanda_symbol)
            record_trade(
                {
                    "timestamp": datetime.utcnow().isoformat(),
                    "symbol": oanda_symbol,
                    "signal": signal,
                    "units": units,
                    "entry_price": _format_price(entry_price, price_precision),
                    "stop_loss": _format_price(stop_loss, price_precision),
                    "take_profit": _format_price(take_profit, price_precision),
                    "strategy": self.strategy_name,
                }
            )
            message = "\n".join(
                [
                    f"Signal: {self.strategy_name} ({signal})",
                    f"Symbol: {oanda_symbol}",
                    f"Order Type: {order_type}",
                    f"Units: {units}",
                    f"Entry: {_format_price(entry_price, price_precision)}",
                    f"Stop Loss: {_format_price(stop_loss, price_precision)}",
                    f"Take Profit: {_format_price(take_profit, price_precision)}",
                    f"Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}",
                ]
            )
            message_id = self.send_telegram(message)
            if message_id:
                self.order_message_map[client_extension_id] = message_id
                order_id = summary.get("order_id")
                if order_id:
                    self.order_message_map[order_id] = message_id
        except requests.RequestException as exc:
            logging.warning("Order placement failed for %s: %s", symbol, exc)

    def sync_trade_updates(self):
        try:
            open_trades_payload = self._request("GET", f"/v3/accounts/{self.oanda_account_id}/openTrades")
            closed_trades_payload = self._request(
                "GET", f"/v3/accounts/{self.oanda_account_id}/trades", params={"state": "CLOSED"}
            )
        except requests.RequestException as exc:
            logging.warning("Trade sync failed: %s", exc)
            return

        for trade in open_trades_payload.get("trades", []):
            extensions = (
                trade.get("clientExtensions")
                or trade.get("tradeClientExtensions")
                or {}
            )
            if not self._order_extensions_match(extensions):
                continue
            trade_id = trade.get("id")
            if not trade_id or trade_id in self.trade_message_map:
                continue
            extension_id = extensions.get("id")
            message_id = None
            if extension_id:
                message_id = self.order_message_map.get(extension_id)
            if not message_id:
                message_id = self.order_message_map.get(trade.get("orderID"))
            if message_id:
                self.trade_message_map[trade_id] = message_id

        for trade in closed_trades_payload.get("trades", []):
            trade_id = trade.get("id")
            if not trade_id or trade_id in self.closed_trade_notified:
                continue
            extensions = (
                trade.get("clientExtensions")
                or trade.get("tradeClientExtensions")
                or {}
            )
            if trade_id not in self.trade_message_map and not self._order_extensions_match(extensions):
                continue
            realized_pl = float(trade.get("realizedPL", 0))
            outcome = "WIN" if realized_pl >= 0 else "LOSS"
            message = "\n".join(
                [
                    f"Exit: {self.strategy_name}",
                    f"Symbol: {trade.get('instrument')}",
                    f"Units: {trade.get('currentUnits')}",
                    f"Open Time: {trade.get('openTime')}",
                    f"Close Time: {trade.get('closeTime')}",
                    f"Entry: {trade.get('price')}",
                    f"Exit Price: {trade.get('closePrice')}",
                    f"PnL: {realized_pl:.2f}",
                    f"Result: {outcome}",
                ]
            )
            reply_to = self.trade_message_map.get(trade_id)
            self.send_telegram(message, reply_to_message_id=reply_to)
            self.closed_trade_notified.add(trade_id)

    def sync_existing_positions(self):
        logging.info("Syncing existing positions")
        trades_payload = self._request("GET", f"/v3/accounts/{self.oanda_account_id}/openTrades")
        self.active_symbols = {
            trade.get("instrument")
            for trade in trades_payload.get("trades", [])
            if trade.get("instrument")
            and self._order_extensions_match(
                trade.get("clientExtensions") or trade.get("tradeClientExtensions") or {}
            )
        }
        if self.active_symbols:
            logging.info("Synced existing positions for %s symbols", len(self.active_symbols))

    def send_eod_pnl(self):
        logging.info("Checking EOD PnL")
        now = datetime.utcnow()
        today = now.date()
        if self.last_eod_date == today:
            return
        account = self._request("GET", f"/v3/accounts/{self.oanda_account_id}")
        nav = float(account["account"]["NAV"])
        closed_payload = self._request(
            "GET", f"/v3/accounts/{self.oanda_account_id}/trades", params={"state": "CLOSED"}
        )
        closed_today = []
        for trade in closed_payload.get("trades", []):
            extensions = trade.get("clientExtensions") or trade.get("tradeClientExtensions") or {}
            if not self._order_extensions_match(extensions):
                continue
            close_time = trade.get("closeTime")
            if not close_time:
                continue
            try:
                close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            except ValueError:
                continue
            if close_dt.date() == today:
                closed_today.append(trade)
        pnl = sum(float(trade.get("realizedPL", 0)) for trade in closed_today)
        pnl_percent = (pnl / nav) * 100 if nav else 0.0
        trade_count = len(closed_today)
        message = (
            f"EOD PnL for {today}: {pnl:.2f} ({pnl_percent:.2f}%). "
            f"Closed trades: {trade_count}. NAV: {nav:.2f}"
        )
        self.send_telegram(message)
        logging.info(message)
        if now.weekday() == 4 and self.last_weekly_update_date != today:
            weekly_message = (
                f"Weekly update (Friday) {today}: EOD PnL {pnl:.2f} ({pnl_percent:.2f}%). "
                f"Closed trades: {trade_count}. NAV: {nav:.2f}"
            )
            self.send_telegram(weekly_message)
            logging.info(weekly_message)
            self.last_weekly_update_date = today
        self.last_eod_date = today

    def run(self):
        logging.info("Starting Oanda trading bot...")
        if self.symbol_params:
            for symbol, params in self.symbol_params.items():
                logging.info("Params for %s: %s", symbol, params)
        try:
            while True:
                try:
                    now = datetime.utcnow()
                    if now.weekday() >= 5:
                        logging.info("Weekend detected (UTC). Trading paused until Monday.")
                        self.send_keepalive()
                        time.sleep(6 * 60 * 60)
                        continue
                    for symbol in self.symbols:
                        # Allow FX and metals (XAU/XAG). Your params file contains XAUUSD/XAGUSD.
                        if (not self.is_fx_pair(symbol)) and (not self.normalize_symbol(symbol).startswith(("XAU", "XAG"))):
                            logging.info("Skipping non-FX symbol %s", symbol)
                            continue
                        logging.info("Processing symbol %s", symbol)
                        signal, latest = self.generate_signal(symbol)
                        if signal in {"BUY", "SELL"}:
                            self.place_order(symbol, signal, latest)
                    self.sync_order_updates()
                    self.sync_trade_updates()
                    self.send_eod_pnl()
                    self.send_keepalive()
                    logging.info("Cycle complete; sleeping 60s")
                    time.sleep(60)
                except requests.RequestException as exc:
                    logging.error("Network error during trading loop: %s", exc)
                    logging.info("Retrying after 15s...")
                    time.sleep(15)
        except KeyboardInterrupt:
            logging.info("Shutting down Oanda trading bot...")


def load_symbol_params(path, top_n):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return [], {}

    assets = payload.get("assets", [])
    assets_positive = [asset for asset in assets if asset.get("net_r", 0) > 0]
    symbols = []
    params = {}
    for asset in sorted(assets_positive, key=lambda item: item.get("net_r", 0), reverse=True):
        symbol = asset["symbol"]
        if "_" in symbol and len(symbol.split("_", maxsplit=1)[0]) == 3 and len(symbol.split("_", maxsplit=1)[1]) == 3:
            symbols.append(symbol)
            params[symbol] = asset.get("params", {})
        elif len(symbol) == 6 and symbol.isalpha():
            symbols.append(symbol)
            params[symbol] = asset.get("params", {})
        else:
            logging.info("Skipping non-FX symbol from params: %s", symbol)
        if top_n and len(symbols) >= top_n:
            break
    return symbols, params


if __name__ == "__main__":
    symbol_params_path = "backtest_outputs/symbol_params_latest.json"
    top_assets_to_trade = 10
    symbols, symbol_params = load_symbol_params(symbol_params_path, top_assets_to_trade)
    if not symbols:
        logging.error("No FX symbols loaded from %s. Update symbol params before trading.", symbol_params_path)
    else:
        bot = OandaAdvancedTradingBot(symbols=symbols, risk_percent=1, symbol_params=symbol_params)
        bot.run()
