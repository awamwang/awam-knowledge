# SPDX-License-Identifier: MIT
"""MCP 自检（零依赖）：临时语料 + 临时状态目录，直连 stdio 断言真实行为。

    python tests/mcp_selftest.py

逐条移植自 Node code/mcp-selftest.js：spawn `python -m awam_knowledge.mcp_server`，
覆盖 search（命中/行号/片段/topk/排除目录）、写入闸（越界拒绝、坏签名、提交、重放、撤销）、
真实导入 job（进度通知、manifest、索引刷新、取消、断点续跑、崩溃恢复、进度落盘）、
跨重启状态（job/提案/撤销/审计）、单写者锁、未配置根。临时目录建在系统 temp 下，跑完删除。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
TMP = Path(tempfile.mkdtemp(prefix="awam-mcp-selftest-"))
CORPUS = TMP / "corpus"
STATE = TMP / "state"
WORKSPACE = TMP / "workspace"
SECRET = "selftest-secret-0123456789abcdef"


def write(p: Path, s: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(s, bytes):
        p.write_bytes(s)
    else:
        p.write_text(s, encoding="utf-8")


def write_corpus() -> None:
    write(CORPUS / "alpha.md", "# 向量检索\n\n倒排索引配合向量检索可以做混合召回。\n这一行是填充内容，用来拉开行号。\n")
    write(CORPUS / "beta.md", "# 搬家清单\n\n用程序员的思路搬家：先列依赖，再打包。\n")
    write(CORPUS / "ignored.md", "# 忽略我\n\n向量检索不应命中这里。\n")
    (CORPUS / ".git").mkdir(parents=True, exist_ok=True)
    write(CORPUS / ".git" / "hidden.md", "# 隐藏\n\n向量检索不应命中这里。\n")


def make_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    e = dict(os.environ)
    e.update({
        "PYTHONPATH": str(SRC),
        "PYTHONIOENCODING": "utf-8",
        "AWAM_STATE_DIR": str(STATE),
        "AWAM_INDEX_ROOT": str(CORPUS),
        "AWAM_WRITE_ROOT": str(WORKSPACE),
        "AWAM_SECRET": SECRET,
        "KM_SETTINGS": str(TMP / "absent-settings.yaml"),
    })
    for k in ("SPIKE_ROOT", "KM_DATA_ROOT", "DESENS_DATA_ROOT", "AWAM_SECRET_CMD",
              "KM_RAW_ROOTS"):
        e.pop(k, None)
    if extra:
        e.update(extra)
    return e


class Server:
    """子进程 mcp_server 的 stdio 客户端：请求按 id 配对，通知（progress）单独缓存。"""

    def __init__(self, env: Optional[Dict[str, str]] = None):
        self.stderr_file = TMP / ("server-%d.stderr.log" % int(time.time() * 1000))
        self._stderr = open(self.stderr_file, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "awam_knowledge.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            env=make_env(env), cwd=str(REPO))
        self.notifications: List[Dict[str, Any]] = []
        self._pending: Dict[int, queue.Queue] = {}
        self._id = 0
        self._lock = threading.Lock()
        t = threading.Thread(target=self._read_stdout, daemon=True)
        t.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            method = m.get("method")
            if method and str(method).startswith("notifications/"):
                self.notifications.append(m)
            mid = m.get("id")
            if mid is not None:
                with self._lock:
                    q = self._pending.pop(mid, None)
                if q is not None:
                    q.put(m)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: float = 15.0) -> Dict[str, Any]:
        with self._lock:
            self._id += 1
            mid = self._id
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._pending[mid] = q
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write((json.dumps(
                {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
                ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            return q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("timeout: " + method) from None
        finally:
            with self._lock:
                self._pending.pop(mid, None)

    def notify(self, method: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
                              .encode("utf-8"))
        self.proc.stdin.flush()

    def stop(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        self.stop()
        try:
            self._stderr.close()
        except OSError:
            pass


def start_server(env: Optional[Dict[str, str]] = None) -> Server:
    s = Server(env)
    r = s.call("initialize", {"clientInfo": {"name": "awam-selftest", "version": "1.0"}})
    if not (r.get("result") or {}).get("serverInfo"):
        raise RuntimeError("initialize 失败: " + json.dumps(r, ensure_ascii=False))
    s.notify("notifications/initialized")
    return s


def tool(server: Server, name: str, args: Optional[Dict[str, Any]] = None,
         meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"name": name, "arguments": args or {}}
    if meta:
        params["_meta"] = meta
    m = server.call("tools/call", params)
    content = (m.get("result") or {}).get("content")
    if not isinstance(content, list):
        raise RuntimeError("bad tools/call reply: " + json.dumps(m, ensure_ascii=False))
    return json.loads(content[0]["text"])


def make_token(proposal_id: str, baseline_hash: str, issued_at: Optional[int] = None) -> str:
    body = base64.urlsafe_b64encode(json.dumps({
        "proposal_id": proposal_id, "baseline_hash": baseline_hash,
        "issued_at": int(issued_at if issued_at is not None else time.time() * 1000),
    }).encode("utf-8")).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(hmac.new(
        SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    return body + "." + sig


def journal_job_progress(job_id: str) -> Optional[Dict[str, Any]]:
    """直接读状态日志，模拟「另一个进程」在 job 运行期间能看到什么。"""
    try:
        txt = (STATE / "state.jsonl").read_text(encoding="utf-8")
    except OSError:
        return None
    best: Optional[Dict[str, Any]] = None
    for line in txt.splitlines():
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # 正在追加的行可能不完整
        if ev.get("t") != "job" or not ev.get("job"):
            continue
        job = ev["job"]
        if job.get("job_id") != job_id or job.get("status") != "running":
            continue
        if best is None or (job.get("done") or 0) >= (best.get("done") or 0):
            best = job
    if best is None:
        return None
    return {"done": best.get("done"), "total": best.get("total"), "phase": best.get("phase")}


def wait_job(server: Server, job_id: str, timeout_ms: int = 30000) -> Dict[str, Any]:
    t0 = time.time()
    last: Optional[Dict[str, Any]] = None
    while time.time() - t0 < timeout_ms / 1000:
        last = tool(server, "get_job_status", {"job_id": job_id})
        if last and last.get("status") in ("done", "cancelled", "failed"):
            return last
        time.sleep(0.02)
    raise RuntimeError("job timeout: " + job_id + " last=" + json.dumps(last, ensure_ascii=False))


def main() -> int:
    write_corpus()
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: Any, detail: Any = None) -> None:
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    s1 = start_server()

    # ---- search ----
    hits = tool(s1, "search", {"query": "向量检索"})
    top = (hits.get("hits") or [None])[0]
    add("search 命中预期文件", top and top.get("path") == "alpha.md", top and top.get("path"))
    add("返回带行号", top and top.get("line", 0) >= 1)
    add("返回非空片段", top and isinstance(top.get("snippet"), str) and len(top["snippet"]) > 0)
    add("模式标注为 bm25-title4", hits.get("mode") == "bm25-title4")
    add("命中不含被排除目录", all(".git" not in h["path"] for h in hits.get("hits") or []))
    add("索引首次构建写缓存", hits.get("index", {}).get("cache", {}).get("enabled") is True,
        hits.get("index", {}).get("cache"))

    topk1 = tool(s1, "search", {"query": "向量检索", "topk": 1})
    add("topk 生效", len(topk1.get("hits") or []) == 1)

    empty = tool(s1, "search", {"query": "  "})
    add("空查询不报错", isinstance(empty.get("hits"), list) and len(empty["hits"]) == 0)

    # ---- 写入闸 ----
    outside = tool(s1, "propose_write", {"target_path": str(TMP.parent / "escape.md"), "content": "x"})
    add("写入根越界被拒", outside.get("error") == "path_outside_write_root", outside)

    p1 = tool(s1, "propose_write", {"target_path": "notes/one.md", "content": "# 一\n\n提案内容。\n"})
    add("提案返回 id 与基线 hash", bool(p1.get("proposal_id"))
        and isinstance(p1.get("baseline_hash"), str) and len(p1["baseline_hash"]) == 64)
    bad = tool(s1, "commit_write", {"proposal_id": p1["proposal_id"], "confirmation_token": "aaa.bbb"})
    add("坏签名被拒", bad.get("error") == "rejected" and bad.get("reason") == "bad_signature", bad)

    t1 = make_token(p1["proposal_id"], p1["baseline_hash"])
    commit1 = tool(s1, "commit_write", {"proposal_id": p1["proposal_id"], "confirmation_token": t1})
    add("有效 token 提交成功", commit1.get("committed") is True, commit1)
    add("提交走原子写（内容落盘）", "提案内容" in (WORKSPACE / "notes" / "one.md").read_text(encoding="utf-8"))
    add("提交后有撤销窗口", commit1.get("undo_available") is True)

    replay = tool(s1, "commit_write", {"proposal_id": p1["proposal_id"], "confirmation_token": t1})
    add("同进程重放被拒", replay.get("error") == "already_committed", replay)

    und1 = tool(s1, "undo_write", {"proposal_id": p1["proposal_id"]})
    add("撤销新建文件", und1.get("undone") is True and und1.get("restored") is False, und1)
    add("撤销后文件不存在", not (WORKSPACE / "notes" / "one.md").exists())
    und2 = tool(s1, "undo_write", {"proposal_id": p1["proposal_id"]})
    add("重复撤销被拒", und2.get("error") == "undo_already_done", und2)

    # ---- 审计：提案 / 提交 / 拒绝 / 撤销 各一条，且带身份 ----
    audit1 = tool(s1, "get_audit", {"limit": 20})
    evs = [e["event"] for e in audit1.get("entries") or []]
    add("审计记录了 propose", "propose" in evs, evs)
    add("审计记录了 commit", "commit" in evs)
    add("审计记录了被拒的提交及原因",
        any(e["event"] == "commit_rejected" and e.get("reason") == "bad_signature"
            for e in audit1.get("entries") or []))
    add("审计记录了 undo", "undo" in evs)
    add("审计带客户端身份",
        all(e.get("client") == "awam-selftest" for e in audit1.get("entries") or []),
        (audit1.get("entries") or [{}])[0])
    add("审计文件独立于状态日志", str(audit1.get("file") or "").endswith("audit.jsonl"), audit1.get("file"))
    committed_entry = next((e for e in audit1.get("entries") or [] if e["event"] == "commit"), None)
    add("审计记下审批时间与提交后 hash",
        bool(committed_entry) and bool(committed_entry.get("approved_at"))
        and isinstance(committed_entry.get("committed_hash"), str)
        and len(committed_entry["committed_hash"]) == 64, committed_entry)

    # P2：提交后不撤销，留给重启后验证
    p2 = tool(s1, "propose_write", {"target_path": "notes/persist.md", "content": "persisted proposal\n"})
    t2 = make_token(p2["proposal_id"], p2["baseline_hash"])
    commit2 = tool(s1, "commit_write", {"proposal_id": p2["proposal_id"], "confirmation_token": t2})
    add("第二份提案提交成功", commit2.get("committed") is True)

    # ---- 真实导入 + 索引刷新 ----
    write(CORPUS / "gamma.md", "# 伽马射线\n\n伽马射线暴是宇宙中最亮的电磁爆发现象。\n")
    start = tool(s1, "start_import", {"root": str(CORPUS)}, {"progressToken": "imp-1"})
    add("start_import 返回 job_id", bool(start.get("job_id")) and start.get("status") == "running", start)
    done = wait_job(s1, start["job_id"])
    add("导入 job 完成", done.get("status") == "done", done)
    add("导入计数：4 个 Markdown", (done.get("counts") or {}).get("markdown_files") == 4, done.get("counts"))
    add("manifest 已落盘", done.get("manifest_written") is True)
    manifest = json.loads(Path(done["manifest_path"]).read_text(encoding="utf-8"))
    add("manifest v2 且含摘要", manifest.get("version") == 2 and manifest.get("summary", {})
        .get("markdown", {}).get("files") == 4)
    add("导入后刷新索引", (done.get("index") or {}).get("refreshed") is True, done.get("index"))
    add("增量索引命中旧文件", (done.get("index") or {}).get("cache", {}).get("hits", 0) >= 3,
        (done.get("index") or {}).get("cache"))
    gamma = tool(s1, "search", {"query": "伽马射线暴"})
    add("导入后 search 能命中新文件",
        any(h["path"] == "gamma.md" for h in gamma.get("hits") or []),
        [h["path"] for h in gamma.get("hits") or []])
    add("导入发进度通知",
        any(n["method"] == "notifications/progress"
            and n["params"].get("progressToken") == "imp-1" for n in s1.notifications))

    # ---- 取消：大目录 ----
    busy = TMP / "busy"
    busy.mkdir(parents=True, exist_ok=True)
    for i in range(1200):
        write(busy / ("f%d.md" % i), "# 文件%d\n\n内容 %d\n" % (i, i))
    j2 = tool(s1, "start_import", {"root": str(busy)})
    cancel = tool(s1, "cancel_job", {"job_id": j2["job_id"]})
    add("取消请求被接受（或 job 已结束）", cancel.get("cancel_requested") is True or bool(cancel.get("note")), cancel)
    cancelled = wait_job(s1, j2["job_id"])
    add("job 状态为 cancelled 或无 manifest",
        (cancelled.get("manifest_written") is False) if cancelled.get("status") == "cancelled"
        else cancelled.get("status") == "done", cancelled)

    # ---- 断点续跑：中断留下检查点，resume_job 起新 job 复用未变文件 ----
    resumable_src = TMP / "resume-src"
    resumable_src.mkdir(parents=True, exist_ok=True)
    RESUME_FILES = 5000
    for i in range(RESUME_FILES):
        write(resumable_src / ("r%d.md" % i), "# 续跑%d\n\n内容 %d\n" % (i, i))
    j3 = tool(s1, "start_import", {"root": str(resumable_src)})
    # 等到检查点真的落盘再取消，否则这条测试会静默跳过续跑分支
    j3live: Optional[Dict[str, Any]] = None
    for _ in range(600):
        j3live = tool(s1, "get_job_status", {"job_id": j3["job_id"]})
        if not j3live or j3live.get("status") != "running" or j3live.get("resumable"):
            break
        time.sleep(0.005)
    tool(s1, "cancel_job", {"job_id": j3["job_id"]})
    j3done = wait_job(s1, j3["job_id"], 60000)
    add("取消前检查点已落盘", j3done.get("status") == "cancelled" and j3done.get("resumable") is True,
        {"status": j3done.get("status"), "resumable": j3done.get("resumable"),
         "checkpoint": j3done.get("checkpoint_path")})
    add("检查点文件真实存在", bool(j3done.get("checkpoint_path"))
        and Path(j3done["checkpoint_path"]).exists(), j3done.get("checkpoint_path"))

    resumed = tool(s1, "resume_job", {"job_id": j3["job_id"]})
    add("resume_job 返回新 job 且指向原 job", bool(resumed.get("job_id"))
        and resumed["job_id"] != j3["job_id"] and resumed.get("resumed_from") == j3["job_id"], resumed)
    resumed_done = wait_job(s1, resumed["job_id"], 120000)
    add("续跑后导入完成并写 manifest", resumed_done.get("status") == "done"
        and resumed_done.get("manifest_written") is True, resumed_done)
    resumed_manifest = json.loads(Path(resumed_done["manifest_path"]).read_text(encoding="utf-8"))
    add("续跑复用了中断前已处理的文件",
        resumed_manifest["summary"]["markdown"].get("unchanged", 0) >= 1,
        {"unchanged": resumed_manifest["summary"]["markdown"].get("unchanged"),
         "files": resumed_manifest["summary"]["markdown"].get("files")})
    add("续跑结果文件数与源一致",
        resumed_manifest["summary"]["markdown"].get("files") == RESUME_FILES,
        resumed_manifest["summary"]["markdown"].get("files"))
    add("完成后检查点被清理", not Path(j3done["checkpoint_path"]).exists()
        and resumed_done.get("resumable") is False)
    again = tool(s1, "resume_job", {"job_id": j3["job_id"]})
    add("无检查点时拒绝续跑", again.get("error") == "no_checkpoint", again)
    resume_unknown = tool(s1, "resume_job", {"job_id": "job_nope"})
    add("续跑未知 job 报 unknown_job", resume_unknown.get("error") == "unknown_job", resume_unknown)

    # ---- 进度数字：运行期间就与检查点同频落盘，另一个进程不必等结束 ----
    j5 = tool(s1, "start_import", {"root": str(resumable_src)})
    persisted: Optional[Dict[str, Any]] = None
    for _ in range(400):
        persisted = journal_job_progress(j5["job_id"])
        if persisted and persisted.get("done", 0) >= 49:
            break
        live = tool(s1, "get_job_status", {"job_id": j5["job_id"]})
        if not live or live.get("status") != "running":
            break
        time.sleep(0.01)
    add("运行中进度已落盘（≥49 个文件）",
        bool(persisted) and persisted.get("done", 0) >= 49
        and persisted.get("total") == RESUME_FILES, persisted)
    tool(s1, "cancel_job", {"job_id": j5["job_id"]})
    wait_job(s1, j5["job_id"], 60000)

    # ---- 崩溃：进程被直接杀掉，重启后应为 interrupted 且可续跑 ----
    j4 = tool(s1, "start_import", {"root": str(resumable_src)})
    time.sleep(0.2)
    j4before = tool(s1, "get_job_status", {"job_id": j4["job_id"]})
    s1.close()

    # ---- 重启：状态持久化 ----
    s2 = start_server()
    job_after = tool(s2, "get_job_status", {"job_id": start["job_id"]})
    add("重启后 job 仍可查且为 done", job_after.get("status") == "done"
        and job_after.get("manifest_written") is True, job_after)
    cancel_after = tool(s2, "get_job_status", {"job_id": j2["job_id"]})
    add("重启后取消状态保留", cancel_after.get("status") in ("cancelled", "done"), cancel_after.get("status"))

    crashed = tool(s2, "get_job_status", {"job_id": j4["job_id"]})
    add("崩溃：running job 重启后标记 interrupted", crashed.get("status") == "interrupted",
        {"before": j4before.get("status"), "after": crashed.get("status")})
    add("崩溃后检查点仍在，job 可续跑", crashed.get("resumable") is True, crashed.get("checkpoint_path"))
    add("崩溃前进度数字已落盘（另一进程可见）", (crashed.get("done") or 0) > 0
        and crashed.get("total") == RESUME_FILES,
        {"done": crashed.get("done"), "total": crashed.get("total")})
    if crashed.get("resumable"):
        resumed2 = tool(s2, "resume_job", {"job_id": j4["job_id"]})
        resumed2_done = wait_job(s2, resumed2["job_id"], 120000)
        m2 = json.loads(Path(resumed2_done["manifest_path"]).read_text(encoding="utf-8"))
        add("崩溃后续跑完成并复用已处理文件",
            resumed2_done.get("status") == "done"
            and m2["summary"]["markdown"].get("unchanged", 0) >= 1
            and m2["summary"]["markdown"].get("files") == RESUME_FILES,
            {"status": resumed2_done.get("status"),
             "unchanged": m2["summary"]["markdown"].get("unchanged"),
             "files": m2["summary"]["markdown"].get("files")})

    t2fresh = make_token(p2["proposal_id"], p2["baseline_hash"])
    replay_after = tool(s2, "commit_write", {"proposal_id": p2["proposal_id"], "confirmation_token": t2fresh})
    add("重启后提案仍为已提交", replay_after.get("error") == "already_committed", replay_after)

    und_after = tool(s2, "undo_write", {"proposal_id": p2["proposal_id"]})
    add("重启后撤销记录可用", und_after.get("undone") is True, und_after)
    audit_after = tool(s2, "get_audit", {"limit": 50})
    add("重启后审计仍在", len(audit_after.get("entries") or []) >= 5, audit_after.get("total"))
    add("撤销后持久化文件被删除", not (WORKSPACE / "notes" / "persist.md").exists())

    # ---- 单写者锁 ----
    s3 = start_server()
    locked = tool(s3, "start_import", {"root": str(CORPUS)})
    add("第二进程写入被锁拒绝", locked.get("error") == "state_locked", locked)
    read_only = tool(s3, "search", {"query": "向量检索"})
    add("第二进程仍可只读 search", isinstance(read_only.get("hits"), list)
        and len(read_only["hits"]) > 0)
    s3.close()
    s2.close()

    # ---- 未配置根 ----
    s4 = start_server({"AWAM_INDEX_ROOT": ""})
    misconfigured = tool(s4, "search", {"query": "向量检索"})
    add("未配置根时返回明确错误", misconfigured.get("error") == "index_root_not_configured", misconfigured)
    s4.close()

    failed = [c for c in checks if not c["pass"]]
    ok = not failed
    print(json.dumps({"selftest": True, "checks": len(checks), "pass": ok,
                      "failed": [f["name"] for f in failed], "detail": failed},
                     ensure_ascii=False, indent=2))
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
