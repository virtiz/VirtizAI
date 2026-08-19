from __future__ import annotations

import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
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


class FileSecretStore(SecretStore):
    """Small local secret store for customer-managed references.

    The file is intentionally outside SQLite, created with restrictive permissions,
    and replaced atomically. APIs expose only whether a reference is configured.
    """

    _REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})
        else:
            os.chmod(self.path, 0o600)

    def _validate(self, reference: str) -> str:
        if not isinstance(reference, str) or not self._REFERENCE.fullmatch(reference):
            raise ValueError("invalid secret reference")
        return reference

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, values: dict[str, str]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".secrets-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(values, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, reference: str) -> str | None:
        return self._read().get(self._validate(reference))

    def set(self, reference: str, value: str) -> None:
        reference = self._validate(reference)
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise ValueError("secret value must be non-empty and <= 4096 characters")
        values = self._read()
        values[reference] = value
        self._write(values)

    def delete(self, reference: str) -> None:
        reference = self._validate(reference)
        values = self._read()
        values.pop(reference, None)
        self._write(values)

    def configured(self, reference: str) -> bool:
        return self.get(reference) is not None
