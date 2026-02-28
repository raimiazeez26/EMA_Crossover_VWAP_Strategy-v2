import MetaTrader5 as mt5
import pandas as pd
import talib
import random
import numpy as np
from datetime import datetime, timedelta
import time
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('mt5_trading.log'), logging.StreamHandler()]
)


class MT5AdvancedTradingBot:
    def __init__(self, symbols, risk_percent=1, pip_values=None, atr_multiplier=1.5, rr_ratio=2):
        self.symbols = symbols
        self.risk_percent = risk_percent
        self.pip_values = pip_values or {sym: 0.0001 for sym in symbols}
        self.atr_multiplier = atr_multiplier
        self.rr_ratio = rr_ratio
        self.signal_history = {sym: {'last': 'NEUTRAL', 'current': 'NEUTRAL'} for sym in symbols}
        self.active_positions = {}

        if not mt5.initialize():
            logging.error("MT5 initialization failed")
            raise ConnectionError("Failed to connect to MT5")
        logging.info(f"Connected to MT5 version {mt5.version()}")

    def fetch_data(self, symbol, timeframe=mt5.TIMEFRAME_H1, count=500):
        try:
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
            df = pd.DataFrame(rates)
            if df.empty:
                return None
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
            return df
        except Exception as e:
            logging.error(f"Data fetch failed for {symbol}: {str(e)}")
            return None

    def calculate_indicators(self, df, symbol):
        try:
            df = df.copy()
            # EMA Calculations
            df['EMA21'] = talib.EMA(df['close'], 21)
            df['EMA13'] = talib.EMA(df['close'], 13)
            df['EMA8'] = talib.EMA(df['close'], 8)

            # VWAP Calculation
            typical_price = (df['high'] + df['low'] + df['close']) / 3
            cumulative_tp = (typical_price * df['tick_volume']).cumsum()
            cumulative_volume = df['tick_volume'].cumsum()
            df['VWAP'] = cumulative_tp / cumulative_volume

            # Momentum Indicators
            df['RSI'] = talib.RSI(df['close'], 14)
            df['ROC'] = talib.ROC(df['close'], 3)

            # Volatility Measures
            df['ATR'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=5)

            # Swing Points
            df['swing_high'] = df['high'].rolling(5).max()
            df['swing_low'] = df['low'].rolling(5).min()

            return df.dropna()
        except Exception as e:
            logging.error(f"Indicator calculation failed for {symbol}: {str(e)}")
            return None

    def generate_signal(self, symbol):
        h1_df = self.calculate_indicators(self.fetch_data(symbol, mt5.TIMEFRAME_H1), symbol)
        m5_df = self.calculate_indicators(self.fetch_data(symbol, mt5.TIMEFRAME_M5), symbol)

        if h1_df is None or m5_df is None:
            return 'NEUTRAL'

        latest_h1 = h1_df.iloc[-1]
        latest_m5 = m5_df.iloc[-1]

        # Trend Direction (H1)
        bullish_trend = latest_h1['EMA21'] > latest_h1['EMA13'] > latest_h1['EMA8']
        bearish_trend = latest_h1['EMA21'] < latest_h1['EMA13'] < latest_h1['EMA8']

        # Entry Conditions (M5)
        entry_conditions = {
            'BUY': (
                    bullish_trend and
                    latest_m5['close'] > latest_m5['VWAP'] and
                    latest_m5['ROC'] > 0 and
                    latest_m5['RSI'] < 70
            ),
            'SELL': (
                    bearish_trend and
                    latest_m5['close'] < latest_m5['VWAP'] and
                    latest_m5['ROC'] < 0 and
                    latest_m5['RSI'] > 30
            )
        }

        if entry_conditions['BUY']:
            return 'BUY'
        elif entry_conditions['SELL']:
            return 'SELL'
        return 'NEUTRAL'

    def calculate_position_size(self, symbol, atr_value):
        account_info = mt5.account_info()
        if not account_info:
            return 0

        balance = account_info.balance
        risk_amount = balance * (self.risk_percent / 100)
        point_value = 1 / mt5.symbol_info(symbol).point
        risk_points = atr_value * self.atr_multiplier * point_value
        lot_size = round(risk_amount / (risk_points * mt5.symbol_info(symbol).trade_tick_value), 2)
        return max(lot_size, 0.01)

    def place_pending_order(self, symbol, signal_type):
        m5_df = self.calculate_indicators(self.fetch_data(symbol, mt5.TIMEFRAME_M5), symbol)
        if m5_df is None:
            return

        latest = m5_df.iloc[-1]
        pip_value = self.pip_values[symbol]
        atr_value = latest['ATR']

        if signal_type == 'BUY':
            entry_price = latest['swing_high'] + 3 * pip_value
            stop_loss = entry_price - (atr_value * self.atr_multiplier)
            take_profit = entry_price + (atr_value * self.atr_multiplier * self.rr_ratio)
            order_type = mt5.ORDER_TYPE_BUY_STOP
        else:
            entry_price = latest['swing_low'] - 3 * pip_value
            stop_loss = entry_price + (atr_value * self.atr_multiplier)
            take_profit = entry_price - (atr_value * self.atr_multiplier * self.rr_ratio)
            order_type = mt5.ORDER_TYPE_SELL_STOP

        lot_size = self.calculate_position_size(symbol, atr_value)
        print(f"Lot Size: {lot_size}")

        lot_size = max(0.01, min(lot_size, 50.0))
        print(f"Lot Size: {lot_size}")

        if lot_size <= 0:
            return

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": lot_size,
            "type": order_type,
            "price": entry_price,
            "sl": stop_loss,
            "tp": take_profit,
            "deviation": 20,
            "magic": 100,
            "comment": "Strategy Entry",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        print(f"Request: {request}")

        result = mt5.order_send(request)
        print(f"Result: {result}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Order failed for {symbol}: {result.comment}")
        else:
            logging.info(f"Pending order placed for {symbol} at {entry_price}")
            self.active_positions[result.order] = {
                'symbol': symbol,
                'type': signal_type,
                'sl': stop_loss,
                'atr': atr_value
            }

    def trail_stop_loss(self):
        for ticket in list(self.active_positions.keys()):
            position = mt5.positions_get(ticket=ticket)
            if not position:
                del self.active_positions[ticket]
                continue

            pos_info = self.active_positions[ticket]
            symbol = pos_info['symbol']
            current_price = mt5.symbol_info_tick(symbol).ask if pos_info['type'] == 'BUY' else mt5.symbol_info_tick(
                symbol).bid
            atr_value = pos_info['atr']

            if pos_info['type'] == 'BUY':
                new_sl = current_price - (atr_value * self.atr_multiplier)
                if new_sl > pos_info['sl']:
                    modify_request = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": ticket,
                        "sl": new_sl,
                        "tp": position.tp,
                        "deviation": 20,
                    }
                    mt5.order_send(modify_request)
                    self.active_positions[ticket]['sl'] = new_sl
            else:
                new_sl = current_price + (atr_value * self.atr_multiplier)
                if new_sl < pos_info['sl']:
                    modify_request = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": ticket,
                        "sl": new_sl,
                        "tp": position.tp,
                        "deviation": 20,
                    }
                    mt5.order_send(modify_request)
                    self.active_positions[ticket]['sl'] = new_sl

    def process_symbol(self, symbol):
        print(f"Last signal: {self.signal_history[symbol]['last']}")
        current_signal = random.choice(["BUY", "SELL"]) #self.generate_signal(symbol)
        print(f"Current Signal: {current_signal}")
        self.signal_history[symbol]['current'] = current_signal

        if self.signal_history[symbol]['last'] == 'NEUTRAL' and current_signal in ['BUY', 'SELL']:
            self.place_pending_order(symbol, current_signal)
            self.signal_history[symbol]['last'] = current_signal

    def run(self):
        logging.info("Starting advanced trading bot...")
        try:
            while True:
                for symbol in self.symbols:
                    self.process_symbol(symbol)
                self.trail_stop_loss()
                time.sleep(60)
        except KeyboardInterrupt:
            logging.info("Shutting down trading bot...")
        finally:
            mt5.shutdown()


if __name__ == "__main__":
    symbols_config = {
        "EURUSD": 0.0001,
        "GBPUSD": 0.0001,
        "XAUUSD": 0.1,
        "USDJPY": 0.01
    }

    bot = MT5AdvancedTradingBot(
        symbols=list(symbols_config.keys()),
        risk_percent=1,
        pip_values=symbols_config,
        atr_multiplier=1.5,
        rr_ratio=2
    )
    bot.run()