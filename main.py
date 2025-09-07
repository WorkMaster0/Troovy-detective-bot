import ccxt
import requests
import time
import os
from datetime import datetime, timedelta
from flask import Flask, request
import telebot
import threading
import json
import math
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from typing import List, Tuple, Dict, Any
import talib
import logging
from decimal import Decimal, ROUND_DOWN

# -------------------------
# Налаштування логування
# -------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

# -------------------------
# Налаштування через environment variables
# -------------------------
API_KEY_TELEGRAM = os.getenv("API_KEY_TELEGRAM")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

GATE_API_KEY = os.getenv("GATE_API_KEY")
GATE_API_SECRET = os.getenv("GATE_API_SECRET")

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", 10))
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", 1.0))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
LEVERAGE = int(os.getenv("LEVERAGE", 20))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", 5))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", 10.0))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", 0.02))  # 2% риску на угоду

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# -------------------------
# РОЗШИРЕНІ НАЛАШТУВАННЯ
# -------------------------
AI_ENABLED = os.getenv("AI_ENABLED", "true").lower() == "true"
DYNAMIC_LEVERAGE = os.getenv("DYNAMIC_LEVERAGE", "true").lower() == "true"
AUTO_HEDGING = os.getenv("AUTO_HEDGING", "true").lower() == "true"
SENTIMENT_ANALYSIS = os.getenv("SENTIMENT_ANALYSIS", "true").lower() == "true"

# Словник для ML моделей
ml_models = {}
market_signals = {}
sentiment_data = {}

# -------------------------
# ІНІЦІАЛІЗАЦІЯ БІРЖІ
# -------------------------
try:
    exchange = ccxt.gateio({
        "apiKey": GATE_API_KEY,
        "secret": GATE_API_SECRET,
        "options": {"defaultType": "swap"},
        "enableRateLimit": True
    })
    exchange.load_markets()
    logger.info("✅ Успішно підключено до Gate.io Futures")
except Exception as e:
    logger.error(f"❌ Помилка підключення: {e}")
    exchange = None

active_positions = {}
trade_history = []
profit_loss = 0.0
token_blacklist = set()
portfolio_value = 0.0
performance_metrics = {
    'win_rate': 0.0,
    'sharpe_ratio': 0.0,
    'max_drawdown': 0.0,
    'profit_factor': 0.0
}

# -------------------------
# КЛАСИ ДЛЯ РОЗШИРЕНОЇ ФУНКЦІОНАЛЬНОСТІ
# -------------------------
class RiskManager:
    """Менеджер ризиків з динамічним управлінням капіталом"""
    
    def __init__(self, initial_balance: float):
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.max_drawdown = 0.0
        self.consecutive_losses = 0
        
    def calculate_position_size(self, symbol: str, entry_price: float, stop_loss: float) -> float:
        """Розрахунок розміру позиції з управлінням ризиками"""
        try:
            # Розрахунок ризику в доларах
            risk_amount = self.current_balance * RISK_PER_TRADE
            
            # Розрахунок відстані до стоп-лосу
            if stop_loss > entry_price:  # Для шортів
                risk_per_contract = entry_price - stop_loss
            else:  # Для лонгів
                risk_per_contract = stop_loss - entry_price
                
            if risk_per_contract <= 0:
                return 0
                
            # Кількість контрактів
            contracts = risk_amount / abs(risk_per_contract)
            
            # Перевірка маржинальних вимог
            market = exchange.market(f"{symbol}/USDT:USDT")
            min_amount = float(market['limits']['amount']['min'])
            
            return max(min_amount, contracts)
            
        except Exception as e:
            logger.error(f"Помилка розрахунку позиції: {e}")
            return 0
            
    def update_balance(self, pnl: float):
        """Оновлення балансу та моніторинг дроудоуну"""
        self.current_balance += pnl
        
        # Розрахунок максимального дроудоуну
        drawdown = (self.initial_balance - self.current_balance) / self.initial_balance
        self.max_drawdown = max(self.max_drawdown, drawdown)
        
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
            
    def should_reduce_risk(self) -> bool:
        """Чи потрібно зменшити ризик?"""
        return (self.max_drawdown > 0.1 or  # 10% дроудоун
                self.consecutive_losses >= 3)  # 3 поспіль програші

