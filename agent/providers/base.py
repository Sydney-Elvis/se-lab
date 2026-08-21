from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class ProviderResult:
    ok: bool
    text: str
    latency_ms: int
    status_code: int | None
    error: str | None = None


class Provider(ABC):
    @abstractmethod
    def chat(self, *, base_url: str, model: str, prompt: str, timeout_connect: float, timeout_read: float, api_key: str | None = None) -> ProviderResult:
        raise NotImplementedError

