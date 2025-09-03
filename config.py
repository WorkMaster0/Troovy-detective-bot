# config.py
WHITELIST_USERS = [
    123456789,  # Ваш Telegram ID
]

EXCHANGE_CONFIG = {
    'kraken': {
        'api_key': os.getenv('KRAKEN_API_KEY'),
        'api_secret': os.getenv('KRAKEN_API_SECRET'),
        'enabled': True
    },
    'binance': {
        'enabled': False  # Вимикаємо Binance
    },
    'coinbase': {
        'enabled': False  # Вимикаємо Coinbase
    }
}

TRADING_CONFIG = {
    'min_profit_percent': 0.3,
    'max_trade_size': 5.0,
    'risk_per_trade': 0.02
}