class AITrader:
    """AI модуль для прогнозування цін та сигналів"""
    
    def __init__(self):
        self.models = {}
        self.training_data = {}
        
    def train_model(self, symbol: str, data: pd.DataFrame):
        """Тренування ML моделі для конкретного символу"""
        try:
            # Підготовка даних
            features = self._create_features(data)
            target = data['close'].pct_change().shift(-1)  # Ціль - наступна зміна ціни
            
            # Видалення NaN
            valid_idx = features.notna().all(axis=1) & target.notna()
            features = features[valid_idx]
            target = target[valid_idx]
            
            if len(features) < 100:  # Мінімум 100 точок для тренування
                return
                
            # Тренування моделі
            model = RandomForestRegressor(n_estimators=100, random_state=42)
            model.fit(features, target)
            
            self.models[symbol] = model
            logger.info(f"✅ Модель для {symbol} натренована")
            
        except Exception as e:
            logger.error(f"Помилка тренування моделі {symbol}: {e}")
            
    def predict_price_movement(self, symbol: str, data: pd.DataFrame) -> float:
        """Прогнозування руху ціни"""
        if symbol not in self.models:
            self.train_model(symbol, data)
            return 0.0
            
        try:
            features = self._create_features(data.iloc[-100:])  # Останні 100 точок
            prediction = self.models[symbol].predict(features.iloc[-1:].values.reshape(1, -1))[0]
            return prediction
            
        except Exception as e:
            logger.error(f"Помилка прогнозування {symbol}: {e}")
            return 0.0
            
    def _create_features(self, data: pd.DataFrame) -> pd.DataFrame:
        """Створення технічних індикаторів як фіч"""
        df = data.copy()
        
        # Технічні індикатори
        df['rsi'] = talib.RSI(df['close'], timeperiod=14)
        df['macd'], df['macd_signal'], _ = talib.MACD(df['close'])
        df['bb_upper'], df['bb_middle'], df['bb_lower'] = talib.BBANDS(df['close'])
        df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
        
        # Волатильність
        df['volatility'] = df['close'].pct_change().rolling(20).std()
        
        # Тренд
        df['sma_20'] = talib.SMA(df['close'], timeperiod=20)
        df['sma_50'] = talib.SMA(df['close'], timeperiod=50)
        df['ema_12'] = talib.EMA(df['close'], timeperiod=12)
        df['ema_26'] = talib.EMA(df['close'], timeperiod=26)
        
        return df[['rsi', 'macd', 'macd_signal', 'bb_upper', 'bb_lower', 
                  'atr', 'volatility', 'sma_20', 'sma_50', 'ema_12', 'ema_26']]

class SentimentAnalyzer:
    """Аналізатор ринкових настроїв"""
    
    def __init__(self):
        self.sources = [
            "https://api.alternative.me/fng/",  # Fear & Greed Index
            "https://api.coingecko.com/api/v3/search/trending",
            "https://news.bitcoin.com/wp-json/wp/v2/posts?per_page=5"
        ]
        
    def get_market_sentiment(self) -> Dict[str, float]:
        """Отримання ринкових настроїв з різних джерел"""
        sentiment_scores = {}
        
        try:
            # Fear & Greed Index
            response = requests.get(self.sources[0], timeout=10)
            if response.status_code == 200:
                data = response.json()
                sentiment_scores['fear_greed'] = float(data['data'][0]['value']) / 100
                
            # Trending coins
            response = requests.get(self.sources[1], timeout=10)
            if response.status_code == 200:
                data = response.json()
                sentiment_scores['trending'] = len(data['coins']) / 30  # Нормалізація
                
            # Crypto news
            response = requests.get(self.sources[2], timeout=10)
            if response.status_code == 200:
                data = response.json()
                sentiment_scores['news_volume'] = len(data) / 10  # Нормалізація
                
        except Exception as e:
            logger.error(f"Помилка отримання настроїв: {e}")
            
        return sentiment_scores

# Ініціалізація менеджерів
risk_manager = RiskManager(get_balance())
ai_trader = AITrader()
sentiment_analyzer = SentimentAnalyzer()

# -------------------------
# WEBHOOK ТА FLASK ФУНКЦІЇ
# -------------------------
@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'OK', 200
    return 'Bad Request', 400

@app.route('/health', methods=['GET'])
def health_check():
    return {
        'status': 'healthy', 
        'timestamp': datetime.now().isoformat(),
        'positions': len(active_positions),
        'balance': get_balance(),
        'ai_enabled': AI_ENABLED,
        'performance': performance_metrics
    }

