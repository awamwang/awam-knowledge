# SPDX-License-Identifier: MIT
"""通用能力：带附件的 Markdown 目录导入（零依赖，只读源目录）。

逐字移植自 Node code/import-md-dir.js（manifest v2 口径勿改）：

    python -m awam_knowledge.import_md_dir --root "<dir>" [--manifest "<out.json>"]
        [--report "<out.md>"] [--skip ".git,node_modules,.recycle"] [--max-list 50]
        [--resume "<prev-manifest.json>"] [--no-scan-secrets]
    python -m awam_knowledge.import_md_dir --selftest [--selftest-root "<dir>"]

能力：Markdown 摄取（条目/标题/字节/行数/SHA256）、附件识别 + SHA256 + 零字节/同内容去重、
深度感知链接扫描器（成对圆括号 / <...> / 引号包裹 / 内嵌双链）、密钥模式扫描（只报规则与次数）、
编码异常检测、断点/幂等（--resume 按 (size, mtime) 复用条目）、平台归一（NFC + win32 大小写）
与 Logseq 语义（双链目标存在性、块引用 ((uuid)) 按 id:: 对账）。

边界：源目录只读；只写 --manifest / --report 指定文件；manifest 不含正文。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import unquote

DFLT_SKIP = [".git", "node_modules", ".recycle"]

_RE_IMG = re.compile(r"<img[^>]*src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_RE_REFDEF = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
_RE_WIKILINK = re.compile(r"(!)?\[\[([^\]|]+)(\|[^\]]*)?\]\]")
_RE_BLOCK_REF = re.compile(r"\(\(([0-9a-fA-F][0-9a-fA-F-]{7,})\)\)")
_RE_ID_DEF = re.compile(r"^\s*id::\s*([0-9a-fA-F][0-9a-fA-F-]{7,})\s*$", re.MULTILINE)
_RE_MD = re.compile(r"\.(md|markdown)$", re.IGNORECASE)
_RE_SPLIT_NL = re.compile(r"\r?\n")

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
             ".tif", ".tiff", ".avif"}
AV_EXT = {".mp4", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".flac", ".ogg"}
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".txt"}

SECRET_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

IS_WIN = sys.platform == "win32"
WIN_LONG_PATH = 240


def parse_skip(value: Optional[str]) -> Set[str]:
    raw = DFLT_SKIP if value is None or value == "" else str(value)
    return {s.strip() for s in raw.split(",") if s.strip()}


def attach_kind(ext: str) -> str:
    if ext in IMAGE_EXT:
        return "image"
    if ext in AV_EXT:
        return "av"
    if ext in DOC_EXT:
        return "doc"
    return "other"


def is_external(t: str) -> bool:
    return re.match(r"^(https?:|mailto:|data:|tel:|ftp:)", t, re.IGNORECASE) is not None


def is_absolute(t: str) -> bool:
    return re.match(r"^[a-zA-Z]:[\\/]", t) is not None or t.startswith("\\\\") or t.startswith("/")


def norm(p: str) -> str:
    return str(p).replace("\\", "/")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean(t: str) -> str:
    s = str(t).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()
    return s


def is_wiki(t: str) -> bool:
    return re.fullmatch(r"\[\[[^\]]+\]\]", t.strip()) is not None


def nfc(s: Any) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", str(s))


def match_key(s: Any) -> str:
    v = nfc(s)
    return v.lower() if IS_WIN else v


def empty_counters() -> Dict[str, Any]:
    return {
        "links_total": 0, "resolved": 0, "missing": 0, "external": 0, "absolute": 0, "anchor": 0,
        "escapes_root": 0, "to_markdown": 0, "is_directory": 0, "wiki_in_link": 0,
        "wiki_in_link_page_exists": 0, "fallback_unique": 0, "fallback_ambiguous": 0,
        "fallback_none": 0, "block_refs_total": 0, "block_refs_resolved": 0,
        "block_refs_unresolved": 0, "wiki_pages_total": 0, "wiki_pages_resolved": 0,
    }


def merge_counters(into: Dict[str, Any], from_: Optional[Dict[str, Any]]) -> None:
    if not from_:
        return
    for k, v in from_.items():
        into[k] = into.get(k, 0) + int(v or 0)


def scan_secrets(raw: str) -> List[Dict[str, Any]]:
    hits = []
    for rid, pat in SECRET_PATTERNS:
        m = pat.findall(raw)
        if m:
            hits.append({"rule": rid, "count": len(m)})
    return hits


def collect(root: Path, skip: Set[str]) -> Dict[str, Any]:
    md_files: List[Dict[str, Any]] = []
    attach_all: List[Dict[str, Any]] = []
    walk_errors: List[Dict[str, Any]] = []
    long_paths = 0

    def walk(dir_path: Path) -> None:
        nonlocal long_paths
        try:
            entries = list(os.scandir(dir_path))
        except OSError as e:
            walk_errors.append({"path": norm(os.path.relpath(dir_path, root)),
                                "error": e.__class__.__name__})
            return
        for e in sorted(entries, key=lambda x: x.name):
            p = Path(e.path)
            if IS_WIN and len(str(p)) > WIN_LONG_PATH:
                long_paths += 1
            if e.is_dir(follow_symlinks=False):
                if e.name not in skip:
                    walk(p)
            elif e.is_file(follow_symlinks=False):
                rel = nfc(os.path.relpath(p, root))
                if _RE_MD.search(e.name):
                    md_files.append({"rel": rel, "abs": p, "name": e.name})
                else:
                    ext = os.path.splitext(e.name)[1].lower()
                    attach_all.append({"rel": rel, "abs": p, "ext": ext, "bytes": p.stat().st_size})

    walk(root)
    return {"mdFiles": md_files, "attachAll": attach_all, "walkErrors": walk_errors,
            "longPaths": long_paths}


def extract_links(txt: str) -> List[Dict[str, str]]:
    """深度感知的 Markdown 行内链接扫描器（成对圆括号、<...> 目标、转义、引号包裹）。"""
    out: List[Dict[str, str]] = []
    i = txt.find("](", 0)
    while i != -1:
        j = i + 2
        while j < len(txt) and txt[j] in (" ", "\t"):
            j += 1
        target: Optional[str] = None
        end = -1
        if j < len(txt) and txt[j] == "<":
            gt = txt.find(">", j + 1)
            if gt != -1:
                k = gt + 1
                while k < len(txt) and txt[k] in (" ", "\t"):
                    k += 1
                if k < len(txt) and txt[k] == ")":
                    target = txt[j + 1:gt]
                    end = k
        else:
            depth = 0
            k = j
            while k < len(txt) and k - j < 600:
                ch = txt[k]
                if ch == "\\":
                    k += 2
                    continue
                if ch in ("\n", "\r"):
                    break
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        break
                    depth -= 1
                k += 1
            if k < len(txt) and txt[k] == ")" and k > j:
                target = txt[j:k]
                end = k
        if target is not None:
            out.append({"target": clean(target), "form": "md-link"})
            i = txt.find("](", end + 1)
        else:
            i = txt.find("](", i + 2)
    return out


def add_security(bucket: Dict[str, Any], file: str, rule: str, count: int) -> None:
    bucket["rules"][rule] = bucket["rules"].get(rule, 0) + count
    found = next((f for f in bucket["files"] if f["file"] == file), None)
    if found:
        found["count"] += count
        if rule not in found["rules"]:
            found["rules"].append(rule)
    else:
        bucket["files"].append({"file": file, "count": count, "rules": [rule]})


def import_steps(root: Path, skip: Set[str], opts: Optional[Dict[str, Any]] = None) -> Iterator[Dict[str, Any]]:
    """逐文件推进的导入流程；进度经 yield 产出，完整结果经 StopIteration.value 返回。"""
    opts = opts or {}
    scan_secrets_enabled = opts.get("scanSecrets") is not False
    resume = opts.get("resume") if isinstance(opts.get("resume"), dict) else None
    checkpoint: Optional[Callable[[Dict[str, Any]], None]] = opts.get("checkpoint") or None

    def do_checkpoint(payload: Dict[str, Any]) -> None:
        if checkpoint is None:
            return
        try:
            checkpoint(payload)
        except Exception:
            pass  # 检查点写失败不影响导入本身

    prev_by_path: Dict[str, Dict[str, Any]] = {}
    if resume and isinstance(resume.get("markdown"), list):
        for e in resume["markdown"]:
            if e and e.get("path"):
                prev_by_path[match_key(e["path"])] = e

    c = collect(root, skip)
    md_files, attach_all, walk_errors = c["mdFiles"], c["attachAll"], c["walkErrors"]
    long_paths = c["longPaths"]
    yield {"phase": "analyzing", "done": 0, "total": len(md_files), "path": ""}

    by_base: Dict[str, List[str]] = {}
    for f in [*md_files, *attach_all]:
        b = match_key(os.path.basename(f["rel"]))
        by_base.setdefault(b, []).append(norm(f["rel"]))
    # pagesIdx：去 pages/ 前缀与扩展名（双链目标按页面名对账）
    pages_idx = set()
    for f in md_files:
        base = re.sub(r"\.(md|markdown)$", "", re.sub(r"^pages/", "", norm(f["rel"])), flags=re.IGNORECASE)
        pages_idx.add(match_key(base))

    md_entries: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    attach_refs: Dict[str, Dict[str, Any]] = {}
    counters = empty_counters()
    security: Dict[str, Any] = {"scan": scan_secrets_enabled, "files_with_hits": 0, "hits_total": 0,
                                "rules": {}, "files": []}
    wikilinks = refdefs = html_img = 0
    total_bytes = 0
    unchanged = 0
    block_ref_pending: List[Dict[str, Any]] = []
    block_ids: Set[str] = set()

    def add_attach_ref(ref: str, from_rel: str) -> None:
        k = match_key(ref)
        if k not in attach_refs:
            attach_refs[k] = {"path": ref, "by": set()}
        attach_refs[k]["by"].add(from_rel)

    should_cancel: Optional[Callable[[], bool]] = opts.get("shouldCancel") or None

    for i, f in enumerate(md_files):
        if should_cancel and should_cancel():
            return {"cancelled": True, "processed": i, "phase": "analyzing"}
        rel_n = norm(f["rel"])

        try:
            st = os.stat(f["abs"])
        except OSError as e:
            walk_errors.append({"path": rel_n, "error": "stat:" + e.__class__.__name__})
            continue

        prev = prev_by_path.get(match_key(rel_n))
        if (prev and prev.get("size") == st.st_size
                and prev.get("mtime_ms") == int(st.st_mtime * 1000) and prev.get("counters")):
            unchanged += 1
            entry = dict(prev)
            entry["unchanged"] = True
            md_entries.append(entry)
            merge_counters(counters, prev.get("counters"))
            for u in prev.get("unresolved") or []:
                unresolved.append(u)
            for r in prev.get("refs") or []:
                add_attach_ref(r, rel_n)
            for idv in prev.get("block_ids") or []:
                block_ids.add(idv)
            if scan_secrets_enabled:
                for h in prev.get("security") or []:
                    add_security(security, rel_n, h["rule"], h["count"])
            do_checkpoint({"phase": "analyzing", "done": i + 1, "total": len(md_files),
                           "markdown": md_entries})
            yield {"phase": "analyzing", "done": i + 1, "total": len(md_files), "path": rel_n}
            continue

        try:
            txt = Path(f["abs"]).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            walk_errors.append({"path": rel_n, "error": "read:" + e.__class__.__name__})
            continue

        data_bytes = len(txt.encode("utf-8"))
        total_bytes += data_bytes
        dir_abs = os.path.dirname(os.fspath(f["abs"]))
        targets = extract_links(txt)
        file_counters = empty_counters()
        file_unresolved: List[Dict[str, Any]] = []
        file_refs: List[str] = []
        for m in _RE_IMG.finditer(txt):
            targets.append({"target": clean(m.group(1)), "form": "html-img"})
            html_img += 1
        for m in _RE_REFDEF.finditer(txt):
            targets.append({"target": clean(m.group(1)), "form": "ref-def"})
            refdefs += 1
        for m in _RE_WIKILINK.finditer(txt):
            wikilinks += 1
            target = match_key(m.group(2).strip())
            file_counters["wiki_pages_total"] += 1
            if target in pages_idx or target.replace("/", ".") in pages_idx:
                file_counters["wiki_pages_resolved"] += 1
        file_block_refs = [m.group(1).lower() for m in _RE_BLOCK_REF.finditer(txt)]
        file_ids: List[str] = []
        for m in _RE_ID_DEF.finditer(txt):
            idv = m.group(1).lower()
            if idv not in file_ids:
                file_ids.append(idv)
            block_ids.add(idv)

        file_counters["block_refs_total"] = len(file_block_refs)
        for t in targets:
            file_counters["links_total"] += 1
            raw = t["target"]
            if is_wiki(raw):
                file_counters["wiki_in_link"] += 1
                name = raw[2:-2].split("|")[0].strip()
                if match_key(name) in pages_idx or match_key(name.replace("/", ".")) in pages_idx:
                    file_counters["wiki_in_link_page_exists"] += 1
                continue
            if is_external(raw):
                file_counters["external"] += 1
                continue
            if raw.startswith("#"):
                file_counters["anchor"] += 1
                continue
            if is_absolute(raw):
                file_counters["absolute"] += 1
                file_unresolved.append({"file": rel_n, "target": raw, "cause": "absolute_path",
                                        "form": t["form"]})
                continue
            dec = raw.split("#")[0].split("?")[0]
            try:
                dec = unquote(dec)
            except Exception:
                pass
            abs_p = os.path.abspath(os.path.join(dir_abs, dec))
            inside = nfc(norm(os.path.relpath(abs_p, root)))
            escapes_root = inside.startswith("..") or os.path.isabs(inside)
            if not os.path.exists(abs_p):
                file_counters["missing"] += 1
                cand = [x for x in (by_base.get(match_key(os.path.basename(dec))) or [])
                        if match_key(x) != match_key(inside)]
                if len(cand) == 1:
                    file_counters["fallback_unique"] += 1
                elif len(cand) > 1:
                    file_counters["fallback_ambiguous"] += 1
                else:
                    file_counters["fallback_none"] += 1
                file_unresolved.append({
                    "file": rel_n, "target": raw, "cause": "missing", "form": t["form"],
                    "would_resolve_to": inside, "basename_candidates": len(cand),
                    "basename_sample": cand[:3],
                })
                continue
            if escapes_root:
                file_counters["escapes_root"] += 1
                file_unresolved.append({"file": rel_n, "target": raw, "cause": "escapes_import_root",
                                        "form": t["form"], "would_resolve_to": inside})
                continue
            st2 = os.stat(abs_p)
            if os.path.isdir(abs_p):
                file_counters["is_directory"] += 1
                file_unresolved.append({"file": rel_n, "target": raw, "cause": "is_directory",
                                        "form": t["form"]})
                continue
            if _RE_MD.search(abs_p):
                file_counters["to_markdown"] += 1
                file_counters["resolved"] += 1
                continue
            file_counters["resolved"] += 1
            ref = nfc(norm(os.path.relpath(abs_p, root)))
            if ref not in file_refs:
                file_refs.append(ref)
            add_attach_ref(ref, rel_n)

        file_security = scan_secrets(txt) if scan_secrets_enabled else []
        for h in file_security:
            add_security(security, rel_n, h["rule"], h["count"])

        merge_counters(counters, file_counters)
        unresolved.extend(file_unresolved)

        md_entries.append({
            "path": rel_n,
            "title": re.sub(r"\.(md|markdown)$", "", f["rel"], flags=re.IGNORECASE),
            "bytes": data_bytes,
            "lines": len(_RE_SPLIT_NL.split(txt)),
            "sha256": hashlib.sha256(txt.encode("utf-8")).hexdigest(),
            "size": st.st_size, "mtime_ms": int(st.st_mtime * 1000),
            "unchanged": False,
            "counters": file_counters, "refs": file_refs, "unresolved": file_unresolved,
            "security": file_security,
            "block_ids": file_ids,
        })
        if file_block_refs:
            block_ref_pending.append({"relN": rel_n, "refs": file_block_refs,
                                      "counters": file_counters, "entry": md_entries[-1]})
        do_checkpoint({"phase": "analyzing", "done": i + 1, "total": len(md_files),
                       "markdown": md_entries})
        yield {"phase": "analyzing", "done": i + 1, "total": len(md_files), "path": rel_n}

    # 块引用要等全部文件的 id:: 读完才能判定是否可解析
    block_ref_unresolved: List[Dict[str, Any]] = []
    for p in block_ref_pending:
        ok = 0
        for u in p["refs"]:
            if u in block_ids:
                ok += 1
            else:
                block_ref_unresolved.append({"file": p["relN"], "target": "((" + u + "))",
                                             "cause": "block_ref_missing", "form": "block-ref"})
        p["counters"]["block_refs_resolved"] = ok
        p["counters"]["block_refs_unresolved"] = len(p["refs"]) - ok
        counters["block_refs_resolved"] += ok
        counters["block_refs_unresolved"] += len(p["refs"]) - ok
    unresolved.extend(block_ref_unresolved)

    yield {"phase": "attachments", "done": 0, "total": len(attach_all), "path": ""}
    attach_registry: List[Dict[str, Any]] = []
    zero_byte_attachments = 0
    for i, a in enumerate(attach_all):
        if should_cancel and should_cancel():
            return {"cancelled": True, "processed": i, "phase": "attachments"}
        rel_n = norm(a["rel"])
        hash_v: Optional[str] = None
        try:
            hash_v = sha256_file(a["abs"])
        except OSError as e:
            walk_errors.append({"path": rel_n, "error": "read:" + e.__class__.__name__})
        if a["bytes"] == 0:
            zero_byte_attachments += 1
        refs = attach_refs.get(match_key(rel_n))
        attach_registry.append({
            "path": rel_n, "kind": attach_kind(a["ext"]), "ext": a["ext"], "bytes": a["bytes"],
            "sha256": hash_v, "zero_byte": a["bytes"] == 0,
            "referenced_by": sorted(refs["by"]) if refs else [],
            "referenced": match_key(rel_n) in attach_refs,
        })
        if (i + 1) % 25 == 0:
            yield {"phase": "attachments", "done": i + 1, "total": len(attach_all), "path": rel_n}
    attach_registry.sort(key=lambda x: x["path"])

    by_hash: Dict[str, List[str]] = {}
    for a in attach_registry:
        if not a["sha256"]:
            continue
        by_hash.setdefault(a["sha256"], []).append(a["path"])
    duplicate_groups = [{"sha256_group": len(g), "paths": g}
                        for g in by_hash.values() if len(g) > 1]

    orphan = [a["path"] for a in attach_registry if not a["referenced"]]
    attach_all_keys = {match_key(a["rel"]) for a in attach_all}
    missing_in_tree = [e["path"] for e in attach_refs.values()
                       if match_key(e["path"]) not in attach_all_keys]
    by_kind: Dict[str, int] = {}
    for a in attach_registry:
        by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1
    denom = counters["resolved"] + counters["missing"]

    security["hits_total"] = sum(f["count"] for f in security["files"])
    security["files_with_hits"] = len(security["files"])

    summary = {
        "root": norm(str(root)), "skip": sorted(skip),
        "markdown": {"files": len(md_entries), "bytes": total_bytes, "unchanged": unchanged},
        "attachments": {
            "files": len(attach_registry),
            "bytes": sum(a["bytes"] for a in attach_registry),
            "by_kind": by_kind,
            "referenced": len(attach_registry) - len(orphan),
            "never_referenced": len(orphan),
            "zero_byte": zero_byte_attachments,
            "duplicate_groups": len(duplicate_groups),
        },
        "links": {
            **counters, "html_img_links": html_img, "wikilinks_info": wikilinks,
            "refdefs_info": refdefs,
            "block_refs": {
                "total": counters["block_refs_total"],
                "resolved": counters["block_refs_resolved"],
                "unresolved": counters["block_refs_unresolved"],
                "id_defs": len(block_ids),
            },
            "resolve_rate_on_relative": (round(counters["resolved"] / denom * 100, 2)
                                         if denom else None),
            "resolve_rate_denominator": "resolved + missing（相对路径文件引用）",
        },
        "security": {
            "scan": scan_secrets_enabled, "files_with_hits": security["files_with_hits"],
            "hits_total": security["hits_total"], "rules": security["rules"],
        },
        "integrity": {
            "walk_errors": len(walk_errors),
            "long_paths": long_paths if IS_WIN else None,
            "orphan_attachments": len(orphan),
            "referenced_but_absent": len(missing_in_tree),
            "zero_byte_attachments": zero_byte_attachments,
            "duplicate_attachment_groups": len(duplicate_groups),
        },
        "resume": {"used": bool(resume), "unchanged_files": unchanged},
        "unresolved_total": len(unresolved),
    }

    return {"summary": summary, "markdown": md_entries, "attachments": attach_registry,
            "unresolved": unresolved, "walkErrors": walk_errors, "orphan": orphan,
            "duplicateGroups": duplicate_groups, "security": security}


def import_dir(root: Path, skip: Set[str], opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    it = import_steps(root, skip, opts)
    while True:
        try:
            next(it)
        except StopIteration as stop:
            return stop.value or {}


# ---------------- selftest（与 Node selftest 相同 fixture 与断言） ----------------

def selftest(root: Path) -> Dict[str, Any]:
    (root / "img").mkdir(parents=True, exist_ok=True)
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "img" / "p.png").write_bytes(bytes([1]) * 100)
    (root / "img" / "p(1).png").write_bytes(bytes([3]) * 60)
    (root / "img" / "orphan.png").write_bytes(bytes([2]) * 50)
    (root / "a.md").write_text(
        "# A\n\n![p](img/p.png)\n\n![paren](img/p(1).png)\n\n[ext](\"https://example.com/x\")\n\n"
        "[abs](C:/x/y.png)\n\n[anc](#head)\n\n[bad](img/nope.png)\n\n[[wiki-page]]\n\n[alias]([[激活函数]])\n",
        encoding="utf-8")
    (root / "sub" / "b.md").write_text("# B\n\n![p](../img/p.png)\n", encoding="utf-8")
    r = import_dir(root, set(DFLT_SKIP))
    c = r["summary"]["links"]
    a = r["summary"]["attachments"]
    checks: List[Tuple[str, bool]] = [
        ("md_files==2", r["summary"]["markdown"]["files"] == 2),
        ("attachments==3", a["files"] == 3),
        ("by_kind image==3", a["by_kind"].get("image") == 3),
        ("referenced==2", a["referenced"] == 2),
        ("never_referenced==1", a["never_referenced"] == 1),
        ("links_total==8", c["links_total"] == 8),
        ("resolved==3 (含路径带括号 + sub 相对)", c["resolved"] == 3),
        ("缺失==1", c["missing"] == 1),
        ("external==1（去引号后识别）", c["external"] == 1),
        ("absolute==1", c["absolute"] == 1),
        ("anchor==1", c["anchor"] == 1),
        ("wiki_in_link==1（[别名]([[页面]]) 不计缺失）", c["wiki_in_link"] == 1),
        ("resolve_rate==75", c["resolve_rate_on_relative"] == 75),
        ("wikilinks_info==2", c["wikilinks_info"] == 2),
        ("unresolved_total==2 (missing+absolute)", len(r["unresolved"]) == 2),
        ("孤儿附件识别", len(r["orphan"]) == 1 and r["orphan"][0] == "img/orphan.png"),
    ]

    # 第二批：密钥扫描 / 零字节 / 去重 / 断点复用
    root2 = root / "extra"
    (root2 / "assets").mkdir(parents=True, exist_ok=True)
    (root2 / "assets" / "a.bin").write_bytes(bytes([7]) * 10)
    (root2 / "assets" / "b.bin").write_bytes(bytes([7]) * 10)
    (root2 / "assets" / "empty.bin").write_bytes(b"")
    (root2 / "leak.md").write_text(
        "# 泄露样例\n\nkey: sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n"
        "![a](assets/a.bin)\n![b](assets/b.bin)\n![e](assets/empty.bin)\n", encoding="utf-8")
    r2 = import_dir(root2, set(DFLT_SKIP))
    checks.append(("密钥扫描命中 1 个文件", r2["summary"]["security"]["files_with_hits"] == 1))
    checks.append(("密钥规则为 openai_key", "openai_key" in r2["summary"]["security"]["rules"]))
    checks.append(("不记录密钥值", "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in json.dumps(r2["security"])))
    checks.append(("零字节附件计数", r2["summary"]["attachments"]["zero_byte"] == 1))
    checks.append(("同内容附件去重成 1 组", r2["summary"]["attachments"]["duplicate_groups"] == 1))
    r2b = import_dir(root2, set(DFLT_SKIP), {"resume": r2})
    checks.append(("断点复用：文件标记 unchanged", r2b["summary"]["markdown"]["unchanged"] == 1))
    checks.append(("断点复用：统计口径不变",
                   r2b["summary"]["links"]["links_total"] == r2["summary"]["links"]["links_total"]))

    # 第三批：平台归一（NFC / win32 大小写）与 Logseq 块引用
    root3 = root / "platform"
    (root3 / "pages").mkdir(parents=True, exist_ok=True)
    (root3 / "img").mkdir(parents=True, exist_ok=True)
    (root3 / "pages" / "Sample.md").write_text(
        "# Sample\n\nid:: 65F1A2B3-0000-4000-8000-00000000AAAA\n", encoding="utf-8")
    (root3 / "img" / "photo.png").write_bytes(bytes([5]) * 20)
    (root3 / "café.md").write_text("# NFD 文件名\n\n![图](IMG/PHOTO.PNG)\n", encoding="utf-8")
    (root3 / "links.md").write_text(
        "# 链接\n\n[[sample]]\n\n[[Sample]]\n\n[[不存在]]\n\n"
        "((65f1a2b3-0000-4000-8000-00000000aaaa))\n\n((65f1a2b3-0000-4000-8000-00000000bbbb))\n",
        encoding="utf-8")
    r3 = import_dir(root3, set(DFLT_SKIP))
    c3 = r3["summary"]["links"]
    a3 = r3["summary"]["attachments"]
    checks.append(("NFC 文件名归一到 manifest", any(e["path"] == "café.md" for e in r3["markdown"])))
    checks.append(("双链统计 3 条", c3["wiki_pages_total"] == 3))
    checks.append(("双链目标存在性（win32 大小写不敏感）", c3["wiki_pages_resolved"] == (2 if IS_WIN else 1)))
    checks.append(("块引用对账：命中 id:: 定义",
                   c3["block_refs"]["total"] == 2 and c3["block_refs"]["resolved"] == 1
                   and c3["block_refs"]["id_defs"] == 1))
    checks.append(("未命中的块引用进 unresolved 明细",
                   any(u["cause"] == "block_ref_missing" for u in r3["unresolved"])))
    if IS_WIN:
        checks.append(("win32 附件引用大小写不敏感", a3["referenced"] == 1 and a3["never_referenced"] == 0))
    else:
        checks.append(("非 win32 保留大小写差异（IMG/PHOTO.PNG 视为缺失）", a3["referenced"] == 0))
    checks.append(("长路径计数只在 win32 上报",
                   r3["summary"]["integrity"]["long_paths"] == (0 if IS_WIN else None)))
    r3b = import_dir(root3, set(DFLT_SKIP), {"resume": r3})
    checks.append(("断点复用时块引用对账不退化",
                   r3b["summary"]["links"]["block_refs"]["resolved"] == 1
                   and r3b["summary"]["markdown"]["unchanged"] == 3))

    failed = [name for name, ok in checks if not ok]
    return {"pass": len(failed) == 0, "checks": len(checks), "failed": failed,
            "fixture_root": norm(str(root)), "summary": r["summary"]}


def _arg(argv: List[str], name: str, dflt: str) -> str:
    if "--" + name in argv:
        i = argv.index("--" + name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return dflt


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        explicit = _arg(argv, "selftest-root", "")
        root = Path(explicit) if explicit else Path(tempfile.mkdtemp(prefix="awam-import-selftest-"))
        res = selftest(root)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if not explicit:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        return 0 if res["pass"] else 1

    root_s = _arg(argv, "root", "")
    if not root_s:
        print("需要 --root <dir>", file=sys.stderr)
        return 1
    skip = parse_skip(_arg(argv, "skip", ""))
    manifest = _arg(argv, "manifest", "")
    report = _arg(argv, "report", "")
    max_list = int(_arg(argv, "max-list", "50"))
    resume_s = _arg(argv, "resume", "")
    scan_secrets = "--no-scan-secrets" not in argv

    resume = None
    if resume_s:
        try:
            resume = json.loads(Path(resume_s).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print("无法读取 --resume manifest: " + str(e), file=sys.stderr)
            return 1

    r = import_dir(Path(root_s), skip, {"resume": resume, "scanSecrets": scan_secrets})
    if manifest:
        Path(manifest).parent.mkdir(parents=True, exist_ok=True)
        import time as _t
        tmp = manifest + ".tmp-" + str(os.getpid())
        Path(tmp).write_text(json.dumps({
            "version": 2,
            "generated_at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
            "summary": r["summary"], "markdown": r["markdown"], "attachments": r["attachments"],
            "unresolved": r["unresolved"], "security": r["security"],
            "duplicate_groups": r["duplicateGroups"],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, manifest)
    if report:
        s = r["summary"]
        L: List[str] = ["# 导入结果 · " + s["root"], ""]
        L.append("- skip=" + json.dumps(s["skip"], ensure_ascii=False)
                 + " · resume=" + ("yes" if s["resume"]["used"] else "no"))
        L.append("- Markdown 条目：**" + str(s["markdown"]["files"]) + "**（" + str(s["markdown"]["bytes"])
                 + " B）· 未变化（断点复用）" + str(s["markdown"]["unchanged"]))
        aa = s["attachments"]
        L.append("- 附件：**" + str(aa["files"]) + "**（" + str(aa["bytes"]) + " B）· 被引用 "
                 + str(aa["referenced"]) + " · 从未被引用 " + str(aa["never_referenced"])
                 + " · 零字节 " + str(aa["zero_byte"]) + " · 同内容组 " + str(aa["duplicate_groups"]))
        lk = s["links"]
        L.append("- 链接：总 " + str(lk["links_total"]) + " · 解析成功 " + str(lk["resolved"])
                 + " · 缺失 " + str(lk["missing"]) + " · 外部 " + str(lk["external"])
                 + " · 绝对 " + str(lk["absolute"]) + " · 锚点 " + str(lk["anchor"])
                 + " · 越出根 " + str(lk["escapes_root"]) + " · 内嵌双链 " + str(lk["wiki_in_link"]))
        L.append("- **相对路径可解析率：" + str(lk["resolve_rate_on_relative"]) + "%**（分母 = 解析成功+缺失）")
        L.append("- 缺失链接的文件名回找：唯一命中 " + str(lk["fallback_unique"]) + " · 多义 "
                 + str(lk["fallback_ambiguous"]) + " · 无候选 " + str(lk["fallback_none"]))
        sec = s["security"]
        L.append("- 密钥扫描：" + (("命中 " + str(sec["files_with_hits"]) + " 个文件 / "
                                   + str(sec["hits_total"]) + " 处") if sec["scan"] else "已关闭"))
        L += ["", "## 失败/丢失清单（前 " + str(max_list) + " 条 / 共 " + str(len(r["unresolved"])) + "）", "",
              "| Markdown | 目标 | 原因 | 形式 | 文件名回找候选 |", "|---|---|---|---|---|"]
        for u in r["unresolved"][:max_list]:
            L.append("| " + u["file"] + " | " + u["target"] + " | " + u["cause"] + " | " + u["form"]
                     + " | " + (str(u["basename_candidates"]) if "basename_candidates" in u else "-") + " |")
        if r["security"]["files"]:
            L += ["", "## 密钥模式命中（只列文件与规则）", "", "| 文件 | 规则 | 次数 |", "|---|---|---|"]
            for f in r["security"]["files"][:max_list]:
                L.append("| " + f["file"] + " | " + ", ".join(f["rules"]) + " | " + str(f["count"]) + " |")
        Path(report).write_text("\n".join(L), encoding="utf-8")
    print(json.dumps(r["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
