from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Mapping


class SecretStore(ABC):
    """Secure-value boundary; ordinary repositories only store secret references."""

    @abstractmethod
    def get(self, reference: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def set(self, reference: str, value: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, reference: str) -> None:
        raise NotImplementedError


class EnvironmentSecretStore(SecretStore):
    """Bootstrap-only store. Production deployments should provide a secret manager."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = dict(values) if values is not None else dict(os.environ)

    def get(self, reference: str) -> str | None:
        return self._values.get(reference)

    def set(self, reference: str, value: str) -> None:
        raise RuntimeError("EnvironmentSecretStore is read-only")

    def delete(self, reference: str) -> None:
        raise RuntimeError("EnvironmentSecretStore is read-only")


class MemorySecretStore(SecretStore):
    """Test-only store. Values never cross the database or telemetry boundary."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, reference: str) -> str | None:
        return self._values.get(reference)

    def set(self, reference: str, value: str) -> None:
        self._values[reference] = value

    def delete(self, reference: str) -> None:
        self._values.pop(reference, None)