@app.route('/stats', methods=['GET'])
def stats():
    return {
        'total_trades': len(trade_history),
        'active_positions': len(active_positions),
        'profit_loss': profit_loss,
        'blacklisted_tokens': len(token_blacklist),
        'portfolio_value': portfolio_value,
        'win_rate': performance_metrics['win_rate']
    }

@app.route('/signal/<symbol>', methods=['POST'])
def handle_signal(symbol):
    """Ендпоінт для отримання зовнішніх сигналів"""
    try:
        data = request.get_json()
        signal_type = data.get('type')
        confidence = data.get('confidence', 0.5)
        
        if signal_type in ['buy', 'sell'] and confidence > 0.7:
            market_signals[symbol.upper()] = {
                'type': signal_type,
                'confidence': confidence,
                'timestamp': datetime.now()
            }
            logger.info(f"✅ Отримано сигнал {signal_type.upper()} для {symbol}")
            
        return {'status': 'signal_received'}, 200
        
    except Exception as e:
        logger.error(f"Помилка обробки сигналу: {e}")
        return {'error': str(e)}, 400

def setup_webhook():
    """Налаштування вебхука для Telegram"""
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"✅ Вебхук налаштовано: {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"❌ Помилка налаштування вебхука: {e}")

# -------------------------
# РОЗШИРЕНІ ФУНКЦІЇ ДЛЯ РОБОТИ З БІРЖЕЮ
# -------------------------
def get_historical_data(symbol: str, timeframe: str = '1h', limit: int = 100) -> pd.DataFrame:
    """Отримання історичних даних для технічного аналізу"""
    try:
        ohlcv = exchange.fetch_ohlcv(f"{symbol}/USDT:USDT", timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df.set_index('timestamp')
    except Exception as e:
        logger.error(f"Помилка отримання історичних даних {symbol}: {e}")
        return pd.DataFrame()

def calculate_advanced_indicators(symbol: str) -> Dict[str, float]:
    """Розрахунок просунутих технічних індикаторів"""
    indicators = {}
    
    try:
        data = get_historical_data(symbol, '1h', 100)
        if data.empty:
            return indicators
            
        # RSI
        indicators['rsi'] = talib.RSI(data['close'], timeperiod=14).iloc[-1]
        
        # MACD
        macd, macd_signal, _ = talib.MACD(data['close'])
        indicators['macd'] = macd.iloc[-1]
        indicators['macd_signal'] = macd_signal.iloc[-1]
        
        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = talib.BBANDS(data['close'])
        indicators['bb_width'] = (bb_upper.iloc[-1] - bb_lower.iloc[-1]) / bb_middle.iloc[-1]
        
        # Volume analysis
        indicators['volume_ma'] = data['volume'].rolling(20).mean().iloc[-1]
        indicators['volume_ratio'] = data['volume'].iloc[-1] / indicators['volume_ma']
        
        # Volatility
        indicators['atr'] = talib.ATR(data['high'], data['low'], data['close'], timeperiod=14).iloc[-1]
        
    except Exception as e:
        logger.error(f"Помилка розрахунку індикаторів {symbol}: {e}")
        
    return indicators

def get_optimal_leverage(symbol: str, volatility: float) -> int:
    """Динамічний розрахунок оптимального плеча на основі волатильності"""
    if not DYNAMIC_LEVERAGE:
        return LEVERAGE
        
    try:
        # Високa волатильність → нижче плече
        if volatility > 0.05:  # 5% денна волатильність
            return min(5, LEVERAGE)
        elif volatility > 0.03:
            return min(10, LEVERAGE)
        elif volatility > 0.02:
            return min(15, LEVERAGE)
        else:
            return LEVERAGE
            
    except Exception as e:
        logger.error(f"Помилка розрахунку плеча: {e}")
        return LEVERAGE

# -------------------------
# ПОЛІПШЕНА АРБІТРАЖНА ЛОГІКА
# -------------------------
def find_advanced_arbitrage_opportunities() -> List[Tuple]:
    """Розширений пошук арбітражних можливостей з AI"""
    opportunities = []
    
    try:
        symbols = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOT', 'LINK', 'MATIC', 'DOGE', 
                  'BNB', 'ATOM', 'LTC', 'OP', 'ARB', 'FIL', 'APT', 'NEAR', 'ALGO', 'XLM']
        
        futures_prices = get_futures_prices(symbols)
        spot_prices = get_spot_prices(symbols)
        sentiment_scores = sentiment_analyzer.get_market_sentiment()
        
        for symbol in symbols:
            if symbol in active_positions or symbol in token_blacklist:
                continue
                
            futures_price = futures_prices.get(symbol)
            spot_price = spot_prices.get(symbol)
            
            if not futures_price or not spot_price:
                continue
                
            spread = calculate_spread(futures_price, spot_price)
            
            # Отримання AI прогнозу
            ai_score = 0
            if AI_ENABLED:
                historical_data = get_historical_data(symbol)
                if not historical_data.empty:
                    ai_score = ai_trader.predict_price_movement(symbol, historical_data)
            
            # Отримання технічних індикаторів
            indicators = calculate_advanced_indicators(symbol)
            
            # Комбінована оцінка можливості
            opportunity_score = self._calculate_opportunity_score(
                spread, ai_score, indicators, sentiment_scores
            )
            
            if opportunity_score >= 0.7:  # Високий рівень впевненості
                opportunities.append((symbol, futures_price, spot_price, spread, opportunity_score))
    
    except Exception as e:
        logger.error(f"Помилка пошуку арбітражу: {e}")
    
    return opportunities

