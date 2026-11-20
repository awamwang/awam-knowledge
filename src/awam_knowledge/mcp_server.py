# SPDX-License-Identifier: MIT
"""最小 MCP server（stdio，零依赖，手写 JSON-RPC 2.0）——逐字移植自 Node code/mcp-server.js。

暴露 9 工具：search / propose_write / commit_write / undo_write / start_import /
get_job_status / cancel_job / resume_job / get_audit。

选择手写 JSON-RPC 而非 mcp SDK（有意偏离「复用 mcp>=2」的初衷，记录于 ADR 0022 M2d）：
Node 原版零依赖手写协议是 selftest oracle 的前提（请求外发 progress 通知、initialize
精确应答、tools/call 的 content[0].text JSON + isError 形状）；SDK 会引入 typer/rich 等
传递依赖且不支持请求上下文外发任意通知。协议子集与 Node 完全一致。

- search：本地 BM25（标题×4），语料根取 AWAM_INDEX_ROOT 或 KM_DATA_ROOT/<KM_SOURCE_ID>
  或 settings.yaml；索引带增量缓存（按 size/mtime/title 复用 chunk），缓存放状态目录。
- 写入闸：propose 不落盘；commit 需一次性 token（HMAC、绑基线 hash、TTL），token 用后
  持久化到状态库，重启后重放仍被拒；提交走原子写；撤销窗口内可 undo_write。
- 导入：start_import 真实遍历目录、写 manifest、后台线程推进、支持取消；运行中限频写
  检查点；中断/取消/崩溃后 resume_job 起新 job 按 size+mtime 复用未变文件。
- 状态：job / 已用 token / 撤销记录落在 AWAM_STATE_DIR（默认 <repo>/.spike-runtime），
  快照 state.json + 追加 state.jsonl；同目录单写者锁，第二个进程只读不写。
- 密钥来源（优先级）：AWAM_SECRET > AWAM_SECRET_CMD > 状态目录下 secret.key（首次生成 0600）。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import bm25
from .fs_util import acquire_lock, atomic_write, ensure_dir, read_json, write_json_atomic
from .import_md_dir import DFLT_SKIP, import_steps, parse_skip
from .index_root import validate_index_root
from .secret_provider import resolve_secret
from .store import open_store

_STATE_FALLBACK = Path(__file__).resolve().parent.parent.parent / ".spike-runtime"
STATE_DIR = os.path.abspath(os.environ.get("AWAM_STATE_DIR") or os.environ.get("SPIKE_ROOT")
                            or str(_STATE_FALLBACK))
WRITE_ROOT = os.path.abspath(os.environ.get("AWAM_WRITE_ROOT") or STATE_DIR)
PROPOSAL_FILE = os.path.join(STATE_DIR, "proposals.json")
IMPORTS_DIR = os.path.join(STATE_DIR, "imports")

PROPOSAL_TTL_MS = int(os.environ.get("PROPOSAL_TTL_MS") or 60000)
TOKEN_TTL_MS = int(os.environ.get("TOKEN_TTL_MS") or 30000)
UNDO_WINDOW_MS = int(os.environ.get("UNDO_WINDOW_MS") or 60000)

ensure_dir(STATE_DIR)
LOCK = acquire_lock(STATE_DIR, "store", stale_ms=int(os.environ.get("AWAM_LOCK_STALE_MS") or 600000))
STORE = open_store(STATE_DIR, recover=bool(LOCK.get("ok")))

_stdout_lock = threading.Lock()

CLIENT: Dict[str, Any] = {"name": "unknown", "version": None}


def _now() -> int:
    return int(time.time() * 1000)


def norm(p: Optional[str]) -> str:
    s = str(p or "")
    return s.replace("\\", "/").rstrip("/")


def sha256_hex(s: Any) -> str:
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()


def _emit(line: str) -> None:
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# ---------- 审计：写入闸的每次提案/提交/撤销都追加一行，独立于状态日志（不随压实丢失） ----------
def audit_write(event: str, extra: Optional[Dict[str, Any]] = None) -> None:
    try:
        rec = {"event": event, "client": CLIENT["name"], "client_version": CLIENT["version"]}
        if extra:
            rec.update(extra)
        STORE.audit(rec)
    except Exception:
        pass  # 审计失败不阻断写入，但也绝不静默改判结果


# ---------- 密钥：环境变量 → 外部命令（如 Bitwarden）→ 本地 0600 文件 ----------
_SECRET: Optional[str] = None
_secret_lock = threading.Lock()


def get_secret() -> str:
    global _SECRET
    with _secret_lock:
        if _SECRET:
            return _SECRET
        _SECRET = resolve_secret({"root": STATE_DIR})["secret"]
        return _SECRET


def read_if_exists(p: str) -> str:
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def guard_writable() -> Optional[Dict[str, Any]]:
    if LOCK.get("ok"):
        return None
    return {"error": "state_locked", "reason": LOCK.get("error") or "locked",
            "holder": LOCK.get("holder"),
            "note": "同一状态目录已有写者（" + STATE_DIR + "），当前进程只读"}


# ---------- 写入闸：提案与一次性 token ----------
TOKEN_REASONS = {
    "MISSING": "missing_token",
    "BAD_SIG": "bad_signature",
    "MISMATCH": "proposal_mismatch",
    "HASH": "hash_mismatch",
    "EXPIRED": "token_expired",
    "REPLAYED": "token_replayed",
}


def verify_token(token: Any, proposal: Dict[str, Any]) -> Dict[str, Any]:
    if not token or not isinstance(token, str) or "." not in token:
        return {"ok": False, "reason": TOKEN_REASONS["MISSING"]}
    body, sig = token.split(".", 1)
    expect = hmac_mod.new(get_secret().encode("utf-8"), body.encode("utf-8"),
                          hashlib.sha256).digest()
    import base64
    expect_b64 = base64.urlsafe_b64encode(expect).rstrip(b"=").decode("ascii")
    if len(sig) != len(expect_b64) or not hmac_mod.compare_digest(sig, expect_b64):
        return {"ok": False, "reason": TOKEN_REASONS["BAD_SIG"]}
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except Exception:
        return {"ok": False, "reason": TOKEN_REASONS["BAD_SIG"]}
    if payload.get("proposal_id") != proposal["id"]:
        return {"ok": False, "reason": TOKEN_REASONS["MISMATCH"]}
    if payload.get("baseline_hash") != proposal["baseline_hash"]:
        return {"ok": False, "reason": TOKEN_REASONS["HASH"]}
    if _now() - payload.get("issued_at", 0) > TOKEN_TTL_MS:
        return {"ok": False, "reason": TOKEN_REASONS["EXPIRED"]}
    if token in STORE.state["usedTokens"]:
        return {"ok": False, "reason": TOKEN_REASONS["REPLAYED"]}
    return {"ok": True, "payload": payload}


def load_proposals() -> Dict[str, Any]:
    return read_json(PROPOSAL_FILE, {}) or {}


def prune_expired(proposals: Dict[str, Any]) -> None:
    for pid in list(proposals.keys()):
        if _now() > (proposals[pid].get("expires_at") or 0):
            del proposals[pid]


def save_proposals(p: Dict[str, Any]) -> None:
    write_json_atomic(PROPOSAL_FILE, p)


def resolve_target(target: Any) -> Optional[str]:
    p = os.path.abspath(os.path.join(WRITE_ROOT, str(target or "")))
    rel = os.path.relpath(p, WRITE_ROOT)
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    return p


def tool_propose_write(args: Dict[str, Any]) -> Dict[str, Any]:
    locked = guard_writable()
    if locked:
        return locked
    target = str((args or {}).get("target_path") or "notes/new.md")
    content = str((args or {}).get("content") or "")
    source = str((args or {}).get("source") or "session")
    fp = resolve_target(target)
    if not fp:
        return {"error": "path_outside_write_root", "target_path": target,
                "write_root": norm(WRITE_ROOT)}
    baseline = read_if_exists(fp)
    import secrets as _secrets
    pid = "prop_" + _secrets.token_hex(6)
    proposals = load_proposals()
    prune_expired(proposals)
    baseline_hash = sha256_hex(baseline)
    proposals[pid] = {
        "id": pid, "target_path": target, "content": content, "source": source,
        "baseline_hash": baseline_hash,
        "baseline_exists": os.path.exists(fp),
        "created_at": _now(), "expires_at": _now() + PROPOSAL_TTL_MS,
        "status": "pending",
    }
    save_proposals(proposals)
    audit_write("propose", {"proposal_id": pid, "target_path": target,
                            "baseline_hash": baseline_hash,
                            "baseline_exists": os.path.exists(fp), "source": source})
    baseline_desc = (str(baseline.count("\n") + 1) + " lines") if baseline else "new file"
    return {
        "proposal_id": pid,
        "target_path": target,
        "diff": [
            "--- baseline (" + baseline_desc + ")",
            "+++ proposal (+" + str(content.count("\n") + 1) + " lines)",
        ],
        "baseline_hash": baseline_hash,
        "proposal_ttl_ms": PROPOSAL_TTL_MS,
        "persisted": False,
    }


def tool_commit_write(args: Dict[str, Any]) -> Dict[str, Any]:
    locked = guard_writable()
    if locked:
        return locked
    pid = str((args or {}).get("proposal_id") or "")
    token = (args or {}).get("confirmation_token")
    proposals = load_proposals()
    p = proposals.get(pid)
    if not p:
        audit_write("commit_rejected", {"proposal_id": pid, "reason": "unknown_proposal"})
        return {"error": "unknown_proposal", "proposal_id": pid}
    if p.get("status") == "committed":
        audit_write("commit_rejected", {"proposal_id": pid, "reason": "already_committed"})
        return {"error": "already_committed", "proposal_id": pid}
    if _now() > p.get("expires_at", 0):
        del proposals[pid]
        save_proposals(proposals)
        audit_write("commit_rejected", {"proposal_id": pid, "reason": "proposal_expired"})
        return {"error": "proposal_expired", "proposal_id": pid}

    try:
        v = verify_token(token, p)
    except Exception as e:
        audit_write("commit_rejected", {"proposal_id": pid, "reason": "secret_unavailable"})
        return {"error": "secret_unavailable", "reason": str(e)}
    if not v.get("ok"):
        audit_write("commit_rejected", {"proposal_id": pid, "reason": v["reason"],
                                        "target_path": p["target_path"]})
        return {"error": "rejected", "reason": v["reason"], "proposal_id": pid}

    fp = resolve_target(p["target_path"])
    if not fp:
        audit_write("commit_rejected", {"proposal_id": pid,
                                        "reason": "path_outside_write_root",
                                        "target_path": p["target_path"]})
        return {"error": "path_outside_write_root", "proposal_id": pid}

    prev = read_if_exists(fp)
    current_hash = sha256_hex(prev)
    if current_hash != p["baseline_hash"]:
        audit_write("commit_rejected", {"proposal_id": pid, "reason": "baseline_changed",
                                        "target_path": p["target_path"]})
        return {"error": "conflict", "reason": "baseline_changed", "proposal_id": pid,
                "current_hash": current_hash, "baseline_hash": p["baseline_hash"]}

    atomic_write(fp, p["content"])
    STORE.record_token(token)  # 一次性 token，重启后仍拒重放
    p["status"] = "committed"
    p["committed_at"] = _now()
    save_proposals(proposals)
    approved_at = None
    try:
        import base64
        body = str(token).split(".", 1)[0]
        approved_at = json.loads(base64.urlsafe_b64decode(
            body + "=" * (-len(body) % 4)).decode("utf-8")).get("issued_at") or None
    except Exception:
        pass  # 仅用于审计
    audit_write("commit", {"proposal_id": pid, "target_path": p["target_path"],
                           "baseline_hash": p["baseline_hash"],
                           "committed_hash": sha256_hex(p["content"]),
                           "approved_at": approved_at})
    STORE.record_undo({
        "proposal_id": pid, "path": norm(p["target_path"]), "existed": p["baseline_exists"],
        "prev_content": prev, "prev_hash": sha256_hex(prev),
        "committed_hash": sha256_hex(p["content"]),
        "at": _now(), "expires_at": _now() + UNDO_WINDOW_MS,
    })
    return {"committed": True, "proposal_id": pid, "target_path": p["target_path"],
            "undo_window_ms": UNDO_WINDOW_MS, "undo_available": True}


def tool_undo_write(args: Dict[str, Any]) -> Dict[str, Any]:
    locked = guard_writable()
    if locked:
        return locked
    pid = str((args or {}).get("proposal_id") or "")
    entry = next((e for e in reversed(STORE.state["undo"]) if e.get("proposal_id") == pid), None)
    if not entry:
        return {"error": "no_undo_entry", "proposal_id": pid}
    if pid in STORE.state["undone"]:
        return {"error": "undo_already_done", "proposal_id": pid}
    if _now() > (entry.get("expires_at") or 0):
        return {"error": "undo_expired", "proposal_id": pid}
    fp = resolve_target(entry["path"])
    if not fp:
        return {"error": "path_outside_write_root", "proposal_id": pid}
    current_hash = sha256_hex(read_if_exists(fp))
    if current_hash != entry.get("committed_hash"):
        return {"error": "conflict", "reason": "file_changed_after_commit", "proposal_id": pid,
                "current_hash": current_hash}
    if entry.get("existed"):
        atomic_write(fp, entry.get("prev_content") or "")
    else:
        try:
            os.unlink(fp)
        except OSError:
            pass  # 已不存在
    STORE.record_undo_used(pid)
    audit_write("undo", {"proposal_id": pid, "target_path": entry["path"],
                         "restored": entry.get("existed")})
    return {"undone": True, "proposal_id": pid, "target_path": entry["path"],
            "restored": entry.get("existed")}


# ---------- search：本地 BM25 + 增量索引缓存 ----------
_index_lock = threading.Lock()
CORPUS: Optional[Dict[str, Any]] = None
BM25_IDX: Optional[Dict[str, Any]] = None
INDEX_INFO: Optional[Dict[str, Any]] = None


def ensure_index(force: bool) -> None:
    global CORPUS, BM25_IDX, INDEX_INFO
    with _index_lock:
        if not force and (BM25_IDX is not None or INDEX_INFO is not None):
            return
        CORPUS = None
        BM25_IDX = None
        INDEX_INFO = None
        v = validate_index_root(os.environ.get("AWAM_INDEX_ROOT") or "")
        if not v.get("ok"):
            INDEX_INFO = {"error": v.get("error"), "reason": v.get("reason")}
            return
        excludes = bm25.parse_excludes(os.environ.get("AWAM_EXCLUDES") or bm25.DEFAULT_EXCLUDES)
        chunk_lines = int(os.environ.get("AWAM_CHUNK_LINES") or bm25.DEFAULT_CHUNK_LINES)
        title_weight = int(os.environ.get("AWAM_TITLE_WEIGHT") or bm25.DEFAULT_TITLE_WEIGHT)
        cache_path = os.path.abspath(os.environ.get("AWAM_INDEX_CACHE")
                                     or os.path.join(STATE_DIR, "index-cache.json"))
        corpus = bm25.build_corpus_cached({"root": v["root"], "excludes": excludes,
                                           "chunkLines": chunk_lines,
                                           "titleWeight": title_weight,
                                           "cachePath": cache_path})
        CORPUS = corpus
        BM25_IDX = bm25.build_index(corpus["docs"])
        INDEX_INFO = {
            "root": v["root"],
            "docs": len({d["path"] for d in corpus["docs"]}),
            "chunks": BM25_IDX["N"],
            "title_weight": title_weight,
            "excluded": excludes,
            "cache": corpus["cache"],
        }


def tool_search(args: Dict[str, Any]) -> Dict[str, Any]:
    q = str((args or {}).get("query") or "").strip()
    if not q:
        return {"hits": [], "note": "未找到（空查询）", "mode": "bm25-title4"}
    ensure_index(False)
    if INDEX_INFO is not None and INDEX_INFO.get("error"):
        return {"hits": [], "error": "index_root_not_configured", "note": INDEX_INFO["error"],
                "mode": "bm25-title4"}
    topk = max(1, min(int((args or {}).get("topk") or 5), 50))
    with _index_lock:
        corpus, idx, info = CORPUS, BM25_IDX, INDEX_INFO
    return {
        "hits": bm25.search(corpus, idx, q, {"topk": topk, "stem": False}),
        "mode": "bm25-title4",
        "index": info,
        "query": q,
    }


# ---------- 长任务：真实导入 job + progress 通知 ----------
def notify(method: str, params: Dict[str, Any]) -> None:
    _emit(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))


def same_root(a: Any, b: Any) -> bool:
    return norm(a).lower() == norm(b).lower()


def maybe_refresh_index(job_root: str) -> Dict[str, Any]:
    try:
        v = validate_index_root(os.environ.get("AWAM_INDEX_ROOT") or "")
        if not v.get("ok") or not same_root(v["root"], job_root):
            return {"refreshed": False, "reason": "index_root_differs"}
        ensure_index(True)
        with _index_lock:
            info = dict(INDEX_INFO) if INDEX_INFO else {}
        return {"refreshed": True, "docs": info.get("docs"), "chunks": info.get("chunks"),
                "cache": info.get("cache")}
    except Exception as e:
        return {"refreshed": False, "reason": "refresh_failed:" + str(e)}


def finish_job(job: Dict[str, Any], status: str, extra: Optional[Dict[str, Any]] = None) -> None:
    job["status"] = status
    job["phase"] = status
    job["finished_at"] = _now()
    job.pop("current_path", None)
    if extra:
        job.update(extra)
    job["resumable"] = bool(job.get("checkpoint_path")
                            and os.path.exists(job["checkpoint_path"]))
    if LOCK.get("ok"):
        try:
            STORE.record_job(job)
        except Exception:
            pass  # 状态写入失败不影响结果


def run_import(job: Dict[str, Any], args: Dict[str, Any], progress_token: Any) -> None:
    skip = parse_skip(args.get("skip") or ",".join(DFLT_SKIP))
    scan_secrets = args.get("scan_secrets") is not False
    resume = None
    if args.get("resume_from"):
        resume = read_json(os.path.abspath(str(args["resume_from"])), None)
        if resume is None:
            finish_job(job, "failed", {"errors": ["resume_manifest_unreadable"]})
            return

    # 检查点：只存已完成 Markdown 的条目（resume 只用得到这些字段），限频落盘
    ckpt_path = job.get("checkpoint_path")
    last_ckpt_at = 0
    last_ckpt_count = 0

    def checkpoint(c: Dict[str, Any]) -> None:
        nonlocal last_ckpt_at, last_ckpt_count
        if not ckpt_path or not c.get("markdown"):
            return
        t = _now()
        if len(c["markdown"]) == last_ckpt_count:
            return
        if t - last_ckpt_at < 1000 and len(c["markdown"]) - last_ckpt_count < 50:
            return
        last_ckpt_at = t
        last_ckpt_count = len(c["markdown"])
        import datetime
        write_json_atomic(ckpt_path, {
            "version": 2, "partial": True, "job_id": job["job_id"], "root": job["root"],
            "generated_at": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": c["phase"], "markdown": c["markdown"],
        })
        job["checkpoint_entries"] = len(c["markdown"])
        # 进度数字与检查点同频落盘：只靠 phase 跃迁写状态，另一进程读到的是整段 analyzing 的旧值
        job["done"] = c["done"]
        job["total"] = c["total"]
        if LOCK.get("ok"):
            try:
                STORE.record_job(job)
            except Exception:
                pass  # 状态写入失败不影响导入

    it = import_steps(Path(job["root"]), skip, {
        "scanSecrets": scan_secrets, "resume": resume, "checkpoint": checkpoint,
        "shouldCancel": lambda: bool(job.get("cancel_requested")),
    })
    last_notify = 0.0
    steps = 0
    while True:
        if job.get("cancel_requested"):
            finish_job(job, "cancelled", {"done": job.get("done", 0)})
            return
        try:
            s = next(it)
        except StopIteration as stop:
            r = stop.value or {}
            if r.get("cancelled"):
                finish_job(job, "cancelled", {"done": r.get("processed") or 0})
                return
            manifest = {
                "version": 2,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "job_id": job["job_id"],
                "summary": r["summary"], "markdown": r["markdown"],
                "attachments": r["attachments"], "unresolved": r["unresolved"],
                "security": r["security"], "duplicate_groups": r["duplicateGroups"],
            }
            write_json_atomic(job["manifest_path"], manifest)
            counts = {
                "markdown_files": r["summary"]["markdown"]["files"],
                "attachments": r["summary"]["attachments"]["files"],
                "unresolved": len(r["unresolved"]),
                "security_files": r["summary"]["security"]["files_with_hits"],
                "walk_errors": len(r["walkErrors"]),
            }
            index = maybe_refresh_index(job["root"])
            errors = [e["path"] + ": " + e["error"] for e in r["walkErrors"][:20]]
            if r["unresolved"]:
                errors.append("unresolved_links: " + str(len(r["unresolved"]))
                              + "（明细见 manifest）")
            if r["summary"]["security"]["files_with_hits"]:
                errors.append("secret_pattern_hits: "
                              + str(r["summary"]["security"]["files_with_hits"]) + " 个文件")
            units = r["summary"]["markdown"]["files"] + r["summary"]["attachments"]["files"]
            if ckpt_path:
                try:
                    os.unlink(ckpt_path)
                except OSError:
                    pass  # 已不存在
                if job.get("resume_from_path"):
                    try:
                        os.unlink(job["resume_from_path"])
                    except OSError:
                        pass  # 已被删或不可写
            finish_job(job, "done", {"done": units, "total": units, "counts": counts,
                                     "errors": errors, "index": index})
            if progress_token:
                notify("notifications/progress", {"progressToken": progress_token,
                                                  "progress": units, "total": units,
                                                  "message": "导入完成"})
            return
        v = s
        phase_changed = v["phase"] != job.get("phase")
        job["phase"] = v["phase"]
        job["done"] = v["done"]
        job["total"] = v["total"]
        if v.get("path"):
            job["current_path"] = v["path"]
        if phase_changed and LOCK.get("ok"):
            try:
                STORE.record_job(job)
            except Exception:
                pass  # 状态写入失败不影响导入
        if (progress_token and v["total"] > 0
                and (v["done"] == 0 or v["done"] == v["total"]
                     or time.time() - last_notify > 0.25)):
            last_notify = time.time()
            notify("notifications/progress", {
                "progressToken": progress_token, "progress": v["done"], "total": v["total"],
                "message": ("附件" if v["phase"] == "attachments" else "导入")
                + " " + str(v["done"]) + "/" + str(v["total"]),
            })
        steps += 1
        if steps % 8 == 0:
            time.sleep(0)


def tool_get_audit(args: Dict[str, Any]) -> Dict[str, Any]:
    r = STORE.read_audit(since=(args or {}).get("since") or 0,
                         limit=(args or {}).get("limit", 100))
    return {"total": r["total"], "entries": r["entries"],
            "file": os.path.join(STATE_DIR, "audit.jsonl")}


def tool_start_import(args: Dict[str, Any], progress_token: Any) -> Dict[str, Any]:
    locked = guard_writable()
    if locked:
        return locked
    raw_root = str((args or {}).get("root") or "").strip()
    if not raw_root:
        return {"error": "root_required",
                "note": "需要 root：要导入的 Markdown 目录（只读源）"}
    root = os.path.abspath(raw_root)
    if not os.path.exists(root):
        return {"error": "root_not_found", "root": norm(root)}
    if not os.path.isdir(root):
        return {"error": "root_not_directory", "root": norm(root)}

    import secrets as _secrets
    job_id = "job_" + _secrets.token_hex(5)
    job: Dict[str, Any] = {
        "job_id": job_id, "kind": "import", "root": norm(root), "status": "running",
        "phase": "starting", "done": 0, "total": 0, "errors": [],
        "cancel_requested": False, "started_at": _now(),
        "args": {"skip": (args or {}).get("skip"),
                 "scan_secrets": (args or {}).get("scan_secrets") is not False},
        "manifest_path": norm(os.path.join(IMPORTS_DIR, job_id + ".json")),
        "checkpoint_path": norm(os.path.join(IMPORTS_DIR, job_id + ".partial.json")),
    }
    STORE.record_job(job)
    th = threading.Thread(target=_run_import_safe, args=(job, dict(args or {}), progress_token),
                          daemon=True)
    th.start()
    return {"job_id": job_id, "status": "running", "root": job["root"],
            "progress_notifications": bool(progress_token)}


def _run_import_safe(job: Dict[str, Any], args: Dict[str, Any], progress_token: Any) -> None:
    try:
        run_import(job, args, progress_token)
    except Exception as e:
        finish_job(job, "failed", {"errors": [str(e)]})


def tool_get_job_status(args: Dict[str, Any]) -> Dict[str, Any]:
    job = STORE.state["jobs"].get(str((args or {}).get("job_id") or ""))
    if not job:
        return {"error": "unknown_job"}
    return {
        "job_id": job["job_id"], "kind": job.get("kind"), "phase": job.get("phase"),
        "status": job.get("status"), "done": job.get("done"), "total": job.get("total"),
        "errors": job.get("errors"), "root": job.get("root"),
        "manifest_path": job.get("manifest_path"),
        "manifest_written": bool(job.get("manifest_path")
                                 and os.path.exists(job["manifest_path"])),
        "counts": job.get("counts"), "index": job.get("index"),
        "resumed_from": job.get("resumed_from"),
        "resumable": bool(job.get("checkpoint_path")
                          and os.path.exists(job["checkpoint_path"])),
        "checkpoint_path": job.get("checkpoint_path"),
        "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
    }


def tool_cancel_job(args: Dict[str, Any]) -> Dict[str, Any]:
    locked = guard_writable()
    if locked:
        return locked
    job = STORE.state["jobs"].get(str((args or {}).get("job_id") or ""))
    if not job:
        return {"error": "unknown_job"}
    if job.get("status") != "running":
        return {"job_id": job["job_id"], "cancel_requested": False, "status": job["status"],
                "note": "job 已结束"}
    job["cancel_requested"] = True
    return {"job_id": job["job_id"], "cancel_requested": True}


# 断点续跑：只认中断/取消/失败后留下的检查点，起一个新 job 指向原 job
def tool_resume_job(args: Dict[str, Any], progress_token: Any) -> Dict[str, Any]:
    locked = guard_writable()
    if locked:
        return locked
    prev = STORE.state["jobs"].get(str((args or {}).get("job_id") or ""))
    if not prev:
        return {"error": "unknown_job"}
    if prev.get("status") == "running":
        return {"error": "already_running", "job_id": prev["job_id"]}
    ckpt = prev.get("checkpoint_path") or os.path.join(IMPORTS_DIR,
                                                       prev["job_id"] + ".partial.json")
    if not os.path.exists(ckpt):
        return {"error": "no_checkpoint", "job_id": prev["job_id"],
                "note": "没有可续跑的检查点（任务已完成，或中断前还没处理任何文件）"}
    if not os.path.exists(prev["root"]):
        return {"error": "root_not_found", "root": prev["root"]}

    import secrets as _secrets
    job_id = "job_" + _secrets.token_hex(5)
    job: Dict[str, Any] = {
        "job_id": job_id, "kind": "import", "root": prev["root"], "status": "running",
        "phase": "starting", "done": 0, "total": 0, "errors": [],
        "cancel_requested": False, "started_at": _now(),
        "resumed_from": prev["job_id"], "resume_from_path": norm(ckpt),
        "args": dict(prev.get("args") or {}),
        "manifest_path": norm(os.path.join(IMPORTS_DIR, job_id + ".json")),
        "checkpoint_path": norm(os.path.join(IMPORTS_DIR, job_id + ".partial.json")),
    }
    STORE.record_job(job)
    run_args = dict(job["args"])
    run_args["resume_from"] = ckpt
    th = threading.Thread(target=_run_import_safe, args=(job, run_args, progress_token),
                          daemon=True)
    th.start()
    return {"job_id": job_id, "resumed_from": prev["job_id"], "resume_from": norm(ckpt),
            "status": "running", "progress_notifications": bool(progress_token)}


# ---------- tools/list ----------
TOOLS: List[Dict[str, Any]] = [
    {"name": "search", "description": "本地 BM25 召回（标题×4）；语料根取 AWAM_INDEX_ROOT / KM_DATA_ROOT+KM_SOURCE_ID / settings.yaml，带增量索引缓存",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "topk": {"type": "number"}}, "required": ["query"]},
     "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {"name": "propose_write", "description": "生成写入提案（不落盘）；target_path 限定在写入根内",
     "inputSchema": {"type": "object", "properties": {"target_path": {"type": "string"}, "content": {"type": "string"}, "source": {"type": "string"}}, "required": ["target_path", "content"]},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False}},
    {"name": "commit_write", "description": "提交写入（需用户签发的一次性 token；原子写；token 用后持久化，重启后重放仍拒）",
     "inputSchema": {"type": "object", "properties": {"proposal_id": {"type": "string"}, "confirmation_token": {"type": "string"}}, "required": ["proposal_id", "confirmation_token"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}},
    {"name": "undo_write", "description": "撤销窗口内回滚一次已提交写入（文件被后续改动时拒绝）",
     "inputSchema": {"type": "object", "properties": {"proposal_id": {"type": "string"}}, "required": ["proposal_id"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}},
    {"name": "start_import", "description": "启动真实导入长任务：遍历 root 下 Markdown 与附件，产出 manifest（状态目录 imports/<job_id>.json），可取消",
     "inputSchema": {
         "type": "object",
         "properties": {
             "root": {"type": "string"},
             "skip": {"type": "string", "description": "逗号分隔的目录名；缺省用内置清单"},
             "scan_secrets": {"type": "boolean", "description": "默认 true"},
             "resume_from": {"type": "string", "description": "上一份 manifest 路径，按 size+mtime 复用未变文件"},
         },
         "required": ["root"],
     },
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "get_job_status", "description": "查询 job 状态（跨进程可见，状态持久化）",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
     "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {"name": "cancel_job", "description": "取消运行中的 job（下一个文件边界生效，不写 manifest）",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "resume_job", "description": "用中断/取消留下的检查点续跑导入（新 job，未变文件复用，不改源目录）",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "get_audit", "description": "读取写入闸审计（提案/提交/拒绝/撤销，只追加不重写）",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "number", "description": "返回最近 N 条，默认 100，上限 2000"},
         "since": {"type": "number", "description": "只返回该时间戳（毫秒）之后的记录"},
     }},
     "annotations": {"readOnlyHint": True, "destructiveHint": False}},
]


# ---------- 分发 ----------
def dispatch(msg: Dict[str, Any]) -> None:
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    def reply(result: Any) -> None:
        if mid is not None:
            _emit(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}, ensure_ascii=False))

    def fail(code: int, message: str) -> None:
        if mid is not None:
            _emit(json.dumps({"jsonrpc": "2.0", "id": mid,
                              "error": {"code": code, "message": message}},
                             ensure_ascii=False))

    if method == "initialize":
        ci = params.get("clientInfo") or {}
        CLIENT["name"] = str(ci.get("name") or "unknown")
        CLIENT["version"] = ci.get("version")
        return reply({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}, "logging": {}},
            "serverInfo": {"name": "awam-knowledge", "version": "0.2.0"},
        })
    if method == "notifications/initialized":
        return
    if method == "ping":
        return reply({})
    if method == "tools/list":
        return reply({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        progress_token = (params.get("_meta") or {}).get("progressToken")
        try:
            if name == "search":
                out = tool_search(args)
            elif name == "propose_write":
                out = tool_propose_write(args)
            elif name == "commit_write":
                out = tool_commit_write(args)
            elif name == "undo_write":
                out = tool_undo_write(args)
            elif name == "start_import":
                out = tool_start_import(args, progress_token)
            elif name == "get_job_status":
                out = tool_get_job_status(args)
            elif name == "cancel_job":
                out = tool_cancel_job(args)
            elif name == "resume_job":
                out = tool_resume_job(args, progress_token)
            elif name == "get_audit":
                out = tool_get_audit(args)
            else:
                return fail(-32601, "unknown tool: " + str(name))
        except Exception as e:
            return fail(-32000, str(e))
        is_error = bool(out and out.get("error"))
        return reply({"content": [{"type": "text",
                                   "text": json.dumps(out, ensure_ascii=False)}],
                      "isError": is_error})
    return fail(-32601, "method not found: " + str(method))


def cleanup() -> None:
    try:
        if LOCK.get("ok") and LOCK.get("release"):
            LOCK["release"]()
    except Exception:
        pass  # ignore


def main() -> int:
    for raw in sys.stdin:
        s = raw.strip()
        if not s:
            continue
        try:
            msg = json.loads(s)
        except ValueError:
            continue
        try:
            dispatch(msg)
        except Exception:
            pass  # 保持进程存活
    cleanup()
    return 0


if __name__ == "__main__":
    import signal

    def _sig(_sig_num, _frame):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    sys.exit(main())
