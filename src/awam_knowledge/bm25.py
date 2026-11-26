# SPDX-License-Identifier: MIT
"""共享 BM25 语料模块（stdlib 零依赖）。

逐字移植自 Node `code/bm25-index.js`（口径固定，勿改）：
纯文本切块（默认 20 行/块）+ 中文 bigram + BM25(k1=1.2, b=0.75) + 标题 token 加权。
只读语料，不写文件。移植等价性由 tests/parity 用同一 fixture 跑 Node↔Python 校验。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .fs_util import read_json, write_json_atomic

DEFAULT_CHUNK_LINES = 20
DEFAULT_TITLE_WEIGHT = 4
DEFAULT_EXCLUDES = "logseq\\bak,.recycle,.git,node_modules,.km,vault"

_RE_ASCII = re.compile(r"[a-z0-9_]{2,}")
_RE_NONCJK = re.compile(r"[^\u4e00-\u9fff]+")
_RE_MD = re.compile(r"\.(md|markdown)$", re.IGNORECASE)
_RE_SPLIT_NL = re.compile(r"\r?\n")
_RE_WS = re.compile(r"\s+")


def norm_path(p: str) -> str:
    # JS normPath：把 path.sep 与反斜杠都换成 '/'，正斜杠不变。
    return str(p).replace("\\", "/")


def stem_ascii(t: str) -> str:
    s = t
    if len(s) > 4 and s.endswith("ies"):
        s = s[:-3] + "y"
    elif len(s) > 4 and s.endswith("es"):
        s = s[:-2]
    elif len(s) > 3 and s.endswith("s") and not s.endswith("ss"):
        s = s[:-1]
    if len(s) > 5 and s.endswith("ing"):
        s = s[:-3]
    elif len(s) > 4 and s.endswith("ed"):
        s = s[:-2]
    return s


def tokenize(text: str, stem: bool = False) -> List[str]:
    t = str(text).lower()
    out: List[str] = []
    for m in _RE_ASCII.finditer(t):
        tok = m.group(0)
        out.append(stem_ascii(tok) if stem else tok)
    cjk = _RE_NONCJK.sub(" ", t)
    for seg in cjk.split(" "):
        if len(seg) == 1:
            out.append(seg)
            continue
        # JS: for(i=0; i+2<=seg.length; i++) → range(len-1)
        for i in range(len(seg) - 1):
            out.append(seg[i:i + 2])
    return out


def parse_excludes(value: Optional[Any]) -> List[str]:
    raw = DEFAULT_EXCLUDES if value is None else str(value)
    return [s.strip() for s in raw.split(",") if s.strip()]


def is_excluded(rel_path: str, excludes: List[str]) -> bool:
    # 大小写不敏感：Windows/macOS 文件系统大小写不敏感，且须与 build 侧
    # km_build_index.iter_note_files 的 p.lower() 口径一致——否则同一知识目录里
    # `.GIT`/`Node_Modules`/`.Recycle` 等大写黑名单目录会在对外 /search 泄露，
    # 却在 build_index 被隐藏（两侧结果不一致）。Node 参照实现同步小写以保持 parity。
    norm = norm_path(rel_path).lower()
    parts = norm.split("/")
    for e in excludes:
        n = norm_path(e).lower()
        if norm == n or ("/" + n + "/") in norm or norm.startswith(n + "/") or n in parts:
            return True
    return False


def _is_link_like(p: Path) -> bool:
    # Node（libuv）在 Windows 上把目录符号链接与 NTFS junction/mount-point 一类的
    # 重解析点（reparse point）都归类为 symlink，fs.readdirSync 返回的 Dirent
    # isSymbolicLink()=True、isDirectory()=False，walk 因此完全不跟随它们。
    # Python 的 Path.is_symlink() 对 junction 返回 False、is_dir() 返回 True，会跟随
    # 自环 junction 直至撞 MAX_PATH 产生大量重复伪文档。为与 Node 保持 parity 并消除
    # 环路，这里把「符号链接 或 任何 reparse point」统一视为链接、在 walk 中跳过。
    try:
        if p.is_symlink():
            return True
    except OSError:
        return False
    try:
        attr = getattr(os.lstat(str(p)), "st_file_attributes", 0)
    except OSError:
        return False
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    return bool(attr & FILE_ATTRIBUTE_REPARSE_POINT)


def _load_sidecar_index(root: Path) -> Optional[Dict[str, Any]]:
    ip = root / "index.json"
    if not ip.exists():
        return None
    try:
        j = json.loads(ip.read_text(encoding="utf-8"))
        by_path: Dict[str, Any] = {}
        for d in (j.get("docs") or []):
            if d and d.get("path"):
                by_path[norm_path(d["path"])] = d
        return {
            "byPath": by_path,
            "count": j.get("count") or len(by_path),
            "generated_at": j.get("generated_at"),
            "generated_from": j.get("generated_from"),
        }
    except Exception as e:  # noqa: BLE001 - parity: JS catches and returns error object
        return {"byPath": {}, "error": str(e)}


def _make_doc(rel_path: str, title: str, lines: List[str], start: int,
              chunk_lines: int, title_weight: int, stem: bool) -> Optional[Dict[str, Any]]:
    body = "\n".join(lines[start:start + chunk_lines])
    if not body.strip():
        return None
    tokens = tokenize(body, stem)
    title_toks = tokenize(title, stem)
    for _ in range(title_weight):
        tokens.extend(title_toks)
    tf: Dict[str, int] = {}
    for tk in tokens:
        tf[tk] = tf.get(tk, 0) + 1
    return {
        "path": norm_path(rel_path), "title": title,
        "lineStart": start + 1, "lineEnd": min(start + chunk_lines, len(lines)),
        "len": len(tokens), "tf": tf, "text": body,
    }


def build_corpus(opts: Dict[str, Any]) -> Dict[str, Any]:
    root = Path(opts["root"])
    ex = opts.get("excludes")
    excludes = ex if isinstance(ex, list) else parse_excludes(ex)
    chunk_lines = opts.get("chunkLines") or DEFAULT_CHUNK_LINES
    title_weight = DEFAULT_TITLE_WEIGHT if opts.get("titleWeight") is None else int(opts["titleWeight"])
    stem = bool(opts.get("stem"))

    docs: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    scanned: List[str] = []
    skipped: List[str] = []
    zero_byte: List[str] = []
    encoding_warn: List[Dict[str, Any]] = []
    total_bytes = 0
    total_lines = 0
    sidecar = _load_sidecar_index(root)

    def walk(dir_path: Path) -> None:
        nonlocal total_bytes, total_lines
        try:
            entries = list(dir_path.iterdir())
        except OSError as e:
            failed.append({"path": norm_path(str(dir_path.relative_to(root))), "stage": "readdir",
                           "error": e.__class__.__name__})
            return
        # JS readdirSync 返回顺序按目录；为确定性按名排序后再遍历（与 Node 在多数 FS 上一致）。
        for e in sorted(entries, key=lambda p: p.name):
            p = dir_path / e.name
            rel = str(p.relative_to(root))
            if e.is_dir():
                # link-safe：符号链接与 NTFS junction/reparse point 一律不跟随（环路防护）。
                # Node 侧 fs.readdirSync 的 Dirent 已把这些重解析点判为 symlink、不递归；
                # Python is_symlink() 不识别 junction，故显式用 _is_link_like 对齐两侧行为。
                if _is_link_like(e):
                    continue
                if not is_excluded(rel, excludes):
                    walk(p)
                else:
                    skipped.append(norm_path(rel))
                continue
            if not e.is_file() or not _RE_MD.search(e.name):
                continue
            scanned.append(rel)
            try:
                data = p.read_bytes()
                raw = data.decode("utf-8", errors="replace")
            except OSError as err:
                failed.append({"path": norm_path(rel), "stage": "read", "error": err.__class__.__name__})
                continue
            if len(data) == 0:
                zero_byte.append(norm_path(rel))
            repl = raw.count("\ufffd")
            if len(raw) and repl / len(raw) > 0.01:
                encoding_warn.append({"path": norm_path(rel), "replacement_chars": repl, "bytes": len(data)})
            total_bytes += len(data)
            lines = _RE_SPLIT_NL.split(raw)
            total_lines += len(lines)
            file_title = _RE_MD.sub("", e.name)
            meta = sidecar["byPath"].get(norm_path(rel)) if sidecar and sidecar.get("byPath") else None
            title = str(meta["title"]) if (meta and meta.get("title")) else file_title
            s = 0
            while s < len(lines):
                d = _make_doc(rel, title, lines, s, chunk_lines, title_weight, stem)
                if d:
                    if meta and meta.get("type"):
                        d["docType"] = meta["type"]
                    docs.append(d)
                s += chunk_lines

    walk(root)
    return {"docs": docs, "failed": failed, "scanned": scanned, "skipped": skipped,
            "zeroByte": zero_byte, "encodingWarn": encoding_warn, "totalBytes": total_bytes,
            "totalLines": total_lines, "sidecar": sidecar}


def build_index(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    df: Dict[str, int] = {}
    for d in docs:
        for tk in d["tf"]:
            df[tk] = df.get(tk, 0) + 1
    n_docs = len(docs)
    avgdl = sum(d["len"] for d in docs) / (n_docs or 1)
    return {"df": df, "N": n_docs, "avgdl": avgdl}


def query_weights(query: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    w: Dict[str, int] = {}
    stem = bool(opts and opts.get("stem"))
    for t in tokenize(query, stem):
        w[t] = 1
    return w


def rank_all(docs: List[Dict[str, Any]], idx: Dict[str, Any], weights: Dict[str, int]) -> List[Dict[str, Any]]:
    k1, b = 1.2, 0.75
    df, n_docs, avgdl = idx["df"], idx["N"], idx["avgdl"]
    out: List[Dict[str, Any]] = []
    for di, d in enumerate(docs):
        score = 0.0
        tf = d["tf"]
        for tk, wq in weights.items():
            f = tf.get(tk)
            if not f:
                continue
            n = df.get(tk, 1)
            idf = math_log(1 + (n_docs - n + 0.5) / (n + 0.5))
            score += wq * idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * d["len"] / avgdl))
        if score > 0:
            out.append({"di": di, "path": d["path"], "title": d["title"],
                        "lineStart": d["lineStart"], "lineEnd": d["lineEnd"], "score": score})
    # JS out.sort((a,b2)=>b2.score-a.score) —— 稳定、按分降序；score 相等保持 di 升序。
    out.sort(key=lambda r: -r["score"])
    return out


def math_log(x: float) -> float:
    import math
    return math.log(x)


def snippet_of(text: str, terms: List[str], width: Optional[int]) -> str:
    limit = width or 160
    flat = _RE_WS.sub(" ", str(text)).strip()
    at = -1
    for t in terms:
        i = flat.lower().find(t)
        if i != -1 and (at == -1 or i < at):
            at = i
    if at == -1:
        return flat[:limit]
    from_ = max(0, at - limit // 3)
    tail = "…" if from_ + limit < len(flat) else ""
    head = "…" if from_ > 0 else ""
    return head + flat[from_:from_ + limit] + tail


def search(corpus: Dict[str, Any], idx: Dict[str, Any], query: str,
           opts: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    opts = opts or {}
    topk = int(opts.get("topk") or 5)
    weights = opts.get("weights") or query_weights(query, {"stem": opts.get("stem")})
    terms = list(weights.keys())
    results = []
    for r in rank_all(corpus["docs"], idx, weights)[:topk]:
        d = corpus["docs"][r["di"]]
        results.append({
            "path": r["path"],
            "line": r["lineStart"],
            "lines": f"{r['lineStart']}-{r['lineEnd']}",
            "title": r["title"],
            "rawScore": r["score"],
            "snippet": snippet_of(d["text"], terms, opts.get("snippetWidth")),
        })
    return results


# ---------------- 带缓存的语料构建（逐字移植 Node buildCorpusCached） ----------------

CACHE_VERSION = 2


def _same_settings(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    if not a or not b:
        return False
    return (a.get("chunkLines") == b.get("chunkLines") and a.get("titleWeight") == b.get("titleWeight")
            and bool(a.get("stem")) == bool(b.get("stem")) and str(a.get("excludes")) == str(b.get("excludes")))


def _encode_docs(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for d in docs:
        e: Dict[str, Any] = {"lineStart": d["lineStart"], "lineEnd": d["lineEnd"], "len": d["len"],
                             "tf": [[k, v] for k, v in d["tf"].items()], "text": d["text"]}
        if d.get("docType"):
            e["docType"] = d["docType"]
        out.append(e)
    return out


def _decode_docs(rel: str, title: str, arr: Any) -> List[Dict[str, Any]]:
    out = []
    for d in arr or []:
        e: Dict[str, Any] = {"path": norm_path(rel), "title": title,
                             "lineStart": d["lineStart"], "lineEnd": d["lineEnd"], "len": d["len"],
                             "tf": {k: v for k, v in d["tf"]}, "text": d["text"]}
        if d.get("docType"):
            e["docType"] = d["docType"]
        out.append(e)
    return out


def build_corpus_cached(opts: Dict[str, Any]) -> Dict[str, Any]:
    """按 (mtime, size, title) 判定文件是否变化，未变则复用 chunk；缓存键含切块/加权参数，
    参数变了整体重建；删除的文件会从缓存中剔除。与 Node buildCorpusCached 行为一致。"""
    root = Path(opts["root"])
    ex = opts.get("excludes")
    excludes = ex if isinstance(ex, list) else parse_excludes(ex)
    chunk_lines = int(opts.get("chunkLines") or DEFAULT_CHUNK_LINES)
    title_weight = DEFAULT_TITLE_WEIGHT if opts.get("titleWeight") is None else int(opts["titleWeight"])
    stem = bool(opts.get("stem"))
    settings = {"chunkLines": chunk_lines, "titleWeight": title_weight, "stem": stem,
                "excludes": ",".join(excludes)}
    cache_path = opts.get("cachePath")
    prev = None
    if cache_path and not opts.get("force"):
        prev = read_json(cache_path, None)
    compatible = bool(prev and prev.get("version") == CACHE_VERSION and _same_settings(prev.get("settings"), settings))
    prev_files: Dict[str, Any] = prev.get("files") if (compatible and prev) else {}

    sidecar = _load_sidecar_index(root)
    docs: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    scanned: List[str] = []
    skipped: List[str] = []
    zero_byte: List[str] = []
    encoding_warn: List[Dict[str, Any]] = []
    next_files: Dict[str, Any] = {}
    total_bytes = 0
    total_lines = 0
    hits = 0
    misses = 0

    files: List[Tuple[Path, str, str]] = []

    def walk(dir_path: Path) -> None:
        try:
            entries = list(dir_path.iterdir())
        except OSError as e:
            failed.append({"path": norm_path(str(dir_path.relative_to(root))), "stage": "readdir",
                           "error": e.__class__.__name__})
            return
        for e in sorted(entries, key=lambda p: p.name):
            p = dir_path / e.name
            rel = str(p.relative_to(root))
            if e.is_dir():
                # link-safe：符号链接 / junction / reparse point 不跟随（环路防护），与 build_corpus / Node 一致。
                if _is_link_like(e):
                    continue
                if not is_excluded(rel, excludes):
                    walk(p)
                else:
                    skipped.append(norm_path(rel))
                continue
            if e.is_file() and _RE_MD.search(e.name):
                files.append((p, rel, e.name))

    walk(root)

    for p, rel, name in files:
        rel_n = norm_path(rel)
        scanned.append(rel)
        try:
            st = p.stat()
        except OSError as e:
            failed.append({"path": rel_n, "stage": "stat", "error": e.__class__.__name__})
            continue
        meta = sidecar["byPath"].get(rel_n) if sidecar and sidecar.get("byPath") else None
        title = str(meta["title"]) if (meta and meta.get("title")) else _RE_MD.sub("", name)
        mtime_ms = int(st.st_mtime * 1000)
        cached = prev_files.get(rel_n)
        if (cached and cached.get("size") == st.st_size and cached.get("mtime_ms") == mtime_ms
                and cached.get("title") == title):
            hits += 1
            docs.extend(_decode_docs(rel_n, title, cached.get("docs") or []))
            total_bytes += cached.get("bytes") or 0
            total_lines += cached.get("lines") or 0
            if cached.get("zero"):
                zero_byte.append(rel_n)
            if cached.get("repl") and cached["repl"] / max(1, cached.get("chars") or 1) > 0.01:
                encoding_warn.append({"path": rel_n, "replacement_chars": cached["repl"], "bytes": cached["size"]})
            next_files[rel_n] = cached
            continue

        misses += 1
        try:
            data = p.read_bytes()
            raw = data.decode("utf-8", errors="replace")
        except OSError as err:
            failed.append({"path": rel_n, "stage": "read", "error": err.__class__.__name__})
            continue
        repl = raw.count("\ufffd")
        lines = _RE_SPLIT_NL.split(raw)
        file_docs: List[Dict[str, Any]] = []
        s = 0
        while s < len(lines):
            d = _make_doc(rel, title, lines, s, chunk_lines, title_weight, stem)
            if d:
                if meta and meta.get("type"):
                    d["docType"] = meta["type"]
                file_docs.append(d)
            s += chunk_lines
        docs.extend(file_docs)
        total_bytes += len(data)
        total_lines += len(lines)
        if len(data) == 0:
            zero_byte.append(rel_n)
        if len(raw) and repl / len(raw) > 0.01:
            encoding_warn.append({"path": rel_n, "replacement_chars": repl, "bytes": len(data)})
        next_files[rel_n] = {
            "mtime_ms": mtime_ms, "size": st.st_size, "title": title, "bytes": len(data),
            "lines": len(lines), "zero": len(data) == 0, "repl": repl, "chars": len(raw),
            "docs": _encode_docs(file_docs),
        }

    removed = sum(1 for rel in prev_files if rel not in next_files)
    saved = False
    if cache_path and (misses > 0 or removed > 0 or not compatible):
        write_json_atomic(cache_path, {
            "version": CACHE_VERSION, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "settings": settings, "files": next_files,
        })
        saved = True

    return {"docs": docs, "failed": failed, "scanned": scanned, "skipped": skipped,
            "zeroByte": zero_byte, "encodingWarn": encoding_warn, "totalBytes": total_bytes,
            "totalLines": total_lines, "sidecar": sidecar,
            "cache": {"enabled": bool(cache_path), "hits": hits, "misses": misses,
                      "removed": removed, "saved": saved, "compatible": compatible}}