def _calculate_opportunity_score(spread: float, ai_score: float, 
                               indicators: Dict, sentiment: Dict) -> float:
    """Розрахунок комбінованого скора можливості"""
    score = 0.0
    
    # Спред (40% ваги)
    spread_weight = 0.4
    spread_norm = min(abs(spread) / MAX_SPREAD, 1.0)
    score += spread_norm * spread_weight
    
    # AI прогноз (30% ваги)
    if AI_ENABLED:
        ai_weight = 0.3
        ai_norm = (ai_score + 1) / 2  # Нормалізація -1..1 до 0..1
        score += ai_norm * ai_weight
    
    # Технічні індикатори (20% ваги)
    tech_weight = 0.2
    if indicators:
        tech_score = 0
        if indicators.get('rsi', 50) < 30 or indicators.get('rsi', 50) > 70:
            tech_score += 0.2
        if indicators.get('macd', 0) > indicators.get('macd_signal', 0):
            tech_score += 0.2
        if indicators.get('volume_ratio', 1) > 1.5:
            tech_score += 0.2
        score += tech_score * tech_weight
    
    # Настрої (10% ваги)
    sentiment_weight = 0.1
    sentiment_score = sentiment.get('fear_greed', 0.5)
    score += sentiment_score * sentiment_weight
    
    return min(score, 1.0)

# -------------------------
# РОЗШИРЕНІ ТОРГОВІ СТРАТЕГІЇ
# -------------------------
def execute_advanced_trade(symbol: str, futures_price: float, spot_price: float, 
                         spread: float, score: float):
    """Виконання просунутої торгівлі з керуванням ризиками"""
    try:
        if len(active_positions) >= MAX_POSITIONS:
            return
            
        # Розрахунок динамічного плеча
        volatility = calculate_advanced_indicators(symbol).get('atr', 0) / futures_price
        optimal_leverage = get_optimal_leverage(symbol, volatility)
        
        # Розрахунок стоп-лосу та тейк-профіту
        stop_loss, take_profit = self._calculate_risk_levels(
            symbol, futures_price, spread, optimal_leverage
        )
        
        amount = risk_manager.calculate_position_size(symbol, futures_price, stop_loss)
        if amount <= 0:
            return
        
        futures_symbol = f"{symbol}/USDT:USDT"
        
        # Встановлення плеча
        exchange.set_leverage(optimal_leverage, futures_symbol)
        
        if spread > 0:  # Ф'ючерси дорожчі - продаємо
            order = exchange.create_market_sell_order(futures_symbol, amount)
            side = "SHORT"
            reason = "Премія ф'ючерсів + AI підтвердження"
        else:  # Ф'ючерси дешевші - купуємо
            order = exchange.create_market_buy_order(futures_symbol, amount)
            side = "LONG"
            reason = "Дисконт ф'ючерсів + AI підтвердження"
        
        # Зберігаємо розширену інформацію про trade
        trade_info = {
            'symbol': symbol,
            'side': side,
            'futures_price': futures_price,
            'spot_price': spot_price,
            'spread': spread,
            'amount': amount,
            'leverage': optimal_leverage,
            'timestamp': datetime.now(),
            'order_id': order['id'],
            'reason': reason,
            'score': score,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'volatility': volatility
        }
        trade_history.append(trade_info)
        active_positions[symbol] = trade_info
        
        # Надсилання розширеного повідомлення
        msg = self._create_trade_message(trade_info)
        bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
        
    except Exception as e:
        error_msg = f"❌ ПОМИЛКА торгівлі {symbol}: {e}"
        bot.send_message(CHAT_ID, error_msg)
        logger.error(error_msg)
        token_blacklist.add(symbol)

