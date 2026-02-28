
import MetaTrader5 as mt5
import pandas as pd
import talib
import random
import numpy as np
from datetime import datetime, timedelta
import time
import logging
from scipy.signal import argrelextrema

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
            df['EMA21'] = talib.EMA(df['close'], 21)
            df['EMA13'] = talib.EMA(df['close'], 13)
            df['EMA8'] = talib.EMA(df['close'], 8)

            typical_price = (df['high'] + df['low'] + df['close']) / 3
            cumulative_tp = (typical_price * df['tick_volume']).cumsum()
            cumulative_volume = df['tick_volume'].cumsum()
            df['VWAP'] = cumulative_tp / cumulative_volume

            df['RSI'] = talib.RSI(df['close'], 14)
            df['ROC'] = talib.ROC(df['close'], 3)
            df['ATR'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=5)

            return df.dropna()
        except Exception as e:
            logging.error(f"Indicator calculation failed for {symbol}: {str(e)}")
            return None

    def get_last_swing(self, df, side='high', order=5):
        arr = df[side].values
        extrema = argrelextrema(arr, np.greater if side == 'high' else np.less, order=order)[0]
        if len(extrema) == 0:
            return None
        return df.iloc[extrema[-1]][side]

    def generate_signal(self, symbol):
        h1_df = self.calculate_indicators(self.fetch_data(symbol, mt5.TIMEFRAME_H1), symbol)
        m5_df = self.calculate_indicators(self.fetch_data(symbol, mt5.TIMEFRAME_M5), symbol)

        if h1_df is None or m5_df is None:
            return 'NEUTRAL'

        latest_h1 = h1_df.iloc[-2]
        latest_m5 = m5_df.iloc[-2]

        # Trend Direction (H1)
        bearish_trend = latest_h1['EMA21'] > latest_h1['EMA13'] > latest_h1['EMA8']
        bullish_trend = latest_h1['EMA21'] < latest_h1['EMA13'] < latest_h1['EMA8']

        # Trend Direction (M5)
        bearish_trend_m5 = latest_m5['EMA21'] > latest_m5['EMA13'] > latest_m5['EMA8']
        bullish_trend_m5 = latest_m5['EMA21'] < latest_m5['EMA13'] < latest_m5['EMA8']

        entry_conditions = {
            'BUY': (
                    bullish_trend and bullish_trend_m5 and
                    latest_m5['close'] > latest_m5['VWAP'] and
                    latest_m5['ROC'] > 0 and
                    latest_m5['RSI'] < 70
            ),
            'SELL': (
                    bearish_trend and bearish_trend_m5 and
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
        atr_value = latest['ATR']
        digits = mt5.symbol_info(symbol).digits
        point = mt5.symbol_info(symbol).point
        point_per_pip = 10 if digits in [3, 5] else 1
        pip_range = 2 #random.randint(2, 4)
        pip_distance = pip_range * point_per_pip * point

        swing_high = self.get_last_swing(m5_df, 'high')
        swing_low = self.get_last_swing(m5_df, 'low')

        if swing_high is None or swing_low is None:
            return

        if signal_type == 'BUY':
            entry_price = swing_high + pip_distance
            stop_loss = swing_low - pip_distance  # entry_price - (atr_value * self.atr_multiplier)
            take_profit = entry_price + (entry_price - stop_loss) * self.rr_ratio
            order_type = mt5.ORDER_TYPE_BUY_STOP
        else:
            entry_price = swing_low - pip_distance
            stop_loss = swing_high + pip_distance # entry_price + (atr_value * self.atr_multiplier)
            take_profit = entry_price - (stop_loss - entry_price) * self.rr_ratio
            order_type = mt5.ORDER_TYPE_SELL_STOP

        lot_size = self.calculate_position_size(symbol, atr_value)
        lot_size = max(0.01, min(lot_size, 50.0))
        if lot_size <= 0:
            return

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": lot_size,
            "type": order_type,
            "price": round(entry_price, digits),
            "sl": round(stop_loss, digits),
            "tp": round(take_profit, digits),
            "deviation": 20,
            "magic": 100,
            "comment": "WMA VWAP",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        # print(f"Pending order placed: {result}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Order failed for {symbol}: {result.comment}")
        else:
            logging.info(f"Pending order placed for {symbol} at {entry_price}")
            self.active_positions[result.order] = {
                'symbol': symbol,
                'type': signal_type,
                'sl': stop_loss,
                'initial_sl': stop_loss,
                'atr': atr_value,
                'entry_price': entry_price,
                'trail_step': 0
            }

    def trail_stop_loss(self):
        print("Trailing...")
        all_positions = mt5.positions_get()
        for position in all_positions:
            ticket = position.ticket
            # for ticket in list(self.active_positions.keys()):
            position_info = mt5.positions_get(ticket=ticket)
            # print(f"Position Info: {position_info}")

            for order_id in list(self.active_positions.keys()):
                if self.active_positions[order_id]["symbol"] == position.symbol:
                    self.active_positions[ticket] = self.active_positions[order_id]
                    # print(f"Position Info for open position {ticket}: {position_info}")

            if not position_info:
                del self.active_positions[ticket]
                continue

            # position = position_info[0]
            symbol = position.symbol
            side = position.type  # 0 = BUY, 1 = SELL
            entry_price = position.price_open
            sl = position.sl
            tp = position.tp
            current_price = mt5.symbol_info_tick(symbol).ask if side == 0 else mt5.symbol_info_tick(symbol).bid
            direction = 1 if side == 0 else -1
            initial_risk = abs(entry_price - self.active_positions[ticket]["initial_sl"])
            current_profit = (current_price - entry_price) * direction
            # print(f"Current Profit for {ticket}: {current_profit}")

            rr = current_profit / initial_risk if initial_risk != 0 else 0
            print(f"RR {symbol}: {rr}")

            # Determine new SL based on RR levels
            if rr >= 1.0:
                if rr < 1.5:
                    new_sl = entry_price  # Break even
                    print(f"{symbol}: BREAKING EVEN!")
                else:
                    # Trail for every 0.5R beyond 1R
                    trail_profit = (rr - 1.0)
                    trail_levels = int(trail_profit / 0.5)
                    new_sl = entry_price + direction * (0.5 * trail_levels * initial_risk)
                    tp = None
                    print(f"{symbol}: TRAILING SL TO {new_sl}")

                print("CHECKING CONDITION FOR PLACING BREAK EVEN OR TRAILING ORDER")
                # Only update if SL improves
                if (direction == 1 and new_sl > sl) or (direction == -1 and new_sl < sl):
                    print(f"{symbol}: PLACING BREAK EVEN OR TRAILING ORDER")
                    modify_request = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": ticket,
                        "sl": round(new_sl, mt5.symbol_info(symbol).digits),
                        "tp": tp,
                        "deviation": 20,
                    }
                    result = mt5.order_send(modify_request)
                    if result.retcode == mt5.TRADE_RETCODE_DONE:
                        self.active_positions[ticket]['sl'] = new_sl
                        logging.info(f"Trailing SL updated for {symbol}: {new_sl} (RR: {rr:.2f})")

    def process_symbol(self, symbol):
        current_signal = self.generate_signal(symbol)
        # print(f"Current Signal  {symbol}: {current_signal}")

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


def detect_pip_values(symbols):
    pip_values = {}
    for symbol in symbols:
        info = mt5.symbol_info(symbol)
        if info is None:
            pip_values[symbol] = 0.0001
            continue
        digits = info.digits
        if digits == 5:
            pip_values[symbol] = 0.0001
        elif digits == 3:
            pip_values[symbol] = 0.01
        elif digits == 2:
            pip_values[symbol] = 0.1
        else:
            pip_values[symbol] = 0.0001
    return pip_values


if __name__ == "__main__":
    symbols = ['EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'USDCAD', 'NZDUSD', 'EURGBP', 'EURJPY', 'EURCHF',
               'EURAUD', 'GBPJPY', 'GBPCHF', 'GBPAUD', 'GBPCAD', 'GBPNZD', 'AUDJPY', 'AUDNZD', 'AUDCAD', 'AUDCHF',
               'CADJPY', 'CHFJPY', 'NZDJPY', 'NZDCAD', 'NZDCHF', 'XAUUSD', 'XAGUSD', 'EURCAD']

    if not mt5.initialize():
        raise RuntimeError("MT5 initialization failed")

    pip_values = detect_pip_values(symbols)

    bot = MT5AdvancedTradingBot(
        symbols=symbols,
        risk_percent=1,
        pip_values=pip_values,
        atr_multiplier=3,
        rr_ratio=2
    )
    bot.run()

