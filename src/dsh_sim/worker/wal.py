"""Worker 本地事件 WAL（定义书 §并发、断线、取消与补算规则）。

断线时事件先落本地 JSONL（每条含 job_id/lease_id/fencing_token/event_seq/kind/payload），
恢复后按序重传；服务端 (job_id,event_seq) 唯一约束 + 幂等重发使重传安全。
刻意简单：不用框架，一行一条 JSON，acked 翻转时整文件重写（事件量小）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonlWal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def _write_all(self, rows: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def append(
        self,
        *,
        job_id: str,
        lease_id: str,
        fencing_token: int,
        event_seq: int,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        rows = self._read_all()
        rows.append(
            {
                "job_id": job_id,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "event_seq": event_seq,
                "kind": kind,
                "payload": payload,
                "acked": False,
            }
        )
        self._write_all(rows)

    def mark_acked(self, job_id: str, event_seq: int) -> None:
        rows = self._read_all()
        for r in rows:
            if r["job_id"] == job_id and r["event_seq"] == event_seq:
                r["acked"] = True
        self._write_all(rows)

    def unacked(self) -> list[dict[str, Any]]:
        """未确认事件，按 (job_id, event_seq) 排序——恢复后按序重传。"""
        return sorted(
            (r for r in self._read_all() if not r.get("acked")),
            key=lambda r: (r["job_id"], r["event_seq"]),
        )


__all__ = ["JsonlWal"]
