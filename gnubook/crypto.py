"""Encryption of stored secrets (Nextcloud app passwords / share passwords) at rest.

The key is derived (HKDF-SHA256) from [nextcloud] encryption_key or, if that is empty, from [app] secret_key.
Both live in config.toml / the environment, not in data_dir, so a copied or leaked system.sqlite alone does
not reveal the passwords. The running server can still decrypt them – it has to, for the background upload.
Changing the key makes the stored passwords unreadable; users then simply connect their Nextcloud again.
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "enc:v1:"


class SecretError(RuntimeError):
    pass


class SecretBox:
    def __init__(self, key_material: str):
        self._fernet = None
        if key_material:
            key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"gnubook", info=b"stored secrets v1").derive(
                key_material.encode())
            self._fernet = Fernet(base64.urlsafe_b64encode(key))

    @property
    def available(self) -> bool:
        return self._fernet is not None

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return (value or "").startswith(PREFIX)

    def encrypt(self, plain: str) -> str:
        if self._fernet is None:
            raise SecretError("Kein Schlüssel: [app] secret_key oder [nextcloud] encryption_key setzen.")
        return PREFIX + self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not self.is_encrypted(value):
            return value  # stored before encryption existed; migrated on start
        if self._fernet is None:
            raise SecretError("Kein Schlüssel zum Entschlüsseln vorhanden.")
        try:
            return self._fernet.decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken:
            raise SecretError("Gespeichertes Passwort lässt sich nicht entschlüsseln (Schlüssel geändert?).")
