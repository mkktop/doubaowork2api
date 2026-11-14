"""Local authenticated account encryption; JSON exports remain portable."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = 'doubaowork-account/aes256gcm/v1'


class AccountVault:
    def __init__(self, directory: Path):
        self.path = directory / '.account-vault.key'

    def key(self, *, create=False):
        try:
            value = self.path.read_bytes()
        except FileNotFoundError:
            if not create:
                raise ValueError('账号加密密钥缺失，请恢复原 .account-vault.key，账号文件未修改。') from None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            value = os.urandom(32)
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return self.key()
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        if len(value) != 32:
            raise ValueError('账号加密密钥长度异常，账号文件未修改。')
        return value

    def encode(self, document):
        nonce = os.urandom(12)
        raw = json.dumps(document, ensure_ascii=False, allow_nan=False).encode()
        ciphertext = AESGCM(self.key(create=True)).encrypt(nonce, raw, FORMAT.encode())
        return {'format':FORMAT, 'nonce':base64.b64encode(nonce).decode(),
                'ciphertext':base64.b64encode(ciphertext).decode()}

    def decode(self, document):
        if not isinstance(document, dict) or 'format' not in document:
            return document
        if document.get('format') != FORMAT:
            raise ValueError('未知的账号加密格式。')
        key = self.key()
        try:
            raw = AESGCM(key).decrypt(base64.b64decode(document['nonce'], validate=True),
                base64.b64decode(document['ciphertext'], validate=True), FORMAT.encode())
            return json.loads(raw)
        except Exception:
            raise ValueError('账号加密文件校验失败，原文件保留。') from None
