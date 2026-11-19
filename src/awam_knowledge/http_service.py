# SPDX-License-Identifier: MIT
"""awam-knowledge HTTP 服务（AFM 集成）：内容检索 + vault 人机通道代理。

逐字移植自 Node code/http-server.js，契约见 design/afm-http-service-design.md，
与 AFM 客户端 src-tauri/src/knowledge_service.rs 完全对齐（端口 18790、Bearer 常量时间、
/search 含 dedup=file 与 refs 提取、/doc 边界防护、/vault/* 未配置 503）。
stdlib http.server，零额外依赖。红线：明文只出现在 /vault/reveal 响应体，不写日志/缓存。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import bm25
from .index_root import validate_index_root

SERVICE_NAME = "awam-knowledge-http"
BODY_LIMIT = 1024 * 1024
VAULT_TIMEOUT_MS = 10000
DEFAULT_TOPK = 12
MAX_TOPK = 50
DOC_MAX_BYTES = 2 * 1024 * 1024
DEDUP_FETCH_MULTIPLIER = 5

VAULT_REF_RX = re.compile(r"见保险库：([^\[\]\n]{1,120}?)\s*\[vault:([A-Za-z0-9_\-]+):([0-9a-f]{8})\]")
REDACTED_RX = re.compile(r"\[REDACTED:([A-Za-z0-9_\-]+):([0-9a-f]{8})\]")
ENTRY_NAME_RX = re.compile(r"^\[REDACTED:[A-Za-z0-9_\-]+:[0-9a-f]{8}\]$")


def extract_refs(text: str) -> List[Dict[str, str]]:
    seen: Dict[str, Dict[str, str]] = {}
    order: List[str] = []

    def push(kind: str, key: str, display: str) -> None:
        prev = seen.get(key)
        if prev is None:
            order.append(key)
            seen[key] = {"kind": kind, "key": key, "display": display}
        elif len(display) > len(prev["display"]):
            seen[key] = {"kind": kind, "key": key, "display": display}

    for m in VAULT_REF_RX.finditer(text):
        push("vault", f"[REDACTED:{m.group(2)}:{m.group(3)}]", m.group(1).strip())
    for m in REDACTED_RX.finditer(text):
        push("redacted", m.group(0), m.group(1))
    return [seen[k] for k in order]


def load_config(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    env = env if env is not None else os.environ
    g = lambda k, d="": str(env.get(k, d) or "").strip()
    token = g("KM_SERVICE_TOKEN")
    vault_script = g("KM_SECRET_SCRIPT")
    vault_token = g("KM_SECRET_AGENT_TOKEN")
    vault_id = g("KM_SECRET_ID")
    vault_data_root = g("KM_SECRET_DATA_ROOT")
    cache = env.get("AWAM_INDEX_CACHE")
    index_cache = str(Path(cache).resolve()) if cache else str(
        Path(env.get("AWAM_STATE_DIR") or str(Path.home())).joinpath(".awam-knowledge-http", "index-cache.json").resolve())
    return {
        "host": g("KM_SERVICE_HOST", "127.0.0.1") or "127.0.0.1",
        "port": int(env.get("KM_SERVICE_PORT") or "18790"),
        "token": token,
        "log": (env.get("KM_SERVICE_LOG", "1") != "0"),
        "indexRoot": g("AWAM_INDEX_ROOT"),
        "excludes": bm25.parse_excludes(env.get("AWAM_EXCLUDES")),
        "chunkLines": int(env.get("AWAM_CHUNK_LINES") or bm25.DEFAULT_CHUNK_LINES),
        "titleWeight": int(env.get("AWAM_TITLE_WEIGHT") or bm25.DEFAULT_TITLE_WEIGHT),
        "indexCache": index_cache,
        "vault": {
            "py": g("KM_SECRET_PY", "python") or "python",
            "script": vault_script,
            "token": vault_token,
            "id": vault_id,
            "dataRoot": vault_data_root,
            "configured": bool(vault_script and vault_token and (vault_data_root or vault_id)),
        },
    }


class IndexState:
    """懒加载 + 单次构建缓存（语义对齐 makeIndex/ensure）。"""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self._corpus: Optional[Dict[str, Any]] = None
        self._idx: Optional[Dict[str, Any]] = None
        self._info: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        if self._idx is not None or (self._info is not None and self._info.get("error")):
            return
        v = validate_index_root(self.cfg["indexRoot"])
        if not v["ok"]:
            self._info = {"error": v["error"], "reason": v.get("reason")}
            return
        corpus = bm25.build_corpus({
            "root": v["root"], "excludes": self.cfg["excludes"],
            "chunkLines": self.cfg["chunkLines"], "titleWeight": self.cfg["titleWeight"],
        })
        idx = bm25.build_index(corpus["docs"])
        self._corpus = corpus
        self._idx = idx
        self._info = {
            "root": v["root"],
            "docs": len({d["path"] for d in corpus["docs"]}),
            "chunks": idx["N"],
        }

    def reset(self) -> None:
        with self._lock:
            self._corpus = self._idx = self._info = None

    def root_info(self) -> Dict[str, Any]:
        v = validate_index_root(self.cfg["indexRoot"])
        if v["ok"]:
            return {"configured": True, "root": v["root"],
                    "docs": self._info["docs"] if self._info and "docs" in self._info else None,
                    "chunks": self._info["chunks"] if self._info and "chunks" in self._info else None}
        return {"configured": False, "root": None, "docs": None, "chunks": None, "reason": v.get("reason")}

    def search(self, q: str, topk: int, snippet_width: int) -> Dict[str, Any]:
        with self._lock:
            self._ensure()
            if not self._idx:
                return {"error": "index_root_not_configured",
                        "reason": (self._info or {}).get("error")}
            weights = bm25.query_weights(q, {"stem": False})
            terms = list(weights.keys())
            ranked = bm25.rank_all(self._corpus["docs"], self._idx, weights)[:topk]
            hits = []
            for r in ranked:
                d = self._corpus["docs"][r["di"]]
                hits.append({
                    "path": d["path"], "line": d["lineStart"],
                    "lines": f"{d['lineStart']}-{d['lineEnd']}",
                    "title": d["title"], "score": round(r["score"], 4),
                    "snippet": bm25.snippet_of(d["text"], terms, snippet_width),
                    "refs": extract_refs(d["text"]),
                })
            return {"hits": hits, "index": {"root": self._info["root"], "docs": self._info["docs"], "chunks": self._info["chunks"]}}


def _vault_run(cfg: Dict[str, Any], args: List[str], want_json: bool = True) -> Dict[str, Any]:
    v = cfg["vault"]
    base = ["--data-root", v["dataRoot"]] if v["dataRoot"] else ["--id", v["id"]]
    if not v["configured"]:
        return {"ok": False, "status": 503, "code": "vault_not_configured",
                "reason": "未配置 KM_SECRET_SCRIPT / KM_SECRET_AGENT_TOKEN / KM_SECRET_ID(或 KM_SECRET_DATA_ROOT)"}
    try:
        proc = subprocess.run([v["py"], v["script"], *base, *args],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=VAULT_TIMEOUT_MS / 1000,
                              env={**os.environ, "AGENT_SECRET_TOKEN": v["token"]})
    except FileNotFoundError:
        return {"ok": False, "status": 503, "code": "vault_backend_error", "reason": f"python 不可用：{v['py']}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": 503, "code": "vault_timeout", "reason": f"vault 子进程超时（>{VAULT_TIMEOUT_MS}ms）"}
    if not want_json:
        if proc.returncode == 0:
            return {"ok": True, "raw": True}
        tail = (proc.stderr or "").strip()[:200]
        return {"ok": False, "status": 503, "code": "vault_backend_error", "reason": "vault 子进程失败" + (f"：{tail}" if tail else "")}
    lines = [s for s in (proc.stdout or "").splitlines() if s.strip()]
    last = lines[-1] if lines else ""
    try:
        j = json.loads(last)
    except ValueError:
        j = None
    if not isinstance(j, dict):
        tail = (proc.stderr or "").strip()[:200]
        return {"ok": False, "status": 503, "code": "vault_backend_error", "reason": "vault 输出无法解析" + (f"：{tail}" if tail else "")}
    if j.get("ok") is True:
        return {"ok": True, "json": j}
    code = str(j.get("code") or "denied")
    reason = str(j.get("reason") or "")
    if code == "unlock_required":
        return {"ok": False, "status": 423, "code": code, "reason": reason}
    if code == "rate_limited":
        return {"ok": False, "status": 429, "code": code, "reason": reason}
    return {"ok": False, "status": 403, "code": code, "reason": reason}


def create_server(cfg: Dict[str, Any]) -> ThreadingHTTPServer:
    index = IndexState(cfg)
    token_hash = hashlib.sha256(cfg["token"].encode("utf-8")).digest() if cfg["token"] else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静音默认 stderr 访问日志
            pass

        def _a_log(self, status: int) -> None:
            if cfg["log"]:
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {self.command} {self.path.split('?')[0]} {status}")

        def _send(self, status: int, obj: Any) -> int:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return status

        def _authed(self) -> bool:
            h = self.headers.get("authorization") or ""
            m = re.match(r"^Bearer\s+(.+)$", h)
            if not m or not token_hash:
                return False
            a = hashlib.sha256(m.group(1).strip().encode("utf-8")).digest()
            return hmac.compare_digest(a, token_hash)

        def _read_body(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
            length = int(self.headers.get("content-length") or "0")
            if length > BODY_LIMIT:
                # 读掉再拒，避免连接状态错乱
                self.rfile.read(min(length, BODY_LIMIT))
                return None, "body_too_large"
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw.decode("utf-8", "replace").strip():
                return {}, None
            try:
                return json.loads(raw.decode("utf-8")), None
            except ValueError:
                return None, "invalid_json"

        def do_GET(self):
            path = re.sub(r"/+$", "", self.path.split("?")[0]) or "/"
            if path == "/health":
                self._a_log(self._send(200, {
                    "ok": True, "service": SERVICE_NAME,
                    "index": index.root_info(),
                    "vault": {"configured": cfg["vault"]["configured"]},
                }))
                return
            if not self._authed():
                self._a_log(self._send(401, {"code": "unauthorized"}))
                return
            self._a_log(self._send(404, {"code": "not_found"}))

        def do_POST(self):
            path = re.sub(r"/+$", "", self.path.split("?")[0]) or "/"
            if not self._authed():
                self._a_log(self._send(401, {"code": "unauthorized"}))
                return
            if path == "/search":
                self._search()
            elif path == "/doc":
                self._doc()
            elif path == "/vault/status":
                r = _vault_run(cfg, ["status"])
                if not r["ok"]:
                    self._a_log(self._send(r["status"], {"code": r["code"], "reason": r["reason"]})); return
                self._a_log(self._send(200, {"ok": True, "window": r["json"].get("window") or None,
                                             "map_exists": bool(r["json"].get("map_exists"))}))
            elif path == "/vault/reveal":
                b, err = self._read_body()
                if err:
                    self._a_log(self._send(413 if err == "body_too_large" else 400, {"code": err})); return
                entry = str((b or {}).get("entry", ""))
                if not ENTRY_NAME_RX.match(entry):
                    self._a_log(self._send(400, {"code": "invalid_entry",
                                                 "reason": "entry 必须形如 [REDACTED:<label>:<hash8>]"})); return
                r = _vault_run(cfg, ["resolve", "--name", entry])
                if not r["ok"]:
                    self._a_log(self._send(r["status"], {"code": r["code"], "reason": r["reason"]})); return
                j = r["json"]
                self._a_log_reveal()
                self._send(200, {"name": j.get("name"), "label": j.get("label"), "sensitivity": j.get("sensitivity"),
                                 "masked": j.get("masked"), "value": j.get("value")})
            elif path == "/vault/unlock":
                b, err = self._read_body()
                if err:
                    self._a_log(self._send(413 if err == "body_too_large" else 400, {"code": err})); return
                if (b or {}).get("confirm") is not True:
                    self._a_log(self._send(400, {"code": "confirm_required",
                                                 "reason": "AFM 侧必须由用户点击确认后才发 unlock"})); return
                ttl = max(1, min(int((b or {}).get("ttl") or 600), 3600))
                r = _vault_run(cfg, ["unlock", "--yes", "--ttl", str(ttl)], want_json=False)
                if not r["ok"]:
                    self._a_log(self._send(r["status"], {"code": r["code"], "reason": r["reason"]})); return
                s = _vault_run(cfg, ["status"])
                self._a_log(self._send(200, {"ok": True, "window": s["json"].get("window") if s["ok"] else None,
                                             "map_exists": bool(s["json"].get("map_exists")) if s["ok"] else None}))
            elif path == "/vault/lock":
                r = _vault_run(cfg, ["lock"], want_json=False)
                if not r["ok"]:
                    self._a_log(self._send(r["status"], {"code": r["code"], "reason": r["reason"]})); return
                s = _vault_run(cfg, ["status"])
                self._a_log(self._send(200, {"ok": True, "window": s["json"].get("window") if s["ok"] else None,
                                             "map_exists": bool(s["json"].get("map_exists")) if s["ok"] else None}))
            else:
                self._a_log(self._send(404, {"code": "not_found"}))

        def _a_log_reveal(self):
            if cfg["log"]:
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {self.command} /vault/reveal 200")

        def _search(self):
            b, err = self._read_body()
            if err:
                self._a_log(self._send(413 if err == "body_too_large" else 400, {"code": err})); return
            b = b or {}
            q = str(b.get("query") or "").strip()
            if not q:
                self._a_log(self._send(200, {"hits": [], "mode": "bm25-title4", "total": 0,
                                             "root": index.root_info().get("root")})); return
            try:
                topk = int(b.get("topk") or DEFAULT_TOPK)
            except (TypeError, ValueError):
                topk = DEFAULT_TOPK
            topk = max(1, min(topk or DEFAULT_TOPK, MAX_TOPK))
            try:
                sw = int(b.get("snippetWidth") or 160)
            except (TypeError, ValueError):
                sw = 160
            sw = max(40, min(sw or 160, 500))
            dedup_file = (b.get("dedup") == "file")
            fetch = max(topk * DEDUP_FETCH_MULTIPLIER, topk) if dedup_file else topk
            r = index.search(q, fetch, sw)
            if r.get("error"):
                self._a_log(self._send(503, {"code": r["error"], "reason": r.get("reason")})); return
            hits = r["hits"]
            if dedup_file:
                seenf = set()
                dd = []
                for h in hits:
                    if h["path"] in seenf:
                        continue
                    seenf.add(h["path"])
                    dd.append(h)
                    if len(dd) >= topk:
                        break
                hits = dd
            seen: Dict[str, Dict[str, str]] = {}
            for h in hits:
                for ref in h["refs"]:
                    prev = seen.get(ref["key"])
                    if prev is None or len(ref["display"]) > len(prev["display"]):
                        seen[ref["key"]] = ref
            resp = {"hits": hits, "refs": list(seen.values()), "mode": "bm25-title4",
                    "total": len(hits), "index": r["index"], "root": r["index"].get("root") if r["index"] else None}
            if dedup_file:
                resp["dedup"] = "file"
            self._a_log(self._send(200, resp))

        def _doc(self):
            b, err = self._read_body()
            if err:
                self._a_log(self._send(413 if err == "body_too_large" else 400, {"code": err})); return
            rel = str((b or {}).get("path", "")).strip()
            if not rel or "\0" in rel:
                self._a_log(self._send(400, {"code": "invalid_path", "reason": "path 必须是索引根内的相对路径"})); return
            root = index.root_info().get("root")
            if not root:
                self._a_log(self._send(503, {"code": "index_root_not_configured"})); return
            root_abs = Path(root).resolve()
            cand = (root_abs / rel).resolve()
            try:
                inside = cand != root_abs and str(cand).startswith(str(root_abs) + os.sep)
            except OSError:
                inside = False
            if not inside:
                self._a_log(self._send(400, {"code": "invalid_path", "reason": "path 越出索引根"})); return
            try:
                st = cand.stat()
            except OSError:
                st = None
            if not st or not cand.is_file():
                self._a_log(self._send(404, {"code": "doc_not_found"})); return
            if st.st_size > DOC_MAX_BYTES:
                self._a_log(self._send(413, {"code": "doc_too_large", "reason": "文档超过 2MiB 上限"})); return
            try:
                content = cand.read_text(encoding="utf-8")
            except OSError as e:
                self._a_log(self._send(500, {"code": "doc_read_failed", "reason": str(e)})); return
            self._a_log(self._send(200, {"ok": True, "path": rel, "root": str(root_abs), "content": content}))

    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv
    if "--selftest" in argv:
        # 契约自检是开发期工具，见 tests/http_selftest.py（不在冻结产物内）。
        print("自检请运行：python tests/http_selftest.py", file=sys.stderr)
        return 2
    cfg = load_config()
    if not cfg["token"]:
        print("[http-server] 缺少 KM_SERVICE_TOKEN（AFM → 服务的鉴权 token）。fail-fast：未设置不启动。",
              file=sys.stderr)
        return 2
    httpd = create_server(cfg)
    host, port = httpd.server_address[0], httpd.server_address[1]
    loopback = host in ("127.0.0.1", "::1", "0.0.0.0")
    print(f"[http-server] {SERVICE_NAME} 监听 http://{host}:{port}")
    if host not in ("127.0.0.1", "::1"):
        print("[http-server] 警告：正在监听非回环地址，服务含本机取密代理，请确认网络暴露面。")
    print(f"[http-server] vault 人机通道：{'已配置' if cfg['vault']['configured'] else '未配置（/vault/* 返回 503）'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
