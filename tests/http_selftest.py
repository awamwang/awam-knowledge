# SPDX-License-Identifier: MIT
"""http 服务 selftest：逐条移植自 Node http-server.js selftest，证明 Python 版满足同一对外契约
（= AFM knowledge_service.rs 所依赖的形状/状态码/边界/鉴权）。运行：python -m awam_knowledge.http_service --selftest
或 python tests/http_selftest.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from awam_knowledge import http_service  # noqa: E402


def _req(port, method, path, body=None, token=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") or "{}"
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except ValueError:
            return e.code, {"raw": raw}, dict(e.headers)


def run_selftest() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="km-http-selftest-"))
    idx_root = tmp / "idx"
    (idx_root / "sub").mkdir(parents=True, exist_ok=True)
    (idx_root / "accounts.md").write_text(
        "# 账号笔记\nAPI key 存放在 见保险库：OpenAI 主账号 [vault:llm-key:1a2b3c4d]。\n"
        "另外还有一个裸占位 [REDACTED:pw:99887766] 供测试。\n", encoding="utf-8")
    (idx_root / "daily.md").write_text(
        "# 日常\n今天研究了 BM25 检索与标题加权。\n", encoding="utf-8")
    multi = ["# 多块文档"] + [f"uniquekeyword 第{i}行内容足够长以区分不同块" for i in range(1, 61)]
    (idx_root / "multi.md").write_text("\n".join(multi) + "\n", encoding="utf-8")

    os.environ.pop("KM_RAW_ROOTS", None)
    cfg = http_service.load_config({
        "KM_SERVICE_TOKEN": "selftest-token-123", "KM_SERVICE_PORT": "0", "KM_SERVICE_LOG": "0",
        "AWAM_INDEX_ROOT": str(idx_root), "AWAM_INDEX_CACHE": str(tmp / "cache.json"),
    })
    httpd = http_service.create_server(cfg)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    passed = 0
    failures = []

    def ok(cond, label):
        nonlocal passed
        if cond:
            passed += 1
            print("  ok - " + label)
        else:
            failures.append(label)
            print("  FAIL - " + label)

    try:
        s, j, h = _req(port, "GET", "/health")
        ok(s == 200 and j.get("ok") is True and j.get("service") == "awam-knowledge-http", "/health 200 无鉴权")
        ok(j["index"]["configured"] is True, "/health 报告索引根已配置")
        ok(j["vault"]["configured"] is False, "/health 报告 vault 未配置")

        s, _, _ = _req(port, "POST", "/search", {"query": "BM25"})
        ok(s == 401, "/search 无 token → 401")
        s, _, _ = _req(port, "POST", "/search", {"query": "BM25"}, "wrong-token")
        ok(s == 401, "/search 错 token → 401")

        s, j, h = _req(port, "POST", "/search", {"query": "BM25"}, cfg["token"])
        ok(s == 200 and len(j["hits"]) == 1 and j["hits"][0]["path"] == "daily.md", "/search 命中 daily.md")
        ok(h.get("Cache-Control") == "no-store", "响应 Cache-Control: no-store")

        s, j, _ = _req(port, "POST", "/search", {"query": "账号 API key"}, cfg["token"])
        refs = j.get("refs") or []
        vref = next((x for x in refs if x["kind"] == "vault"), None)
        rref = next((x for x in refs if x["kind"] == "redacted"), None)
        ok(s == 200 and len(j["hits"]) >= 1, "/search 命中账号笔记")
        ok(vref and vref["key"] == "[REDACTED:llm-key:1a2b3c4d]" and vref["display"] == "OpenAI 主账号", "vault ref 提取")
        ok(rref and rref["key"] == "[REDACTED:pw:99887766]" and rref["display"] == "pw", "redacted ref 提取")

        s, j, _ = _req(port, "POST", "/search", {"query": "  "}, cfg["token"])
        ok(s == 200 and j["hits"] == [] and j["total"] == 0, "/search 空查询 → hits []")
        s, j, _ = _req(port, "POST", "/search", {"query": "BM25", "topk": 999}, cfg["token"])
        ok(s == 200, "/search topk 夹取不报错")

        s, j, _ = _req(port, "POST", "/search", {"query": "uniquekeyword"}, cfg["token"])
        ok(s == 200 and len(j["hits"]) > 1 and all(x["path"] == "multi.md" for x in j["hits"]), "/search 无 dedup 多块")
        s, j, _ = _req(port, "POST", "/search", {"query": "uniquekeyword", "dedup": "file", "topk": 5}, cfg["token"])
        ok(s == 200 and len(j["hits"]) == 1 and j["hits"][0]["path"] == "multi.md", "/search dedup=file 去重")
        ok(j.get("dedup") == "file" and isinstance(j.get("root"), str) and j["index"]["root"] == j["root"], "/search dedup 带 root")
        s, j, _ = _req(port, "POST", "/search", {"query": "uniquekeyword", "dedup": "other"}, cfg["token"])
        ok(s == 200 and "dedup" not in j, "/search 未知 dedup 忽略")

        s, _, _ = _req(port, "POST", "/doc", {"path": "accounts.md"})
        ok(s == 401, "/doc 无 token → 401")
        s, j, _ = _req(port, "POST", "/doc", {"path": "accounts.md"}, cfg["token"])
        ok(s == 200 and j.get("ok") is True and "账号笔记" in j["content"] and j["root"] == str(idx_root.resolve()),
           "/doc 读全文 + 索引根")
        s, j, _ = _req(port, "POST", "/doc", {"path": "nope.md"}, cfg["token"])
        ok(s == 404 and j.get("code") == "doc_not_found", "/doc 缺失 → 404")
        s, j, _ = _req(port, "POST", "/doc", {"path": "../escape.md"}, cfg["token"])
        ok(s == 400 and j.get("code") == "invalid_path", "/doc .. 穿越 → 400")
        s, j, _ = _req(port, "POST", "/doc", {"path": str((tmp / "outside.md").resolve())}, cfg["token"])
        ok(s == 400 and j.get("code") == "invalid_path", "/doc 绝对路径 → 400")
        s, j, _ = _req(port, "POST", "/doc", {"path": ""}, cfg["token"])
        ok(s == 400 and j.get("code") == "invalid_path", "/doc 空路径 → 400")
        s, j, _ = _req(port, "POST", "/doc", {}, cfg["token"])
        ok(s == 400 and j.get("code") == "invalid_path", "/doc 缺 path → 400")

        s, j, _ = _req(port, "POST", "/vault/status", {}, cfg["token"])
        ok(s == 503 and j.get("code") == "vault_not_configured", "/vault/status 未配置 → 503")
        s, j, _ = _req(port, "POST", "/vault/reveal", {"entry": "[REDACTED:llm-key:1a2b3c4d]"}, cfg["token"])
        ok(s == 503 and j.get("code") == "vault_not_configured", "/vault/reveal 未配置 → 503")
        s, j, _ = _req(port, "POST", "/vault/unlock", {"confirm": False}, cfg["token"])
        ok(s == 400 and j.get("code") == "confirm_required", "/vault/unlock 缺 confirm → 400")
        s, j, _ = _req(port, "POST", "/vault/reveal", {"entry": "not a valid entry"}, cfg["token"])
        ok(s == 400 and j.get("code") == "invalid_entry", "/vault/reveal 非法 entry → 400")

        s, _, _ = _req(port, "GET", "/nope", None, cfg["token"])
        ok(s == 404, "未知路径 → 404")
    finally:
        httpd.shutdown()
        httpd.server_close()

    if failures:
        print(f"http selftest: {len(failures)} 失败: {failures}")
        return False
    print(f"http-server selftest: {passed} 项全部通过")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if run_selftest() else 1)