def _calculate_risk_levels(symbol: str, entry_price: float, spread: float, leverage: int) -> Tuple[float, float]:
    """Розрахунок рівнів стоп-лосу та тейк-профіту"""
    # Базові рівні ризику
    base_stop = 0.02  # 2% стоп
    base_take = 0.04  # 4% тейк
    
    # Коригування на основі спреду
    spread_factor = min(abs(spread) / 5.0, 2.0)  # Макс 2x множник
    
    # Коригування на основі волатильності
    indicators = calculate_advanced_indicators(symbol)
    atr_ratio = indicators.get('atr', 0) / entry_price if entry_price > 0 else 0
    volatility_factor = max(0.5, min(2.0, atr_ratio / 0.01))
    
    stop_loss = entry_price * (1 - base_stop * spread_factor * volatility_factor)
    take_profit = entry_price * (1 + base_take * spread_factor * volatility_factor)
    
    return stop_loss, take_profit

def _create_trade_message(trade_info: Dict) -> str:
    """Створення деталізованого повідомлення про торгівлю"""
    msg = f"🎯 *{trade_info['side']} {trade_info['symbol']}*\n"
    msg += f"📊 *Впевненість:* {trade_info['score']:.0%}\n"
    msg += f"💰 *Ціна:* ${trade_info['futures_price']:.6f}\n"
    msg += f"📈 *Спред:* {trade_info['spread']:+.2f}%\n"
    msg += f"⚖️ *Плече:* {trade_info['leverage']}x\n"
    msg += f"🛑 *Стоп-лос:* ${trade_info['stop_loss']:.6f}\n"
    msg += f"🎯 *Тейк-профіт:* ${trade_info['take_profit']:.6f}\n"
    msg += f"📦 *Кількість:* {trade_info['amount']:.6f}\n"
    msg += f"📊 *Волатильність:* {trade_info['volatility']:.2%}\n"
    msg += f"🆔 *Order:* {trade_info['order_id']}\n"
    msg += f"⏰ *Час:* {trade_info['timestamp'].strftime('%H:%M:%S')}"
    
    return msg

# -------------------------
# ПОЛІПШЕНИЙ МОНІТОРИНГ
# -------------------------
def advanced_monitor_positions():
    """Розширений моніторинг позицій з автоматичним керуванням"""
    while True:
        try:
            current_time = datetime.now()
            
            for symbol, position in list(active_positions.items()):
                try:
                    # Отримання поточної ціни
                    ticker = exchange.fetch_ticker(f"{symbol}/USDT:USDT")
                    current_price = ticker['last']
                    entry_price = position['futures_price']
                    
                    # Розрахунок PnL
                    if position['side'] == 'LONG':
                        pnl_percent = ((current_price - entry_price) / entry_price) * 100 * position['leverage']
                    else:
                        pnl_percent = ((entry_price - current_price) / entry_price) * 100 * position['leverage']
                    
                    # Перевірка стоп-лосу та тейк-профіту
                    should_close = False
                    close_reason = ""
                    
                    if position['side'] == 'LONG':
                        if current_price <= position['stop_loss']:
                            should_close = True
                            close_reason = "🛑 Стоп-лос"
                        elif current_price >= position['take_profit']:
                            should_close = True
                            close_reason = "🎯 Тейк-профіт"
                    else:
                        if current_price >= position['stop_loss']:
                            should_close = True
                            close_reason = "🛑 Стоп-лос"
                        elif current_price <= position['take_profit']:
                            should_close = True
                            close_reason = "🎯 Тейк-профіт"
                    
                    # Перевірка часу утримання (макс 24 години)
                    time_in_trade = current_time - position['timestamp']
                    if time_in_trade > timedelta(hours=24):
                        should_close = True
                        close_reason = "⏰ Час вийшов"
                    
                    if should_close:
                        close_position(symbol, current_price, pnl_percent, close_reason)
                        
                except Exception as e:
                    logger.error(f"Помилка моніторингу {symbol}: {e}")
            
            # Оновлення метрик продуктивності
            update_performance_metrics()
            
            time.sleep(30)
            
        except Exception as e:
            logger.error(f"Помилка моніторингу: {e}")
            time.sleep(60)

