# SPDX-License-Identifier: MIT
"""MCP 自检（零依赖）——逐字移植自 Node code/mcp-selftest.js。

    python tests/mcp_selftest.py

临时语料 + 临时状态目录，subprocess 直连 stdio 断言真实行为。
覆盖：search（命中/行号/片段/topk/排除目录/缓存/未配置根）、写入闸（越界拒绝、坏签名、
提交、重放、撤销）、审计（事件/身份/独立文件/审批时间）、真实导入 job（进度通知、manifest、
索引刷新、取消）、断点续跑（检查点、复用、清理、无检查点拒绝）、运行中进度落盘、崩溃恢复
（running→interrupted→resume）、跨重启状态、单写者锁。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SECRET = "selftest-secret-0123456789abcdef"
TMP = Path(tempfile.mkdtemp(prefix="awam-mcp-selftest-"))
CORPUS = TMP / "corpus"
STATE = TMP / "state"
WORKSPACE = TMP / "workspace"


def write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def write_corpus() -> None:
    write(CORPUS / "alpha.md", "# 向量检索\n\n倒排索引配合向量检索可以做混合召回。\n"
                               "这一行是填充内容，用来拉开行号。\n")
    write(CORPUS / "beta.md", "# 搬家清单\n\n用程序员的思路搬家：先列依赖，再打包。\n")
    write(CORPUS / "ignored.md", "# 忽略我\n\n向量检索不应命中这里。\n")
    (CORPUS / ".git").mkdir(parents=True, exist_ok=True)
    write(CORPUS / ".git" / "hidden.md", "# 隐藏\n\n向量检索不应命中这里。\n")


def base_env() -> dict:
    e = dict(os.environ)
    e.update({
        "AWAM_STATE_DIR": str(STATE),
        "AWAM_INDEX_ROOT": str(CORPUS),
        "AWAM_WRITE_ROOT": str(WORKSPACE),
        "AWAM_SECRET": SECRET,
        "KM_SETTINGS": str(TMP / "absent-settings.yaml"),
        "PYTHONPATH": str(REPO / "src"),
        "PYTHONIOENCODING": "utf-8",
    })
    for k in ("SPIKE_ROOT", "KM_DATA_ROOT", "DESENS_DATA_ROOT", "AWAM_SECRET_CMD"):
        e.pop(k, None)
    return e


class RpcClient:
    def __init__(self, child: subprocess.Popen):
        self.child = child
        self.notifications: list = []
        self.pending: dict = {}
        self._idc = 0
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.child.stdout is not None
        for raw in self.child.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("method", "").startswith("notifications/"):
                self.notifications.append(m)
            mid = m.get("id")
            if mid is not None:
                with self._lock:
                    fut = self.pending.pop(mid, None)
                if fut is not None:
                    fut["msg"] = m
                    fut["ev"].set()

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        with self._lock:
            self._idc += 1
            mid = self._idc
        fut = {"ev": threading.Event(), "msg": None}
        with self._lock:
            self.pending[mid] = fut
        assert self.child.stdin is not None
        self.child.stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                                            "params": params or {}}, ensure_ascii=False)
                                 + "\n").encode("utf-8"))
        self.child.stdin.flush()
        if not fut["ev"].wait(timeout):
            raise TimeoutError("timeout: " + method)
        return fut["msg"]

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.child.stdin is not None
        self.child.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method,
                                            "params": params or {}}, ensure_ascii=False)
                                + "\n").encode("utf-8"))
        self.child.stdin.flush()

    def stop(self, kill: bool = False) -> None:
        if self.child.poll() is not None:
            return
        if kill:
            self.child.kill()
        else:
            try:
                assert self.child.stdin is not None
                self.child.stdin.close()  # stdin 结束 → 正常退出
            except OSError:
                self.child.kill()
        try:
            self.child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=15)


def start_server(environment: dict | None = None) -> RpcClient:
    child = subprocess.Popen(
        [sys.executable, "-m", "awam_knowledge.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment or base_env(), cwd=str(REPO))
    api = RpcClient(child)
    api.call("initialize", {"clientInfo": {"name": "awam-selftest", "version": "1.0"}})
    api.notify("notifications/initialized")
    return api


def parse_tool(m: dict) -> dict:
    r = m.get("result") or {}
    if not isinstance(r.get("content"), list):
        raise RuntimeError("bad tools/call reply: " + json.dumps(m, ensure_ascii=False))
    return json.loads(r["content"][0]["text"])


def tool(server: RpcClient, name: str, args: dict, meta: dict | None = None) -> dict:
    params: dict = {"name": name, "arguments": args}
    if meta:
        params["_meta"] = meta
    return parse_tool(server.call("tools/call", params))


def make_token(proposal_id: str, baseline_hash: str, issued_at: int | None = None) -> str:
    body = base64.urlsafe_b64encode(json.dumps({
        "proposal_id": proposal_id, "baseline_hash": baseline_hash,
        "issued_at": issued_at or int(time.time() * 1000),
    }, ensure_ascii=False).encode("utf-8")).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(hmac.new(SECRET.encode("utf-8"), body.encode("utf-8"),
                                            hashlib.sha256).digest()).rstrip(b"=").decode("ascii")
    return body + "." + sig


def journal_job_progress(job_id: str) -> dict | None:
    best = None
    try:
        txt = (STATE / "state.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in txt.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # 正在追加的行可能不完整
        j = ev.get("job")
        if ev.get("t") != "job" or not j or j.get("job_id") != job_id or j.get("status") != "running":
            continue
        if best is None or (j.get("done") or 0) >= (best.get("done") or 0):
            best = j
    return {"done": best.get("done", 0), "total": best.get("total", 0),
            "phase": best.get("phase")} if best else None


def wait_job(server: RpcClient, job_id: str, timeout_ms: float = 15000) -> dict:
    t0 = time.time()
    last = None
    while (time.time() - t0) * 1000 < timeout_ms:
        last = tool(server, "get_job_status", {"job_id": job_id})
        if last and last.get("status") in ("done", "cancelled", "failed"):
            return last
        time.sleep(0.02)
    raise RuntimeError("job timeout: " + job_id + " last=" + json.dumps(last, ensure_ascii=False))


def main() -> int:
    write_corpus()
    checks: list = []

    def add(name: str, passed: bool, detail=None) -> None:
        checks.append({"name": name, "pass": bool(passed), "detail": detail})

    s1 = start_server()

    # ---- search ----
    hits = tool(s1, "search", {"query": "向量检索"})
    top = (hits.get("hits") or [{}])[0]
    add("search 命中预期文件", top.get("path") == "alpha.md", top.get("path"))
    add("返回带行号", isinstance(top.get("line"), int) and top["line"] >= 1)
    add("返回非空片段", isinstance(top.get("snippet"), str) and len(top["snippet"]) > 0)
    add("模式标注为 bm25-title4", hits.get("mode") == "bm25-title4")
    add("命中不含被排除目录", all(".git" not in h["path"] for h in hits.get("hits", [])))
    add("索引首次构建写缓存", bool(hits.get("index", {}).get("cache", {}).get("enabled")),
        hits.get("index", {}).get("cache"))

    topk1 = tool(s1, "search", {"query": "向量检索", "topk": 1})
    add("topk 生效", len(topk1.get("hits", [])) == 1)

    empty = tool(s1, "search", {"query": "  "})
    add("空查询不报错", isinstance(empty.get("hits"), list) and len(empty["hits"]) == 0)

    # ---- 写入闸 ----
    outside = tool(s1, "propose_write", {"target_path": str(TMP.parent / "escape.md"),
                                         "content": "x"})
    add("写入根越界被拒", outside.get("error") == "path_outside_write_root", outside)

    p1 = tool(s1, "propose_write", {"target_path": "notes/one.md", "content": "# 一\n\n提案内容。\n"})
    import re
    add("提案返回 id 与基线 hash", bool(p1.get("proposal_id"))
        and re.fullmatch(r"[0-9a-f]{64}", p1.get("baseline_hash") or "") is not None)
    bad = tool(s1, "commit_write", {"proposal_id": p1["proposal_id"], "confirmation_token": "aaa.bbb"})
    add("坏签名被拒", bad.get("error") == "rejected" and bad.get("reason") == "bad_signature", bad)

    t1 = make_token(p1["proposal_id"], p1["baseline_hash"])
    commit1 = tool(s1, "commit_write", {"proposal_id": p1["proposal_id"], "confirmation_token": t1})
    add("有效 token 提交成功", commit1.get("committed") is True, commit1)
    add("提交走原子写（内容落盘）",
        "提案内容" in (WORKSPACE / "notes" / "one.md").read_text(encoding="utf-8"))
    add("提交后有撤销窗口", commit1.get("undo_available") is True)

    replay = tool(s1, "commit_write", {"proposal_id": p1["proposal_id"], "confirmation_token": t1})
    add("同进程重放被拒", replay.get("error") == "already_committed", replay)

    und1 = tool(s1, "undo_write", {"proposal_id": p1["proposal_id"]})
    add("撤销新建文件", und1.get("undone") is True and und1.get("restored") is False, und1)
    add("撤销后文件不存在", not (WORKSPACE / "notes" / "one.md").exists())
    und2 = tool(s1, "undo_write", {"proposal_id": p1["proposal_id"]})
    add("重复撤销被拒", und2.get("error") == "undo_already_done", und2)

    # ---- 审计 ----
    audit1 = tool(s1, "get_audit", {"limit": 20})
    evs = [e.get("event") for e in audit1.get("entries", [])]
    add("审计记录了 propose", "propose" in evs, evs)
    add("审计记录了 commit", "commit" in evs, evs)
    add("审计记录了被拒的提交及原因",
        any(e.get("event") == "commit_rejected" and e.get("reason") == "bad_signature"
            for e in audit1.get("entries", [])))
    add("审计记录了 undo", "undo" in evs, evs)
    add("审计带客户端身份", all(e.get("client") == "awam-selftest"
                              for e in audit1.get("entries", [])),
        (audit1.get("entries") or [{}])[0])
    add("审计文件独立于状态日志", str(audit1.get("file", "")).endswith("audit.jsonl"),
        audit1.get("file"))
    committed_entry = next((e for e in audit1.get("entries", [])
                            if e.get("event") == "commit"), None)
    add("审计记下审批时间与提交后 hash",
        bool(committed_entry) and bool(committed_entry.get("approved_at"))
        and re.fullmatch(r"[0-9a-f]{64}", committed_entry.get("committed_hash") or "") is not None,
        committed_entry)

    # P2：提交后不撤销，留给重启后验证
    p2 = tool(s1, "propose_write", {"target_path": "notes/persist.md",
                                    "content": "persisted proposal\n"})
    t2 = make_token(p2["proposal_id"], p2["baseline_hash"])
    commit2 = tool(s1, "commit_write", {"proposal_id": p2["proposal_id"], "confirmation_token": t2})
    add("第二份提案提交成功", commit2.get("committed") is True)

    # ---- 真实导入 + 索引刷新 ----
    write(CORPUS / "gamma.md", "# 伽马射线\n\n伽马射线暴是宇宙中最亮的电磁爆发现象。\n")
    start = tool(s1, "start_import", {"root": str(CORPUS)}, {"progressToken": "imp-1"})
    add("start_import 返回 job_id", bool(start.get("job_id")) and start.get("status") == "running",
        start)
    done = wait_job(s1, start["job_id"])
    add("导入 job 完成", done.get("status") == "done", done)
    add("导入计数：4 个 Markdown", done.get("counts", {}).get("markdown_files") == 4,
        done.get("counts"))
    add("manifest 已落盘", done.get("manifest_written") is True)
    manifest = json.loads(Path(done["manifest_path"]).read_text(encoding="utf-8"))
    add("manifest v2 且含摘要", manifest.get("version") == 2
        and manifest.get("summary", {}).get("markdown", {}).get("files") == 4)
    add("导入后刷新索引", done.get("index", {}).get("refreshed") is True, done.get("index"))
    add("增量索引命中旧文件", (done.get("index", {}).get("cache") or {}).get("hits", 0) >= 3,
        done.get("index", {}).get("cache"))
    gamma = tool(s1, "search", {"query": "伽马射线暴"})
    add("导入后 search 能命中新文件",
        any(h.get("path") == "gamma.md" for h in gamma.get("hits", [])),
        [h.get("path") for h in gamma.get("hits", [])])
    add("导入发进度通知",
        any(n.get("method") == "notifications/progress"
            and n.get("params", {}).get("progressToken") == "imp-1"
            for n in s1.notifications))

    # ---- 取消：大目录 ----
    busy = TMP / "busy"
    for i in range(1200):
        write(busy / ("f%d.md" % i), "# 文件%d\n\n内容 %d\n" % (i, i))
    j2 = tool(s1, "start_import", {"root": str(busy)})
    cancel = tool(s1, "cancel_job", {"job_id": j2["job_id"]})
    add("取消请求被接受（或 job 已结束）",
        cancel.get("cancel_requested") is True or bool(cancel.get("note")), cancel)
    cancelled = wait_job(s1, j2["job_id"], 30000)
    add("job 状态为 cancelled 或无 manifest",
        (cancelled.get("manifest_written") is False)
        if cancelled.get("status") == "cancelled" else cancelled.get("status") == "done",
        cancelled)

    # ---- 断点续跑 ----
    resumable_src = TMP / "resume-src"
    RESUME_FILES = 5000
    for i in range(RESUME_FILES):
        write(resumable_src / ("r%d.md" % i), "# 续跑%d\n\n内容 %d\n" % (i, i))
    j3 = tool(s1, "start_import", {"root": str(resumable_src)})
    j3live = None
    for _ in range(600):
        j3live = tool(s1, "get_job_status", {"job_id": j3["job_id"]})
        if not j3live or j3live.get("status") != "running" or j3live.get("resumable"):
            break
        time.sleep(0.005)
    tool(s1, "cancel_job", {"job_id": j3["job_id"]})
    j3done = wait_job(s1, j3["job_id"], 30000)
    add("取消前检查点已落盘", j3done.get("status") == "cancelled" and j3done.get("resumable") is True,
        {"status": j3done.get("status"), "resumable": j3done.get("resumable"),
         "checkpoint": j3done.get("checkpoint_path")})
    add("检查点文件真实存在", bool(j3done.get("checkpoint_path"))
        and Path(j3done["checkpoint_path"]).exists(), j3done.get("checkpoint_path"))

    resumed = tool(s1, "resume_job", {"job_id": j3["job_id"]})
    add("resume_job 返回新 job 且指向原 job", bool(resumed.get("job_id"))
        and resumed.get("job_id") != j3["job_id"]
        and resumed.get("resumed_from") == j3["job_id"], resumed)
    resumed_done = wait_job(s1, resumed["job_id"], 60000)
    add("续跑后导入完成并写 manifest", resumed_done.get("status") == "done"
        and resumed_done.get("manifest_written") is True, resumed_done)
    rm = json.loads(Path(resumed_done["manifest_path"]).read_text(encoding="utf-8"))
    add("续跑复用了中断前已处理的文件", rm["summary"]["markdown"]["unchanged"] >= 1,
        {"unchanged": rm["summary"]["markdown"]["unchanged"],
         "files": rm["summary"]["markdown"]["files"]})
    add("续跑结果文件数与源一致", rm["summary"]["markdown"]["files"] == RESUME_FILES,
        rm["summary"]["markdown"]["files"])
    add("完成后检查点被清理", not Path(j3done["checkpoint_path"]).exists()
        and resumed_done.get("resumable") is False)
    again = tool(s1, "resume_job", {"job_id": j3["job_id"]})
    add("无检查点时拒绝续跑", again.get("error") == "no_checkpoint", again)
    resume_unknown = tool(s1, "resume_job", {"job_id": "job_nope"})
    add("续跑未知 job 报 unknown_job", resume_unknown.get("error") == "unknown_job",
        resume_unknown)

    # ---- 运行中进度落盘 ----
    j5 = tool(s1, "start_import", {"root": str(resumable_src)})
    persisted = None
    for _ in range(400):
        persisted = journal_job_progress(j5["job_id"])
        if persisted and persisted["done"] >= 49:
            break
        live = tool(s1, "get_job_status", {"job_id": j5["job_id"]})
        if not live or live.get("status") != "running":
            break
        time.sleep(0.01)
    add("运行中进度已落盘（≥49 个文件）", bool(persisted) and persisted["done"] >= 49
        and persisted["total"] == RESUME_FILES, persisted)
    tool(s1, "cancel_job", {"job_id": j5["job_id"]})
    wait_job(s1, j5["job_id"], 30000)

    # ---- 崩溃：直接杀进程，重启后 running→interrupted 且可续跑 ----
    j4 = tool(s1, "start_import", {"root": str(resumable_src)})
    time.sleep(0.15)
    j4before = tool(s1, "get_job_status", {"job_id": j4["job_id"]})
    s1.stop(kill=True)

    s2 = start_server()
    job_after = tool(s2, "get_job_status", {"job_id": start["job_id"]})
    add("重启后 job 仍可查且为 done", job_after.get("status") == "done"
        and job_after.get("manifest_written") is True, job_after)
    cancel_after = tool(s2, "get_job_status", {"job_id": j2["job_id"]})
    add("重启后取消状态保留", cancel_after.get("status") in ("cancelled", "done"),
        cancel_after.get("status"))

    crashed = tool(s2, "get_job_status", {"job_id": j4["job_id"]})
    add("崩溃：running job 重启后标记 interrupted", crashed.get("status") == "interrupted",
        {"before": j4before.get("status"), "after": crashed.get("status")})
    add("崩溃后检查点仍在，job 可续跑", crashed.get("resumable") is True,
        crashed.get("checkpoint_path"))
    add("崩溃前进度数字已落盘（另一进程可见）", (crashed.get("done") or 0) > 0
        and crashed.get("total") == RESUME_FILES,
        {"done": crashed.get("done"), "total": crashed.get("total")})
    if crashed.get("resumable"):
        resumed2 = tool(s2, "resume_job", {"job_id": j4["job_id"]})
        resumed2_done = wait_job(s2, resumed2["job_id"], 60000)
        m2 = json.loads(Path(resumed2_done["manifest_path"]).read_text(encoding="utf-8"))
        add("崩溃后续跑完成并复用已处理文件", resumed2_done.get("status") == "done"
            and m2["summary"]["markdown"]["unchanged"] >= 1
            and m2["summary"]["markdown"]["files"] == RESUME_FILES,
            {"status": resumed2_done.get("status"),
             "unchanged": m2["summary"]["markdown"]["unchanged"],
             "files": m2["summary"]["markdown"]["files"]})

    t2fresh = make_token(p2["proposal_id"], p2["baseline_hash"])
    replay_after = tool(s2, "commit_write", {"proposal_id": p2["proposal_id"],
                                             "confirmation_token": t2fresh})
    add("重启后提案仍为已提交", replay_after.get("error") == "already_committed", replay_after)

    und_after = tool(s2, "undo_write", {"proposal_id": p2["proposal_id"]})
    add("重启后撤销记录可用", und_after.get("undone") is True, und_after)
    audit_after = tool(s2, "get_audit", {"limit": 50})
    add("重启后审计仍在", len(audit_after.get("entries", [])) >= 5, audit_after.get("total"))
    add("撤销后持久化文件被删除", not (WORKSPACE / "notes" / "persist.md").exists())

    # ---- 单写者锁 ----
    s3 = start_server()
    locked = tool(s3, "start_import", {"root": str(CORPUS)})
    add("第二进程写入被锁拒绝", locked.get("error") == "state_locked", locked)
    read_only = tool(s3, "search", {"query": "向量检索"})
    add("第二进程仍可只读 search", isinstance(read_only.get("hits"), list)
        and len(read_only["hits"]) > 0)
    s3.stop()
    s2.stop()

    # ---- 未配置根 ----
    no_root_env = base_env()
    no_root_env["AWAM_INDEX_ROOT"] = ""
    s4 = start_server(no_root_env)
    misconfigured = tool(s4, "search", {"query": "向量检索"})
    add("未配置根时返回明确错误", misconfigured.get("error") == "index_root_not_configured",
        misconfigured)
    s4.stop()

    failed = [c for c in checks if not c["pass"]]
    ok = not failed
    print(json.dumps({"selftest": True, "checks": len(checks), "pass": ok,
                      "failed": [f["name"] for f in failed], "detail": failed,
                      "corpus": str(TMP)}, ensure_ascii=False, indent=2))
    if not ok:
        print("(fixture kept for debugging at %s)" % TMP, file=sys.stderr)
    else:
        shutil.rmtree(TMP, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
