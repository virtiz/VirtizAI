from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TaskClassification:
    kind: str
    reason: str


class TaskClassifier:
    """Cheap, deterministic classifier with environment-configurable signals."""

    def __init__(self, config: dict | None = None) -> None:
        raw = config or {}
        env = os.environ.get("VIRTIZAI_TASK_CLASSIFIER_CONFIG")
        if env:
            try:
                raw = {**raw, **json.loads(env)}
            except json.JSONDecodeError:
                pass
        self.hard = tuple(raw.get("hard_keywords", (
            "code", "coding", "implement", "modify", "repository", "workspace",
            "infrastructure", "deploy", "run command", "tool", "agent", "worker",
            "fix ", "build ",
        )))
        self.medium = tuple(raw.get("medium_keywords", (
            "analyze", "analysis", "compare", "design", "architecture",
            "plan", "planning", "research", "explain in depth", "evaluate",
        )))

    def classify(self, content: str) -> TaskClassification:
        text = content.strip().lower()
        if text.startswith(("hard:", "agent:", "code:")):
            return TaskClassification("hard", "explicit worker request")
        if text.startswith(("medium:", "analyze:", "plan:")):
            return TaskClassification("medium", "explicit medium request")
        if any(signal in text for signal in self.hard):
            return TaskClassification("hard", "capability or execution signal")
        if any(signal in text for signal in self.medium):
            return TaskClassification("medium", "reasoning or planning signal")
        return TaskClassification("simple", "default conversational request")


class CodexWorker:
    """Constrained Codex CLI worker; credentials stay in the CLI's secret store."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.executable = os.environ.get("VIRTIZAI_CODEX_BIN", "codex")
        self.timeout_seconds = min(float(os.environ.get("VIRTIZAI_CODEX_TIMEOUT", "900")), 3600)

    def _workspace(self, job_id: str, requested: str | None = None) -> Path:
        target = Path(requested).expanduser() if requested else self.workspace_root / "codex" / job_id
        target = target.resolve()
        if target != self.workspace_root and self.workspace_root not in target.parents:
            raise PermissionError("Codex workspace is outside the approved worker root")
        target.mkdir(parents=True, exist_ok=True)
        return target

    async def run(self, job_id: str, payload: dict) -> dict:
        workspace = self._workspace(job_id, payload.get("workspace"))
        if shutil.which(self.executable) is None and not Path(self.executable).exists():
            return {"worker": "codex", "status": "unavailable", "reason": "codex_cli_not_installed", "workspace": str(workspace)}
        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            return {"worker": "codex", "status": "failed", "reason": "empty_prompt", "workspace": str(workspace)}
        argv = [self.executable, "exec", "--json", "--sandbox", "workspace-write", "--approve-for-me", prompt]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=workspace, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return {"worker": "codex", "status": "timeout", "workspace": str(workspace)}
        output = stdout.decode(errors="replace")
        error = stderr.decode(errors="replace")
        # Keep worker results reviewable without returning raw command transcripts.
        summaries = []
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                summaries.append(str(item["text"]))
        summary = "\n".join(summaries)[-4000:] if summaries else (error[-1000:] if error else "Codex completed without a summary")
        return {
            "worker": "codex",
            "status": "succeeded" if process.returncode == 0 else "failed",
            "exit_code": process.returncode,
            "summary": summary,
            "workspace": str(workspace),
        }