def update_performance_metrics():
    """Оновлення метрик продуктивності трейдингу"""
    global performance_metrics
    
    if not trade_history:
        return
        
    # Win Rate
    winning_trades = [t for t in trade_history if 'spread' in t and t.get('realized_pnl', 0) > 0]
    performance_metrics['win_rate'] = len(winning_trades) / len(trade_history) if trade_history else 0
    
    # Profit Factor
    total_profit = sum(t.get('realized_pnl', 0) for t in trade_history if t.get('realized_pnl', 0) > 0)
    total_loss = abs(sum(t.get('realized_pnl', 0) for t in trade_history if t.get('realized_pnl', 0) < 0))
    performance_metrics['profit_factor'] = total_profit / total_loss if total_loss > 0 else float('inf')
    
    # Оновлення Sharpe Ratio (спрощено)
    returns = [t.get('realized_pnl', 0) / TRADE_AMOUNT_USD for t in trade_history[-30:]]
    if returns:
        avg_return = np.mean(returns)
        std_return = np.std(returns)
        performance_metrics['sharpe_ratio'] = avg_return / std_return if std_return > 0 else 0

# -------------------------
# ОСНОВНИЙ ЦИКЛ З РОЗШИРЕНИМИ МОЖЛИВОСТЯМИ
# -------------------------
def start_advanced_arbitrage_bot():
    """Розширений головний цикл бота"""
    welcome_msg = "🚀 *Розширений Арбітражний Бот запущено!*\n\n"
    welcome_msg += f"🤖 *AI Модель:* {'✅' if AI_ENABLED else '❌'}\n"
    welcome_msg += f"⚖️ *Динамічне плече:* {'✅' if DYNAMIC_LEVERAGE else '❌'}\n"
    welcome_msg += f"📊 *Аналіз настроїв:* {'✅' if SENTIMENT_ANALYSIS else '❌'}\n"
    welcome_msg += f"💰 *Баланс:* ${get_balance():.2f}"
    
    bot.send_message(CHAT_ID, welcome_msg, parse_mode='Markdown')
    
    # Запуск моніторингу
    monitor_thread = threading.Thread(target=advanced_monitor_positions, daemon=True)
    monitor_thread.start()
    
    # Запуск оновлення ML моделей
    if AI_ENABLED:
        ml_update_thread = threading.Thread(target=update_ml_models, daemon=True)
        ml_update_thread.start()
    
    cycle = 0
    while True:
        cycle += 1
        
        try:
            balance = get_balance()
            logger.info(f"🔄 Цикл {cycle} | Баланс: ${balance:.2f} | Позиції: {len(active_positions)}")
            
            # Оновлення балансу в risk manager
            risk_manager.update_balance(profit_loss - risk_manager.current_balance)
            
            # Пошук можливостей
            opportunities = find_advanced_arbitrage_opportunities()
            
            if opportunities:
                logger.info(f"📊 Знайдено {len(opportunities)} арбітражів")
                
                # Сортування за впевненістю
                opportunities.sort(key=lambda x: x[4], reverse=True)
                
                for symbol, futures_price, spot_price, spread, score in opportunities:
                    if len(active_positions) < MAX_POSITIONS and not risk_manager.should_reduce_risk():
                        execute_advanced_trade(symbol, futures_price, spot_price, spread, score)
                        time.sleep(2)
            
            # Оновлення портфеля
            update_portfolio_value()
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Помилка в циклі: {e}")
            time.sleep(60)

def update_ml_models():
    """Періодичне оновлення ML моделей"""
    while True:
        try:
            symbols = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOT', 'LINK', 'MATIC', 'DOGE']
            
            for symbol in symbols:
                data = get_historical_data(symbol, '1h', 500)
                if not data.empty:
                    ai_trader.train_model(symbol, data)
            
            logger.info("✅ ML моделі оновлено")
            time.sleep(3600)  # Оновлення кожну годину
            
        except Exception as e:
            logger.error(f"Помилка оновлення ML: {e}")
            time.sleep(300)

