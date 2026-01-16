import MetaTrader5 as mt5
import pandas as pd
import talib
import random
import numpy as np
import time
import logging
import sys
import json


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('mt5_trading.log'), logging.StreamHandler()]
)


class MT5AdvancedTradingBot:
    def __init__(self, symbols, risk_percent=1, symbol_params=None):
        self.symbols = symbols
        self.risk_percent = risk_percent
        self.symbol_params = symbol_params or {}
        self.signal_history = {sym: {'last': 'NEUTRAL', 'current': 'NEUTRAL'} for sym in symbols}
        self.active_positions = {}
        self.active_symbols = set()
        self.comment = "EMA+VWAP"

        if not mt5.initialize():
            logging.error("MT5 initialization failed")
            raise ConnectionError("Failed to connect to MT5")
        logging.info(f"Connected to MT5 version {mt5.version()}")
        self.sync_existing_positions()

    def get_pip_value(self, symbol):
        try:
            symbol_info = mt5.symbol_info(symbol)
            if symbol_info is None:
                return 0.0001

            # Calculate pip value based on symbol properties
            pip_value = 10 * symbol_info.point if symbol_info.digits == 5 else symbol_info.point
            return pip_value
        except Exception as e:
            logging.error(f"Error getting pip value for {symbol}: {str(e)}")
            return 0.0001

    def fetch_data(self, symbol, timeframe=mt5.TIMEFRAME_H1, count=500):
        try:
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
            # print(f"symbol: {symbol} - Rates: {rates}")
            return pd.DataFrame(rates) if rates is not None else None
        except Exception as e:
            logging.error(f"Data fetch failed for {symbol}: {str(e)}")
            return None

    def calculate_indicators(self, df, symbol):
        try:
            params = self.symbol_params.get(symbol, {})
            ema_fast = params.get("ema_fast", 8)
            ema_mid = params.get("ema_mid", 13)
            ema_slow = params.get("ema_slow", 21)
            rsi_period = params.get("rsi_period", 14)
            roc_period = params.get("roc_period", 3)
            atr_period = params.get("atr_period", 5)
            swing_lookback = params.get("swing_lookback", 21)
            df = df.copy()
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)

            # EMA Calculations
            df['EMA21'] = talib.EMA(df['close'], ema_slow)
            df['EMA13'] = talib.EMA(df['close'], ema_mid)
            df['EMA8'] = talib.EMA(df['close'], ema_fast)

            # VWAP Calculation
            typical_price = (df['high'] + df['low'] + df['close']) / 3
            cumulative_tp = (typical_price * df['tick_volume']).cumsum()
            cumulative_volume = df['tick_volume'].cumsum()
            df['VWAP'] = cumulative_tp / cumulative_volume

            # Momentum and Volatility
            df['RSI'] = talib.RSI(df['close'], rsi_period)
            df['ROC'] = talib.ROC(df['close'], roc_period)
            df['ATR'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=atr_period)

            # Swing Points
            df['swing_high'] = df['high'].rolling(swing_lookback).max()
            df['swing_low'] = df['low'].rolling(swing_lookback).min()

            return df.dropna()
        except Exception as e:
            logging.error(f"Indicator calculation failed for {symbol}: {str(e)}")
            return None

    def generate_signal(self, symbol):
        params = self.symbol_params.get(symbol, {})
        rsi_overbought = params.get("rsi_overbought", 70)
        rsi_oversold = params.get("rsi_oversold", 30)
        entry_timeframe = params.get("entry_timeframe", mt5.TIMEFRAME_M5)
        trend_timeframe = params.get("trend_timeframe", mt5.TIMEFRAME_H1)
        h1_df = self.calculate_indicators(self.fetch_data(symbol, trend_timeframe), symbol)
        m5_df = self.calculate_indicators(self.fetch_data(symbol, entry_timeframe), symbol)

        if h1_df is None or m5_df is None:
            return 'NEUTRAL'

        latest_h1 = h1_df.iloc[-2]  # USE LAST CLOSED CANDLE
        latest_m5 = m5_df.iloc[-2]  # USE LAST CLOSED CANDLE
        # print(f"Latest M5: {latest_m5}")
        # print(f"Latest H1: {latest_h1}")

        # Trend Direction (H1)
        bearish_trend = latest_h1['EMA21'] > latest_h1['EMA13'] > latest_h1['EMA8']
        bullish_trend = latest_h1['EMA21'] < latest_h1['EMA13'] < latest_h1['EMA8']

        # Trend Direction (M5)
        bearish_trend_m5 = latest_m5['EMA21'] > latest_m5['EMA13'] > latest_m5['EMA8']
        bullish_trend_m5 = latest_m5['EMA21'] < latest_m5['EMA13'] < latest_m5['EMA8']

        # Entry Conditions (M5)
        entry_conditions = {
            'BUY': (
                    bullish_trend and bullish_trend_m5 and
                    latest_m5['close'] > latest_m5['VWAP'] and
                    latest_m5['ROC'] > 0 and
                    latest_m5['RSI'] < rsi_overbought
            ),
            'SELL': (
                    bearish_trend and bearish_trend_m5 and
                    latest_m5['close'] < latest_m5['VWAP'] and
                    latest_m5['ROC'] < 0 and
                    latest_m5['RSI'] > rsi_oversold
            )
        }

        if entry_conditions['BUY']:
            # print(f"Bullish Trend Data: {latest_h1}")
            return 'BUY'
        elif entry_conditions['SELL']:
            # print(f"Bearish Trend Data: {latest_h1}")
            return 'SELL'
        return 'NEUTRAL'

    def calculate_position_size(self, symbol, entry_price, stop_loss):
        account_info = mt5.account_info()
        if not account_info:
            return 0

        balance = account_info.balance
        risk_amount = balance * (self.risk_percent / 100)
        price_distance = abs(entry_price - stop_loss)

        symbol_info = mt5.symbol_info(symbol)
        tick_value = symbol_info.trade_tick_value
        tick_size = symbol_info.trade_tick_size

        if tick_value == 0 or tick_size == 0:
            return 0

        risk_points = price_distance / tick_size
        lot_size = round(risk_amount / (risk_points * tick_value), 2)
        return max(lot_size, symbol_info.volume_min)

    def has_existing_trade(self, symbol):
        if symbol in self.active_symbols:
            return True
        positions = mt5.positions_get(symbol=symbol) or []
        orders = mt5.orders_get(symbol=symbol) or []
        has_position = any(getattr(position, "comment", "") == self.comment for position in positions)
        has_order = any(getattr(order, "comment", "") == self.comment for order in orders)
        return has_position or has_order

    def place_pending_order(self, symbol, signal_type):
        if self.has_existing_trade(symbol):
            logging.info(f"Existing trade found for {symbol}, skipping new order")
            return

        params = self.symbol_params.get(symbol, {})
        entry_timeframe = params.get("entry_timeframe", mt5.TIMEFRAME_M5)
        m5_df = self.calculate_indicators(self.fetch_data(symbol, entry_timeframe), symbol)
        if m5_df is None or m5_df.empty:
            return

        latest = m5_df.iloc[-1]
        pip_value = self.get_pip_value(symbol)
        atr_multiplier = params.get("atr_multiplier", 2)
        rr_ratio = params.get("rr_ratio", 2)
        pip_buffer = params.get("pip_buffer", 3)
        atr_value = latest['ATR']

        if signal_type == 'BUY':
            entry_price = latest['swing_high'] + pip_buffer * pip_value
            stop_loss = latest['swing_low'] - (atr_value * atr_multiplier)
        else:
            entry_price = latest['swing_low'] - pip_buffer * pip_value
            stop_loss = latest['swing_high'] + (atr_value * atr_multiplier)

        # Adjust position size based on risk percentage
        lot_size = self.calculate_position_size(symbol, entry_price, stop_loss)
        if lot_size <= 0:
            return

        # Calculate take profit based on risk-reward ratio
        take_profit = entry_price + (entry_price - stop_loss) * rr_ratio if signal_type == 'BUY' \
            else entry_price - (stop_loss - entry_price) * rr_ratio

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": lot_size,
            "type": mt5.ORDER_TYPE_BUY_STOP if signal_type == 'BUY' else mt5.ORDER_TYPE_SELL_STOP,
            "price": entry_price,
            "sl": stop_loss,
            "tp": take_profit,
            "deviation": 20,
            "magic": 100,
            "comment": self.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        print(request)
        result = mt5.order_send(request)
        print(f'Result: {result}')

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logging.info(f"Pending order placed for {symbol} at {entry_price}")
            self.active_positions[result.order] = {
                'symbol': symbol,
                'type': signal_type,
                'initial_sl': stop_loss,
                'entry_price': entry_price,
                'current_rr': 0.0,
                'is_position': False  # Marks as pending order
            }

    def trail_stop_loss(self):
        # Fetch ALL open positions and pending orders
        all_positions = mt5.positions_get()
        all_orders = mt5.orders_get()
        active_symbols = set()

        # Process pending orders first
        if all_orders:
            for order in all_orders:
                if order.comment == self.comment:
                    active_symbols.add(order.symbol)
                self.check_pending_order(order.ticket, order)

        # Process open positions
        if all_positions:
            for position in all_positions:
                if position.comment == self.comment:
                    active_symbols.add(position.symbol)
                ticket = position.ticket
                # Update active_positions record
                self.active_positions[ticket] = {
                    'symbol': position.symbol,
                    'type': "BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL",
                    'initial_sl': position.sl,
                    'entry_price': position.price_open,
                    'current_rr': self.active_positions.get(ticket, {}).get('current_rr', 0.0),
                    'is_position': True
                }
                # Apply trailing logic
                self.process_position_trailing(ticket, position)
        self.active_symbols = active_symbols

    def process_position_trailing(self, ticket, position):
        pos_info = self.active_positions[ticket]
        current_price = position.price_current
        entry_price = pos_info['entry_price']  # position.price_open
        initial_sl = pos_info['initial_sl']
        initial_risk = abs(entry_price - initial_sl)
        direction = 'BUY' if position.type == mt5.ORDER_TYPE_BUY else 'SELL'

        # Calculate current RR
        if direction == 'BUY':
            profit_distance = current_price - entry_price
        else:
            profit_distance = entry_price - current_price

        print(f"Point distance: {profit_distance}")
        print(f'Initial Risk: {initial_risk}')
        rr_level = profit_distance / initial_risk
        print(f"RR Level for {pos_info['symbol']}: {rr_level}")

        if profit_distance > 0 and initial_risk > 0: #
            new_sl = None

            # Trailing logic
            if rr_level >= 1.0:
                # Break even at 1:1
                if pos_info['current_rr'] < 1.0:
                    new_sl = entry_price
                    # self.active_positions[ticket]['current_rr'] = 1.0
                    logging.info(f"Reached 1:1 RR for {position.symbol}, moving SL to breakeven")

                # Progressive trailing with 0.5 increments
                trailing_levels = [1.0 + 0.5 * n for n in range(1, 18)]  # Up to 10:1 RR
                for level in trailing_levels:
                    if rr_level >= level and pos_info['current_rr'] < level:
                        print(f"Trailing beyond 1:1. RR level: {rr_level}, Level: {level}")

                        steps = (level - 1.0) / 0.5  # Each step represents 0.5 RR
                        new_sl = entry_price + (initial_risk * 0.5 * steps) if direction == 'BUY' \
                            else entry_price - (initial_risk * 0.5 * steps)

                        # self.active_positions[ticket]['current_rr'] = level
                        logging.info(f"{position.symbol} reached 1:{level:.1f} RR, moving SL to {new_sl:.5f}")
                        break

            # Update SL if needed
            if new_sl is not None and (
                    (direction == 'BUY' and new_sl > position.sl) or
                    (direction == 'SELL' and new_sl < position.sl)
            ):
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "sl": new_sl,
                    "deviation": 20,
                }
                result = mt5.order_send(request)
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.active_positions[ticket]['current_rr'] = rr_level
                    self.active_positions[ticket]['sl'] = new_sl

    def sync_existing_positions(self):
        positions = mt5.positions_get()
        if not positions:
            return
        active_symbols = set()
        for position in positions:
            if position.comment != self.comment:
                continue
            active_symbols.add(position.symbol)
            ticket = position.ticket
            self.active_positions[ticket] = {
                'symbol': position.symbol,
                'type': "BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL",
                'initial_sl': position.sl,
                'entry_price': position.price_open,
                'current_rr': 0.0,
                'is_position': True
            }
        self.active_symbols = active_symbols
        if self.active_positions:
            logging.info("Synced %s existing positions for trailing.", len(self.active_positions))

    def check_pending_order(self, ticket, order):
        symbol = order.symbol
        current_price = mt5.symbol_info_tick(symbol).ask

        # Calculate adverse movement
        price_distance = abs(current_price - order.price_open)
        initial_risk = abs(order.price_open - order.sl)

        # Check for 1:1 adverse move
        if price_distance >= initial_risk * 0.5:
            # logging.info(f"Half initial risk: {initial_risk * 0.5}")

            # logging.info(f"Price Distance: {price_distance}")
            # logging.info(f"Adverse 1:0.5 move detected for {symbol} pending order, closing")
            mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
            del self.active_positions[ticket]

    # def calculate_dynamic_sl(self, order_type, current_price, prev_distance, risk_amount, ticket):
    #     symbol = mt5.positions_get(ticket=ticket)[0].symbol
    #     tick_value = mt5.symbol_info(symbol).trade_tick_value
    #     tick_size = mt5.symbol_info(symbol).trade_tick_size
    #
    #     if tick_value == 0 or tick_size == 0:
    #         return current_price
    #
    #     required_distance = (risk_amount / (
    #                 self.calculate_position_size(symbol, current_price, current_price) * tick_value)) * tick_size
    #     required_distance = max(required_distance, tick_size * 10)  # Minimum 10 pips
    #
    #     if order_type == 'BUY':
    #         tracking_sl = current_price - required_distance
    #         print(f"Tracking SL: {tracking_sl}")
    #         return tracking_sl
    #     else:
    #         tracking_sl = current_price + required_distance
    #         print(f"Tracking SL: {tracking_sl}")
    #         return tracking_sl

    def process_symbol(self, symbol):
        # print(f"Last signal {symbol}: {self.signal_history[symbol]['last']}")
        current_signal = self.generate_signal(symbol) #random.choice(['BUY', 'SELL']) #self.generate_signal(symbol)
        print(f"Current Signal  {symbol}: {current_signal}")

        self.signal_history[symbol]['current'] = current_signal

        if current_signal in ['BUY', 'SELL']: # self.signal_history[symbol]['last'] == 'NEUTRAL' and
            self.place_pending_order(symbol, current_signal)
            self.signal_history[symbol]['last'] = current_signal

    def run(self):
        logging.info("Starting advanced trading bot...")
        try:
            while True:
                try:
                    if not mt5.initialize():
                        logging.warning("MT5 connection lost, retrying...")
                        time.sleep(10)
                        continue
                    for symbol in self.symbols:
                        self.process_symbol(symbol)
                    self.trail_stop_loss()
                    time.sleep(60)
                    print("===========================================================")
                except Exception as e:
                    logging.warning(f"Connection or runtime issue: {e}. Retrying in 30s.")
                    time.sleep(30)
        except KeyboardInterrupt:
            logging.info("Shutting down trading bot...")
        finally:
            mt5.shutdown()

