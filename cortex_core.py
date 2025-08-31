# cortex_core.py
import sqlite3
import json
import hashlib
from datetime import datetime

class CortexCore:
    def __init__(self):
        self.db = self.init_database()
        
    def init_database(self):
        """Ініціалізація бази даних"""
        conn = sqlite3.connect('cortex_ecosystem.db')
        cursor = conn.cursor()
        
        # Таблиця користувачів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE,
                balance REAL DEFAULT 0,
                risk_level INTEGER DEFAULT 2,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблиця стратегій
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategies (
                id INTEGER PRIMARY KEY,
                creator_id INTEGER,
                name TEXT,
                description TEXT,
                performance REAL DEFAULT 0,
                risk_level INTEGER,
                is_public BOOLEAN DEFAULT TRUE,
                staking_amount REAL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (creator_id) REFERENCES users (id)
            )
        ''')
        
        # Таблиця підписок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                strategy_id INTEGER,
                staked_amount REAL,
                start_date DATETIME,
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (strategy_id) REFERENCES strategies (id)
            )
        ''')
        
        conn.commit()
        return conn