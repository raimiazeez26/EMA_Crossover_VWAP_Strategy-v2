from trading_engine import AlpacaTradingBot  # Import the trading engine

symbol = "AAOI"
signal = {'direction': 'NEUTRAL', 'entry': 12.9353, 'stop_loss': None,
          'take_profit': None, 'position_size': None, 'timestamp': '2025-04-04T10:48:20.066002'}


bot = AlpacaTradingBot()
bot.send_telegram_alert(symbol, signal)
