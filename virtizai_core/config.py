from __future__ import annotations

import os
import subprocess
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
    deployment: str = "release"
    source_commit: str | None = None

    @classmethod
    def from_environment(cls) -> "AppConfig":
        data_dir = Path(os.environ.get("VIRTIZAI_DATA_DIR", "./data"))
        workspace_dir = Path(os.environ.get("VIRTIZAI_WORKSPACE_DIR", "./workspace"))
        log_dir = Path(os.environ.get("VIRTIZAI_LOG_DIR", "./logs"))
        database_path = Path(
            os.environ.get("VIRTIZAI_DATABASE_PATH", str(data_dir / "virtizai.db"))
        )
        deployment = os.environ.get("VIRTIZAI_DEPLOYMENT", "release").strip().lower()
        source_commit = os.environ.get("VIRTIZAI_SOURCE_COMMIT")
        if deployment in {"dev", "development", "source"} and not source_commit:
            try:
                source_commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=Path.cwd(), text=True, stderr=subprocess.DEVNULL).strip() or None
            except (OSError, subprocess.CalledProcessError):
                source_commit = None
        app_version = os.environ.get("VIRTIZAI_APP_VERSION", __version__)
        if deployment in {"dev", "development", "source"}:
            app_version = f"{app_version}-dev+{source_commit}" if source_commit else f"{app_version}-dev"
        return cls(
            data_dir=data_dir,
            workspace_dir=workspace_dir,
            log_dir=log_dir,
            database_path=database_path,
            app_version=app_version,
            deployment=deployment,
            source_commit=source_commit,
        )

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.workspace_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
