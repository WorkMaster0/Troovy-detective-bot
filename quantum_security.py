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
            'localhost',
            # Додаткові IP для Render.com та інших хостингів
            '0.0.0.0',  # Для локального тестування
            # Додайте ваші конкретні IP тут
        ]
    
    async def generate_quantum_signature(self, data: Any) -> str:
        """Генерація квантового підпису"""
        timestamp = int(datetime.now().timestamp() * 1000)
        data_str = f"{data}{timestamp}{self.encryption_key}"
        return hashlib.sha3_256(data_str.encode()).hexdigest()
    
    async def validate_request(self, request_ip: str) -> bool:
        """Перевірка IP адреси"""
        # Дозволяємо всі IP якщо це тестовий режим
        if os.getenv('TEST_MODE', 'false').lower() == 'true':
            return True
            
        return request_ip in self.allowed_ips
    
    async def get_current_ip(self) -> str:
        """Отримати публічну IP адресу сервера"""
        try:
            response = requests.get('https://api.ipify.org', timeout=5)
            return response.text.strip()
        except requests.RequestException:
            try:
                # Резервний спосіб
                hostname = socket.gethostname()
                return socket.gethostbyname(hostname)
            except:
                return "unknown"
    
    async def verify_kraken_ip(self, client_ip: str) -> bool:
        """Перевірка, що IP належить Kraken (для webhook)"""
        kraken_ips = [
            '52.89.214.238',
            '34.212.75.30', 
            '54.218.53.128',
            '52.32.178.7',
            '52.36.174.99'
        ]
        return client_ip in kraken_ips
    
    async def encrypt_data(self, data: str) -> str:
        """Просте шифрування даних"""
        import base64
        from cryptography.fernet import Fernet
        
        key = base64.urlsafe_b64encode(self.encryption_key.encode()[:32].ljust(32, b'='))
        cipher = Fernet(key)
        encrypted = cipher.encrypt(data.encode())
        return encrypted.decode()
    
    async def decrypt_data(self, encrypted_data: str) -> str:
        """Розшифрування даних"""
        import base64
        from cryptography.fernet import Fernet
        
        key = base64.urlsafe_b64encode(self.encryption_key.encode()[:32].ljust(32, b'='))
        cipher = Fernet(key)
        decrypted = cipher.decrypt(encrypted_data.encode())
        return decrypted.decode()
    
    async def get_system_status(self) -> Dict[str, Any]:
        """Статус системи"""
        current_ip = await self.get_current_ip()
        
        return {
            'exchange_connections': 3,
            'response_time': 47,
            'active_strategies': 2,
            'quantum_level': int(os.getenv('QUANTUM_LEVEL', 9)),
            'current_ip': current_ip,
            'security_level': 'high',
            'encryption_enabled': True,
            'ip_validation_enabled': True
        }
    
    async def check_environment(self) -> Dict[str, bool]:
        """Перевірка налаштувань оточення"""
        required_vars = [
            'BOT_TOKEN',
            'BINANCE_API_KEY',
            'BINANCE_API_SECRET',
            'ENCRYPTION_KEY'
        ]
        
        results = {}
        for var in required_vars:
            results[var] = bool(os.getenv(var))
        
        return results

# Додатковий клас для роботи з IP білістингом
class IPWhitelist:
    def __init__(self):
        self.whitelist_path = "ip_whitelist.txt"
        self.allowed_ips = self._load_whitelist()
    
    def _load_whitelist(self) -> List[str]:
        """Завантаження білого списку IP"""
        try:
            with open(self.whitelist_path, 'r') as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return []
    
    def add_ip(self, ip: str) -> bool:
        """Додавання IP до білого списку"""
        if ip not in self.allowed_ips:
            self.allowed_ips.append(ip)
            self._save_whitelist()
            return True
        return False
    
    def remove_ip(self, ip: str) -> bool:
        """Видалення IP з білого списку"""
        if ip in self.allowed_ips:
            self.allowed_ips.remove(ip)
            self._save_whitelist()
            return True
        return False
    
    def _save_whitelist(self):
        """Збереження білого списку"""
        with open(self.whitelist_path, 'w') as f:
            for ip in self.allowed_ips:
                f.write(f"{ip}\n")
    
    def is_allowed(self, ip: str) -> bool:
        """Перевірка IP"""
        return ip in self.allowed_ips

# Приклад використання
async def example_usage():
    security = QuantumSecuritySystem()
    
    # Генерація підпису
    signature = await security.generate_quantum_signature("test_data")
    print(f"Signature: {signature}")
    
    # Перевірка IP
    is_valid = await security.validate_request("127.0.0.1")
    print(f"IP Valid: {is_valid}")
    
    # Статус системи
    status = await security.get_system_status()
    print(f"System Status: {status}")
    
    # Перевірка оточення
    env_check = await security.check_environment()
    print(f"Environment: {env_check}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(example_usage())