from __future__ import annotations

import time
import uuid
import json
import sqlite3
import threading
from pathlib import Path
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RequestLogEntry:
    id: str
    started_at: float
    model: str
    stream: bool
    status: str = "running"
    account_id: str = ""
    error: str = ""
    finished_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    first_token_at: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_estimated: bool = False
    total_tokens: int | None = None
    cached_tokens: int | None = None

    def public_dict(self) -> dict[str, Any]:
        duration = (
            (self.finished_at or time.time()) - self.started_at
            if self.started_at
            else 0.0
        )
        return {
            "id": self.id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": max(0, round(duration * 1000)),
            "ttft_ms": max(0, round((self.first_token_at - self.started_at) * 1000)) if self.first_token_at else None,
            "tokens_per_second": round(self.output_tokens / (self.finished_at - self.first_token_at), 1)
                if self.output_tokens is not None and self.first_token_at and self.finished_at > self.first_token_at else None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens if self.total_tokens is not None else self.input_tokens + self.output_tokens if self.input_tokens is not None and self.output_tokens is not None else None,
            "usage_estimated": self.usage_estimated,
            "cached_tokens": self.cached_tokens,
            "credits": None,
            "estimated_price": None,
            "model": self.model,
            "stream": self.stream,
            "status": self.status,
            "account_id": self.account_id,
            "error": self.error,
            "meta": self.meta,
        }


class RequestLog:
    def __init__(self, capacity: int = 200, path: Path | None = None) -> None:
        self._entries: deque[RequestLogEntry] = deque(maxlen=capacity)
        self.path = path
        self._lock = threading.RLock()
        self._writes = 0
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, at REAL, model TEXT, account TEXT, key_id TEXT, status TEXT, body TEXT)')
                db.execute('CREATE INDEX IF NOT EXISTS requests_time ON requests(at)')
                db.execute('DELETE FROM requests WHERE at < ?', (time.time()-30*86400,))
                for row in db.execute("SELECT body FROM requests WHERE status='running'").fetchall():
                    value = json.loads(row[0])
                    value.update(status='interrupted', finished_at=time.time(), error='服务重启，请求未完成。')
                    value['duration_ms'] = max(0,round((value['finished_at']-value['started_at'])*1000))
                    db.execute('UPDATE requests SET status=?,body=? WHERE id=?', ('interrupted',json.dumps(value),value['id']))
            try:
                path.chmod(0o600)
            except OSError:
                pass

    def _persist(self, entry):
        if self.path is None:
            return
        value = entry.public_dict()
        value['meta'] = {key:item for key,item in value['meta'].items()
            if key in {'messages','key_id','reasoning_effort','native_reused','usage_source'}}
        with self._lock, sqlite3.connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?)',
                (entry.id, entry.started_at, entry.model, entry.account_id,
                 str(value['meta'].get('key_id','')), entry.status, json.dumps(value, ensure_ascii=False)))
            self._writes += 1
            if self._writes % 1000 == 0:
                db.execute('DELETE FROM requests WHERE at < ? OR id IN (SELECT id FROM requests ORDER BY at DESC LIMIT -1 OFFSET 100000)',
                    (time.time()-30*86400,))

    def start(
        self,
        *,
        model: str,
        stream: bool,
        meta: dict[str, Any] | None = None,
    ) -> RequestLogEntry:
        entry = RequestLogEntry(
            id=f"req_{uuid.uuid4().hex[:12]}",
            started_at=time.time(),
            model=model,
            stream=stream,
            meta=meta or {},
        )
        self._entries.appendleft(entry)
        self._persist(entry)
        return entry

    def account(self, entry: RequestLogEntry, account_id: str) -> None:
        entry.account_id = account_id
        self._persist(entry)

    def success(self, entry: RequestLogEntry) -> None:
        entry.status = "success"
        entry.finished_at = time.time()
        self._persist(entry)

    def failure(self, entry: RequestLogEntry, error: str) -> None:
        entry.status = "error"
        entry.error = error[:500]
        entry.finished_at = time.time()
        self._persist(entry)

    def snapshot(self, limit: int = 100, *, offset=0, since=0, until=None, model='', account_id='', key_id='') -> list[dict[str, Any]]:
        if self.path:
            filters, values = ['at>=?'], [since]
            if until is not None:
                filters.append('at<=?'); values.append(until)
            for name, value in [('model',model),('account',account_id),('key_id',key_id)]:
                if value:
                    filters.append(name+'=?'); values.append(value)
            with self._lock, sqlite3.connect(self.path) as db:
                rows = db.execute('SELECT body FROM requests WHERE '+' AND '.join(filters)+' ORDER BY at DESC LIMIT ? OFFSET ?',
                    values + [max(1,min(limit,200)), max(0,offset)]).fetchall()
            active = {entry.id:entry.public_dict() for entry in self._entries if entry.status=='running'}
            return [active.get((value:=json.loads(row[0]))['id'], value) for row in rows]
        return [
            entry.public_dict()
            for entry in list(self._entries)[: max(1, min(limit, 200))]
        ]

    def analytics(self, days=7):
        if not self.path:
            return {'days':days,'by_model':[],'by_account':[],'by_key':[],'by_day':[]}
        result = {'days':days}
        with self._lock, sqlite3.connect(self.path) as db:
            for name, column in [('model','model'),('account','account'),('key','key_id'),('day',"date(at,'unixepoch','+8 hours')")]:
                rows = db.execute(f"SELECT {column},COUNT(*),SUM(status='success'),SUM(status IN ('error','interrupted')),AVG(CASE WHEN status!='running' THEN json_extract(body,'$.duration_ms') END),SUM(COALESCE(json_extract(body,'$.input_tokens'),0)),SUM(COALESCE(json_extract(body,'$.output_tokens'),0)) FROM requests WHERE at>=? GROUP BY {column} ORDER BY COUNT(*) DESC", (time.time()-days*86400,)).fetchall()
                result['by_'+name] = [dict(zip(['id','requests','success','errors','average_ms','input_tokens','output_tokens'], row)) for row in rows]
        return result
