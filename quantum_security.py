import hashlib
import os
import requests
from datetime import datetime
from typing import List, Dict, Any
import socket

class QuantumSecuritySystem:
    def __init__(self):
        self.encryption_key = os.getenv('ENCRYPTION_KEY', 'default_key_32bytes_long_here!')
        self.allowed_ips = self._get_allowed_ips()
    
    def _get_allowed_ips(self) -> List[str]:
        """Список дозволених IP адрес"""
        return [
            '127.0.0.1',
            '::1',
            'localhost'
        ]
    
    async def generate_quantum_signature(self, data: Any) -> str:
        """Генерація квантового підпису"""
        timestamp = int(datetime.now().timestamp() * 1000)
        data_str = f"{data}{timestamp}{self.encryption_key}"
        return hashlib.sha3_256(data_str.encode()).hexdigest()
    
    async def validate_request(self, request_ip: str) -> bool:
        """Перевірка IP адреси"""
        if os.getenv('TEST_MODE', 'false').lower() == 'true':
            return True
        return request_ip in self.allowed_ips
    
    async def get_current_ip(self) -> str:
        """Отримати публічну IP адресу сервера"""
        try:
            response = requests.get('https://api.ipify.org', timeout=5)
            return response.text.strip()
        except:
            return "unknown"
    
    async def verify_kraken_ip(self, client_ip: str) -> bool:
        """Перевірка, що IP належить Kraken"""
        kraken_ips = [
            '52.89.214.238',
            '34.212.75.30', 
            '54.218.53.128',
            '52.32.178.7'
        ]
        return client_ip in kraken_ips
    
    async def get_system_status(self) -> Dict[str, Any]:
        """Статус системи"""
        current_ip = await self.get_current_ip()
        
        return {
            'exchange_connections': 0,
            'response_time': 100,
            'active_strategies': 0,
            'quantum_level': int(os.getenv('QUANTUM_LEVEL', 9)),
            'current_ip': current_ip,
            'security_level': 'high'
        }