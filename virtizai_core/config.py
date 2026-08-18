from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .version import __version__


@dataclass(frozen=True)
class AppConfig:
    """Deployment paths are inputs, never application-owned source constants."""

    data_dir: Path
    workspace_dir: Path
    log_dir: Path
    database_path: Path
    app_version: str = __version__

    @classmethod
    def from_environment(cls) -> "AppConfig":
        data_dir = Path(os.environ.get("VIRTIZAI_DATA_DIR", "./data"))
        workspace_dir = Path(os.environ.get("VIRTIZAI_WORKSPACE_DIR", "./workspace"))
        log_dir = Path(os.environ.get("VIRTIZAI_LOG_DIR", "./logs"))
        database_path = Path(
            os.environ.get("VIRTIZAI_DATABASE_PATH", str(data_dir / "virtizai.db"))
        )
        return cls(
            data_dir=data_dir,
            workspace_dir=workspace_dir,
            log_dir=log_dir,
            database_path=database_path,
            app_version=os.environ.get("VIRTIZAI_APP_VERSION", __version__),
        )

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.workspace_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
