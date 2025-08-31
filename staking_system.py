# staking_system.py
import random
from datetime import datetime, timedelta

class SmartStaking:
    def __init__(self):
        self.min_stake = 10
        
    def calculate_daily_reward(self, strategy_id, staked_amount):
        """Розрахунок щоденної винагороди"""
        base_rate = 0.008
        performance_boost = random.uniform(0.001, 0.005)
        risk_adjustment = 0.9 if strategy_id % 3 == 0 else 1.0
        
        daily_reward = staked_amount * (base_rate + performance_boost) * risk_adjustment
        return round(daily_reward, 2)
    
    def get_strategy_performance(self, strategy_id):
        """Симуляція продуктивності стратегії"""
        performances = {
            1: 0.234,  # 23.4%
            2: 0.312,  # 31.2%
            3: 0.287   # 28.7%
        }
        return performances.get(strategy_id, 0.2)