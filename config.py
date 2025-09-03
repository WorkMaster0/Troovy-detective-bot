# Конфігурація бота
WHITELIST_USERS = [
    123456789,  # Ваш Telegram ID
    987654321   # Додаткові користувачі
]

EXCHANGE_CONFIG = {
    'binance': {
        'api_key': None,
        'api_secret': None,
        'testnet': False
    },
    'kraken': {
        'api_key': None,
        'api_secret': None
    },
    'coinbase': {
        'api_key': None,
        'api_secret': None
    }
}

TRADING_CONFIG = {
    'min_profit_percent': 0.3,
    'max_trade_size': 5.0,
    'risk_per_trade': 0.02
}