def update_portfolio_value():
    """Оновлення загальної вартості портфеля"""
    global portfolio_value
    
    try:
        balance = get_balance()
        unrealized_pnl = 0
        
        for symbol, position in active_positions.items():
            ticker = exchange.fetch_ticker(f"{symbol}/USDT:USDT")
            current_price = ticker['last']
            entry_price = position['futures_price']
            
            if position['side'] == 'LONG':
                pnl = (current_price - entry_price) * position['amount'] * position['leverage']
            else:
                pnl = (entry_price - current_price) * position['amount'] * position['leverage']
                
            unrealized_pnl += pnl
        
        portfolio_value = balance + unrealized_pnl
        
    except Exception as e:
        logger.error(f"Помилка оновлення портфеля: {e}")

# -------------------------
# ДОДАТКОВІ TELEGRAM КОМАНДИ
# -------------------------
@bot.message_handler(commands=['ai_stats'])
def show_ai_stats(message):
    """Показати статистику AI моделей"""
    if not AI_ENABLED:
        bot.reply_to(message, "🤖 AI функціонал вимкнено")
        return
        
    msg = "🧠 *AI Статистика*\n\n"
    msg += f"📊 *Натреновано моделей:* {len(ai_trader.models)}\n"
    msg += f"📈 *Останнє оновлення:* {datetime.now().strftime('%H:%M:%S')}\n"
    
    if ai_trader.models:
        msg += "\n*Доступні моделі:*\n"
        for symbol in list(ai_trader.models.keys())[:5]:
            msg += f"• {symbol}\n"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['risk'])
def show_risk_metrics(message):
    """Показати метрики ризику"""
    msg = "⚠️ *Метрики Ризику*\n\n"
    msg += f"📉 *Макс. дроудоун:* {risk_manager.max_drawdown:.2%}\n"
    msg += f"🔴 *Послідовні втрати:* {risk_manager.consecutive_losses}\n"
    msg += f"💰 *Поточний баланс:* ${risk_manager.current_balance:.2f}\n"
    msg += f"📊 *Зменшення ризику:* {'✅' if risk_manager.should_reduce_risk() else '❌'}\n"
    msg += f"🎯 *Ризик на угоду:* {RISK_PER_TRADE:.2%}"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['portfolio'])
def show_portfolio(message):
    """Показати стан портфеля"""
    update_portfolio_value()
    
    msg = "💼 *Стан Портфеля*\n\n"
    msg += f"💰 *Загальна вартість:* ${portfolio_value:.2f}\n"
    msg += f"📊 *Активні позиції:* {len(active_positions)}\n"
    msg += f"📈 *Нереалізований PnL:* ${portfolio_value - get_balance():.2f}\n"
    msg += f"🎯 *Загальний PnL:* ${profit_loss:.2f}"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['performance'])
def show_performance(message):
    """Показати детальну статистику продуктивності"""
    update_performance_metrics()
    
    msg = "📊 *Детальна Продуктивність*\n\n"
    msg += f"🎯 *Win Rate:* {performance_metrics['win_rate']:.2%}\n"
    msg += f"📈 *Profit Factor:* {performance_metrics['profit_factor']:.2f}\n"
    msg += f"⚡ *Sharpe Ratio:* {performance_metrics['sharpe_ratio']:.2f}\n"
    msg += f"📉 *Max Drawdown:* {performance_metrics['max_drawdown']:.2%}\n"
    msg += f"🔄 *Всього угод:* {len(trade_history)}"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

# -------------------------
# ЗАПУСК СИСТЕМИ
# -------------------------
if __name__ == "__main__":
    logger.info("🚀 Запуск розширеного арбітражного бота з AI...")
    
    # Перевірка ключів
    required_keys = [API_KEY_TELEGRAM, CHAT_ID, GATE_API_KEY, GATE_API_SECRET, WEBHOOK_HOST]
    if not all(required_keys):
        logger.error("❌ Відсутні обов'язкові ключі!")
        exit(1)
    
    # Налаштування вебхука
    setup_webhook()
    
    # Запуск бота
    bot_thread = threading.Thread(target=start_advanced_arbitrage_bot, daemon=True)
    bot_thread.start()
    
    logger.info(f"✅ Бот запущено. Вебхук: {WEBHOOK_URL}")
    
    # Запуск Flask
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)