def load_symbol_params(path, top_n):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return [], {}

    assets = payload.get("assets", [])
    assets_positive = [asset for asset in assets if asset.get("net_r", 0) > 0]
    assets_sorted = sorted(assets_positive, key=lambda item: item.get("net_r", 0), reverse=True)
    top_assets = assets_sorted[:top_n] if top_n else assets_sorted
    symbols = [asset["symbol"] for asset in top_assets]
    params = {asset["symbol"]: asset.get("params", {}) for asset in top_assets}
    return symbols, params


if __name__ == "__main__":
    symbol_params_path = "backtest_outputs/symbol_params_latest.json"
    top_assets_to_trade = 10

    symbols, symbol_params = load_symbol_params(symbol_params_path, top_assets_to_trade)
    # if not symbols:
    #     symbols = [
    #         "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD",
    #         "USDCAD", "NZDUSD", "EURGBP", "EURJPY", "EURCHF",
    #         "EURAUD", "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD",
    #         "GBPNZD", "AUDJPY", "AUDNZD", "AUDCAD", "AUDCHF",
    #         "CADJPY", "CHFJPY", "NZDJPY", "NZDCAD", "NZDCHF",
    #         "XAUUSD", "XAGUSD", "EURCAD"
    #     ]
    bot = MT5AdvancedTradingBot(
        symbols=symbols,
        risk_percent=1,
        symbol_params=symbol_params
    )
    if symbol_params:
        logging.info("Loaded symbol parameters:")
        for symbol, params in symbol_params.items():
            ema_fast = params.get("ema_fast", 8)
            ema_mid = params.get("ema_mid", 13)
            ema_slow = params.get("ema_slow", 21)
            logging.info(
                "%s params: EMA %s/%s/%s, ATRx %s, RR %s, RSI %s/%s, pip_buffer %s, entry_tf %s, trend_tf %s",
                symbol,
                ema_fast,
                ema_mid,
                ema_slow,
                params.get("atr_multiplier", 2),
                params.get("rr_ratio", 2),
                params.get("rsi_overbought", 70),
                params.get("rsi_oversold", 30),
                params.get("pip_buffer", 3),
                params.get("entry_timeframe", mt5.TIMEFRAME_M5),
                params.get("trend_timeframe", mt5.TIMEFRAME_H1),
            )

    bot.run()
