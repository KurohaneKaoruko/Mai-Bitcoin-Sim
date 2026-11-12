"""事件日志：记录 LLM 决策思考、命令调用与后台事件，供 WebUI 决策日志页展示。

JSON Lines 追加写入（每行一条），环形截断保留最近 max 条；与钱包同目录持久化。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 各类型展示名
KIND_LABELS = {
    "llm": "🤖 LLM 决策",
    "command": "⌨️ 命令",
    "system": "⚙️ 后台事件",
}


def _now() -> int:
    return int(time.time())


class Journal:
    """事件日志存储。写入走 asyncio.to_thread，读快照走内存缓存。"""

    def __init__(self, data_dir: Path, max_entries: int = 500):
        self.path = Path(data_dir) / "journal.jsonl"
        self.max_entries = max(50, int(max_entries))
        self._lock = asyncio.Lock()
        self._cache: Optional[List[Dict[str, Any]]] = None

    async def load(self) -> List[Dict[str, Any]]:
        """读取全部日志条目（加载到缓存）。"""
        async with self._lock:
            if self._cache is not None:
                return self._cache
            entries: List[Dict[str, Any]] = []
            if self.path.exists():
                try:
                    raw = await asyncio.to_thread(self.path.read_text, "utf-8")
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except ValueError:
                            continue
                except Exception as e:  # noqa: BLE001
                    logger.error("事件日志读取失败: %s", e)
            self._cache = entries[-self.max_entries:]
            return self._cache

    async def log(
        self,
        kind: str,
        title: str,
        reasoning: str = "",
        detail: str = "",
        stream_id: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """追加一条事件。kind: llm / command / system。"""
        entry = {
            "ts": _now(),
            "kind": kind,
            "title": title[:200],
            "reasoning": (reasoning or "")[:2000],
            "detail": (detail or "")[:2000],
            "stream_id": (stream_id or "")[:120],
            "meta": meta or {},
        }
        async with self._lock:
            cache = await self._ensure_cache()
            cache.append(entry)
            trimmed = cache[-self.max_entries:]
            self._cache = trimmed
            try:
                await asyncio.to_thread(self._write_all, trimmed)
            except Exception as e:  # noqa: BLE001
                logger.error("事件日志写入失败: %s", e)

    async def _ensure_cache(self) -> List[Dict[str, Any]]:
        if self._cache is None:
            entries: List[Dict[str, Any]] = []
            if self.path.exists():
                try:
                    raw = await asyncio.to_thread(self.path.read_text, "utf-8")
                    for line in raw.splitlines():
                        line = line.strip()
                        if line:
                            try:
                                entries.append(json.loads(line))
                            except ValueError:
                                continue
                except Exception:
                    pass
            self._cache = entries[-self.max_entries:]
        return self._cache

    def _write_all(self, entries: List[Dict[str, Any]]) -> None:
        """全量覆写（条目有上限，体量可控）；先写临时文件再原子替换。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)

    async def tail(self, limit: int = 100, kind: str = "") -> List[Dict[str, Any]]:
        """取最近 limit 条（可按类型过滤）。"""
        entries = await self.load()
        if kind:
            entries = [e for e in entries if e.get("kind") == kind]
        return list(reversed(entries[-max(1, limit):]))
