# staking_system.py
import random
import numpy as np
from datetime import datetime, timedelta

class SmartStaking:
    def __init__(self):
        self.min_stake = 10  # Мінімальний стейк $10
        
    def calculate_daily_reward(self, strategy_id, staked_amount):
        """Розрахунок щоденної винагороди"""
        base_rate = 0.008  # 0.8% в день базово
        performance_boost = self.get_strategy_performance(strategy_id)
        risk_adjustment = self.get_risk_adjustment(strategy_id)
        
        daily_reward = staked_amount * (base_rate + performance_boost) * risk_adjustment
        return max(daily_reward, 0)
    
    def get_strategy_performance(self, strategy_id):
        """Отримання performance стратегії"""
        # Тут буде реальна логіка з БД
        return random.uniform(0.001, 0.005)  # 0.1%-0.5% бонус