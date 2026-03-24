from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StateStore:
    """Tracks PipelineRuns already handled (by UID) to avoid duplicate PRs."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def _atomic_write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=".fixer-state-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            with self._path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("State file unreadable (%s), starting fresh", e)
            return {}

    def is_processed(self, pipelinerun_uid: str) -> bool:
        data = self.load()
        return pipelinerun_uid in data.get("processed_runs", {})

    def mark_processed(self, pipelinerun_uid: str, meta: dict[str, Any]) -> None:
        data = self.load()
        runs = data.setdefault("processed_runs", {})
        runs[pipelinerun_uid] = meta
        self._atomic_write(data)
