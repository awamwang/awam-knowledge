# SPDX-License-Identifier: MIT
"""进程外状态（stdlib 零依赖）：job、一次性 token、撤销记录 + 独立审计日志。

逐字移植自 Node code/store.js：state.json 快照 + state.jsonl 追加日志（启动回放 at>=saved_at 的事件），
超阈值压实；审计 audit.jsonl 只追加、永不重写。崩溃后 running job 标 interrupted（不自动续跑）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .fs_util import append_jsonl, ensure_dir, read_json, read_jsonl, write_json_atomic

SNAPSHOT = "state.json"
JOURNAL = "state.jsonl"
AUDIT = "audit.jsonl"
JOURNAL_MAX_BYTES = 512 * 1024


def _now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, dir_: str, snapshot_path: str, journal_path: str, audit_path: str,
                 state: Dict[str, Any], interrupted: List[str]):
        self.dir = dir_
        self._snapshot = snapshot_path
        self._journal = journal_path
        self._audit = audit_path
        self.state = state
        self.interrupted = interrupted

    def _append(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        ev["at"] = _now_ms()
        append_jsonl(self._journal, ev)
        return ev

    def _maybe_compact(self) -> None:
        try:
            if os.path.getsize(self._journal) > JOURNAL_MAX_BYTES:
                self.compact()
        except OSError:
            pass

    def compact(self) -> None:
        write_json_atomic(self._snapshot, {
            "saved_at": _now_ms(),
            "jobs": list(self.state["jobs"].values()),
            "tokens": list(self.state["usedTokens"]),
            "undo": self.state["undo"],
            "undone": list(self.state["undone"]),
        })
        try:
            with open(self._journal, "w"):
                pass
        except OSError:
            pass

    def record_job(self, job: Dict[str, Any]) -> None:
        self.state["jobs"][job["job_id"]] = job
        self._append({"t": "job", "job": job})
        self._maybe_compact()

    def record_token(self, token: str) -> None:
        self.state["usedTokens"].add(token)
        self._append({"t": "token", "token": token})
        self._maybe_compact()

    def record_undo(self, entry: Dict[str, Any]) -> None:
        self.state["undo"].append(entry)
        self._append({"t": "undo", "entry": entry})
        self._maybe_compact()

    def record_undo_used(self, proposal_id: str) -> None:
        self.state["undone"].add(proposal_id)
        self._append({"t": "undo_used", "proposal_id": proposal_id})
        self._maybe_compact()

    def audit(self, rec: Dict[str, Any]) -> None:
        full = dict(rec)
        full["at"] = _now_ms()
        append_jsonl(self._audit, full)

    def read_audit(self, since: int = 0, limit: Optional[int] = 100) -> Dict[str, Any]:
        since = int(since or 0)
        raw = 100 if limit in (None, "") else int(limit)
        lim = max(1, min(raw, 2000))
        allrec = [e for e in read_jsonl(self._audit) if int(e.get("at") or 0) >= since]
        return {"total": len(allrec), "entries": allrec[-lim:]}


def _new_state() -> Dict[str, Any]:
    return {"jobs": {}, "usedTokens": set(), "undo": [], "undone": set()}


def _apply_snapshot(state: Dict[str, Any], snap: Any) -> None:
    if not snap or not isinstance(snap, dict):
        return
    for j in snap.get("jobs") or []:
        if j and j.get("job_id"):
            state["jobs"][j["job_id"]] = j
    for t in snap.get("tokens") or []:
        state["usedTokens"].add(t)
    if isinstance(snap.get("undo"), list):
        state["undo"] = list(snap["undo"])
    for pid in snap.get("undone") or []:
        state["undone"].add(pid)


def _apply_event(state: Dict[str, Any], ev: Any) -> None:
    if not ev or not isinstance(ev, dict):
        return
    t = ev.get("t")
    if t == "job" and ev.get("job") and ev["job"].get("job_id"):
        state["jobs"][ev["job"]["job_id"]] = ev["job"]
    elif t == "token" and ev.get("token"):
        state["usedTokens"].add(ev["token"])
    elif t == "undo" and ev.get("entry"):
        state["undo"].append(ev["entry"])
    elif t == "undo_used" and ev.get("proposal_id"):
        state["undone"].add(ev["proposal_id"])


def open_store(dir_: str, recover: bool = True) -> Store:
    ensure_dir(dir_)
    snapshot_path = os.path.join(dir_, SNAPSHOT)
    journal_path = os.path.join(dir_, JOURNAL)
    audit_path = os.path.join(dir_, AUDIT)
    state = _new_state()

    snap = read_json(snapshot_path, None)
    _apply_snapshot(state, snap)
    since = (snap.get("saved_at") if snap else 0) or 0
    for ev in read_jsonl(journal_path):
        if int(ev.get("at") or 0) >= since:
            _apply_event(state, ev)

    interrupted: List[str] = []
    if recover:
        for jid in list(state["jobs"].keys()):
            j = state["jobs"][jid]
            if j.get("status") == "running":
                j["status"] = "interrupted"
                j["phase"] = "interrupted"
                j["interrupted_at"] = _now_ms()
                interrupted.append(jid)

    return Store(dir_, snapshot_path, journal_path, audit_path, state, interrupted)
