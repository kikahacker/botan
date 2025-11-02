from typing import Optional
from cryptography.fernet import Fernet, InvalidToken
from config import CFG
fernet = Fernet(CFG.FERNET_KEY.encode())

def encrypt_text(plaintext: str) -> str:
    return fernet.encrypt(plaintext.encode()).decode()

def decrypt_text(token_str: str) -> str:
    try:
        return fernet.decrypt(token_str.encode()).decode()
    except InvalidToken as e:
        raise ValueError('Invalid encryption token or wrong FERNET_KEY') from e