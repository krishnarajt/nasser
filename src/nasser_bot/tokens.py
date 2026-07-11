from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class StoredToken:
    payload: Any
    created_at: float


class TokenStore:
    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl_seconds = ttl_seconds
        self._tokens: dict[str, StoredToken] = {}

    def put(self, payload: Any) -> str:
        self.prune()
        token = secrets.token_urlsafe(6)
        while token in self._tokens:
            token = secrets.token_urlsafe(6)
        self._tokens[token] = StoredToken(payload=payload, created_at=time.monotonic())
        return token

    def get(self, token: str) -> Any | None:
        self.prune()
        stored = self._tokens.get(token)
        if stored is None:
            return None
        return stored.payload

    def pop(self, token: str) -> Any | None:
        self.prune()
        stored = self._tokens.pop(token, None)
        if stored is None:
            return None
        return stored.payload

    def prune(self) -> None:
        now = time.monotonic()
        expired = [
            token
            for token, stored in self._tokens.items()
            if now - stored.created_at > self._ttl_seconds
        ]
        for token in expired:
            self._tokens.pop(token, None)

