import os
import time

import requests
import pandas as pd
# from alpaca.data import StockHistoricalDataClient, StockBarsRequest #, StockTickerRequest
from alpaca.data.historical import StockHistoricalDataClient

from alpaca.data.timeframe import TimeFrame
import alpaca_trade_api as tradeapi
from alpaca.trading.client import TradingClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# from alpaca.trading.enums import OrderSide, TimeInForce
from typing import List, Dict

# Modified TvDataFeed implementation
from tvDatafeed import TvDatafeed, Interval

from datetime import datetime, timedelta
import talib
import numpy as np
from dotenv import load_dotenv

load_dotenv()

class AlpacaTradingBot:
    def __init__(self, accounts, last_signal, symbol='AAPL', risk_percent=1, pip_value=0.0001):
        # self.data_client = StockHistoricalDataClient(
        #     os.getenv('ALPACA_KEY'), os.getenv('ALPACA_SECRET'))
        # self.trading_client = TradingClient(
        #     os.getenv('ALPACA_KEY'), os.getenv('ALPACA_SECRET'), paper=True)
        # # Initialize the Alpaca trade API to access positions and orders
        # self.api = tradeapi.REST(os.getenv('ALPACA_KEY'), os.getenv('ALPACA_SECRET'), os.getenv('APCA_API_BASE_URL'))

        """
           accounts: List of dicts [{
               'name': 'Account 1',
               'key': '...',
               'secret': '...',
               'base_url': '...'
           }]
           """
        self.accounts = accounts or []
        self.clients = []

        # Initialize clients for all accounts
        for acc in self.accounts:
            try:
                client = {
                    'name': acc.get('name', 'Unnamed Account'),
                    'trading': TradingClient(acc['key'], acc['secret'], paper=True),
                    'data': StockHistoricalDataClient(acc['key'], acc['secret']),
                    'rest': tradeapi.REST(acc['key'], acc['secret'],
                                          acc.get('base_url', os.getenv('APCA_API_BASE_URL')))
                }
                self.clients.append(client)
            except Exception as e:
                print(f"Error initializing account {acc.get('name')}: {e}")

        self.telegram_token = os.getenv('TELEGRAM_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.symbol = symbol
        self.risk_percent = risk_percent
        self.pip_value = pip_value
        # self.balance = self.get_account_balance()
        self.open_positions: Dict[str, dict] = {}  # Track open positions {symbol: position_data}
        self.last_signals = last_signal
        print(f"Imported last signal: {self.last_signals}")

        self.tv = TvDatafeed(
            username=os.getenv('TV_USERNAME'),
            password=os.getenv('TV_PASSWORD'),
            # auto_login=True
        )

        # Set default balance if no accounts
        # self.balance = 100000  # Default demo balance
        # if self.clients:
        #     self.balance = self.get_account_info(client)
    def get_account_info(self, client):
        """Returns (balance, account_number, market_open) for a client"""
        try:
            # Get account details using TradingClient
            account = client['trading'].get_account()
            market_status = client['rest'].get_clock().is_open
            return (
                float(account.buying_power),
                account.account_number,
                market_status
            )
        except Exception as e:
            print(f"Error getting info for {client['name']}: {e}")
            return 0, "N/A", False

    def get_latest_price(self, asset, client):
        # request - single symbol is similar
        request_params = StockLatestTradeRequest(symbol_or_symbols=[asset])  # , "GLD", "TLT"])
        quotes = client.get_stock_latest_trade(request_params)
        # print(quotes)

        # ask-buy, bid-sell
        return quotes[asset].price

    def fetch_data(self, symbol, timeframe='1H'):
        """Fetch historical data from TradingView"""
        interval_map = {
            '1H': Interval.in_1_hour,
            '5M': Interval.in_5_minute
        }

        max_retries = 3
        retry_delay = 5  # seconds between retries

        for attempt in range(max_retries):
            try:
                # Get 100 latest bars
                data = self.tv.get_hist(
                    symbol=symbol,
                    exchange='NASDAQ',
                    interval=interval_map[timeframe],
                    n_bars=100
                )

                if data is not None and not data.empty:
                    # Clean dataframe if valid data received
                    data = data.rename(columns={
                        'open': 'Open',
                        'high': 'High',
                        'low': 'Low',
                        'close': 'Close',
                        'volume': 'Volume'
                    })
                    return data

                print(f"Empty data received for {symbol}, retry {attempt + 1}/{max_retries}")

            except Exception as e:
                print(f"Data fetch failed for {symbol}: {str(e)}, retry {attempt + 1}/{max_retries}")

            if attempt < max_retries - 1:
                time.sleep(retry_delay)

        print(f"Failed to fetch data for {symbol} after {max_retries} attempts")
        return None

    def calculate_indicators(self, df):
        """Calculate technical indicators"""
        df['EMA21'] = talib.EMA(df['Close'], 21)
        df['EMA13'] = talib.EMA(df['Close'], 13)
        df['EMA8'] = talib.EMA(df['Close'], 8)

        # VWAP Calculation
        typical_price = (df['High'] + df['Low'] + df['Close']) / 3
        cumulative_tp = (typical_price * df['Volume']).cumsum()
        cumulative_volume = df['Volume'].cumsum()
        df['VWAP'] = cumulative_tp / cumulative_volume

        df['RSI'] = talib.RSI(df['Close'], 14)
        df['ROC'] = talib.ROC(df['Close'], 3)
        # print("DF from calculate_indicators")
        # print(df)
        return df

    # In generate_signal method:
    def generate_signal(self, symbol, risk_percent=1):
        """Generate entry signal without checking previous signals or sending alerts"""
        # Generate entry signal
        h1_data = self.fetch_data(symbol, '1H')
        time.sleep(30)
        m5_data = self.fetch_data(symbol, '5M')

        h1_df = self.calculate_indicators(h1_data)
        m5_df = self.calculate_indicators(m5_data)

        signal = self._generate_entry_signal(h1_df, m5_df)
        return signal

    def _generate_entry_signal(self, h1_df, m5_df):

        # Signal logic
        """Core strategy logic"""
        # Get latest values
        latest_h1 = h1_df.iloc[-1]
        latest_m5 = m5_df.iloc[-1]
        prev_m5 = m5_df.iloc[-2]

        # Trend Condition (H1)
        bullish_trend = (latest_h1['EMA21'] > latest_h1['EMA13'] > latest_h1['EMA8'])
        bearish_trend = (latest_h1['EMA21'] < latest_h1['EMA13'] < latest_h1['EMA8'])

        # Entry Conditions (M5)
        ema_bullish = (latest_m5['EMA21'] > latest_m5['EMA13'] > latest_m5['EMA8'])
        ema_bearish = (latest_m5['EMA21'] < latest_m5['EMA13'] < latest_m5['EMA8'])

        vwap_bullish = (latest_m5['Close'] > latest_m5['VWAP']) & (latest_m5['ROC'] > 0)
        vwap_bearish = (latest_m5['Close'] < latest_m5['VWAP']) & (latest_m5['ROC'] < 0)

        rsi_condition = (latest_m5['RSI'] < 65 if bullish_trend else latest_m5['RSI'] > 35)

        # Swing Point Detection (Last 5 candles)
        recent_high = m5_df['High'].rolling(5).max().iloc[-1]
        recent_low = m5_df['Low'].rolling(5).min().iloc[-1]

        # Entry Price Calculation
        buy_entry = recent_high + (3 * self.pip_value)
        sell_entry = recent_low - (3 * self.pip_value)

        # Risk Management
        # position_size = (self.balance * self.risk_percent / 100) / latest_m5['Close']

        signal = None
        if bullish_trend and ema_bullish and vwap_bullish and rsi_condition:
            stop_loss = recent_low - (5 * self.pip_value)
            take_profit = buy_entry + (2 * (buy_entry - stop_loss))
            signal = {
                'direction': 'BUY',
                'entry': round(buy_entry, 5),
                'stop_loss': round(stop_loss, 5),
                'take_profit': round(take_profit, 5),
                # 'position_size': round(position_size, 2),
                'timestamp': datetime.now().isoformat(),
                'type': 'entry'
            }
        elif bearish_trend and ema_bearish and vwap_bearish and rsi_condition:
            stop_loss = recent_high + (5 * self.pip_value)
            take_profit = sell_entry - (2 * (stop_loss - sell_entry))
            signal = {
                'direction': 'SELL',
                'entry': round(sell_entry, 5),
                'stop_loss': round(stop_loss, 5),
                'take_profit': round(take_profit, 5),
                # 'position_size': round(position_size, 2),
                'timestamp': datetime.now().isoformat(),
                'type': 'entry'
            }

        else:
            signal = {
                'direction': 'NEUTRAL',
                'entry': round(buy_entry, 5),
                'stop_loss': None,
                'take_profit': None,
                # 'position_size': None,
                'timestamp': datetime.now().isoformat(),
                'type': 'entry'
            }

        return signal

    def check_exit_conditions(self, symbol):
        """Check exit conditions for open positions"""
        exit_signals = []
        if symbol in self.open_positions:
            position = self.open_positions[symbol]
            current_data = self.fetch_data(symbol, '5M')
            current_close = current_data['Close'].iloc[-1]

            # Get current indicators
            m5_data = self.fetch_data(symbol, '5M')
            m5_df = self.calculate_indicators(m5_data)

            # Check technical exit conditions
            exit_condition = False
            pips_pnl = 0.0
            if position['direction'] == 'BUY':
                # Check for bearish crossover or price hitting SL/TP
                exit_condition = (
                        m5_df['EMA8'].iloc[-1] < m5_df['EMA13'].iloc[-1] or
                        current_close <= position['stop_loss'] or
                        current_close >= position['take_profit']
                )
                pips_pnl = (current_close - position['entry_price']) / self.pip_value
            elif position['direction'] == 'SELL':
                exit_condition = (
                        m5_df['EMA8'].iloc[-1] > m5_df['EMA13'].iloc[-1] or
                        current_close >= position['stop_loss'] or
                        current_close <= position['take_profit']
                )
                pips_pnl = (position['entry_price'] - current_close) / self.pip_value

            pips_pnl = round(pips_pnl, 2)
            if exit_condition:
                exit_signals.append({
                    'direction': f"EXIT_{position['direction']}",
                    'entry_price': position['entry_price'],
                    'exit_price': current_close,
                    'pips_pnl': pips_pnl,
                    # 'position_size': position['position_size'],
                    'timestamp': datetime.now().isoformat(),
                    'type': 'exit'
                })

        return exit_signals

    def send_telegram_alert(self, symbol, signal):
        """Send formatted alert to Telegram"""
        # print(f'Signal received: {signal}')
        if signal.get('type') == 'exit':
            direction = '🟡 EXIT ' + signal['direction'].split('_')[1]
            message = (
                f"🚨 *{symbol} Exit Signal* 🚨\n"
                f"Signal: {direction}\n"
                f"Entry Price: {signal['entry_price']}\n"
                f"Exit Price: {signal['exit_price']}\n"
                f"Pips PnL: {signal['pips_pnl']}\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "Type: Exit"
            )

        elif signal.get('type') == 'entry':

            if signal['direction'] == "BUY":
                direction = '🟢 BUY'
            elif signal['direction'] == "SELL":
                direction = '🔴 SELL'
            else:
                direction = 'NEUTRAL'

            message = (
                f"🚀 *{symbol} Trading Signal* 🚨\n"
                f"Direction: {direction}\n"
                f"Entry: {signal['entry']}\n"
                f"SL: {signal['stop_loss']} | TP: {signal['take_profit']}\n"
                # f"Size: {signal['position_size']} shares\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "Type: Entry"
            )

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        params = {
            'chat_id': self.telegram_chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        send_msg = requests.post(url, params=params)
        print(send_msg)

    # def execute_order(self, symbol, signal):
    #     """Execute on all accounts"""
    #     for client in self.clients:
    #         try:
    #             if 'EXIT' in signal['direction']:
    #                 client['trading'].close_position(symbol)
    #             else:
    #                 order_data = MarketOrderRequest(
    #                     symbol=symbol,
    #                     qty=signal['position_size'],
    #                     side=OrderSide.BUY if signal['direction'] == 'BUY' else OrderSide.SELL,
    #                     time_in_force=TimeInForce.GTC
    #                 )
    #                 client['trading'].submit_order(order_data)
    #         except Exception as e:
    #             print(f"Error executing on {client['name']}: {e}")

    def execute_trade(self, client, symbol, qty, side):
        """Execute trade for a specific client"""
        try:
            balance, acct_num, market_open = self.get_account_info(client)
            latest_price = self.get_latest_price(symbol, client['data'])
            position_size = float((balance * self.risk_percent / 100) / latest_price)

            # preparing market order
            market_order_data = MarketOrderRequest(
                symbol=symbol,
                qty=position_size,
                side=side.lower(),
                time_in_force='day'
            )
            # Market order
            order = client['trading'].submit_order(
                order_data=market_order_data
            )
            return order
        except Exception as e:
            print(f"Trade failed for {client['name']}: {e}")
            return None

    def close_position(self, client, symbol):
        """Close position for a specific client"""
        try:
            position = client['rest'].get_position(symbol)
            if position:
                # preparing market order
                market_order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=int(position.qty),
                    side='sell' if position.side == 'long' else 'buy',
                    time_in_force='day'
                )
                # Market order
                order = client['trading'].submit_order(
                    order_data=market_order_data
                )
                return order
            return None
        except Exception as e:
            print(f"Position close failed for {client['name']}: {e}")
            return None

    def market_is_open(self):
        """Check if market is open for all configured accounts"""
        if not self.clients:
            return False  # No accounts configured

        for client in self.clients:
            try:
                clock = client['trading'].get_clock()
                if not clock.is_open:
                    return False
            except Exception as e:
                print(f"Error checking market status for {client.get('name', 'unnamed account')}: {e}")
                return False  # Treat connection errors as market closed
        return True

#
# if __name__ == "__main__":
#     SYMBOL = 'AAPL'
#
#     bot = AlpacaTradingBot()
#     bot.generate_signal('AAPL', risk_percent=1)