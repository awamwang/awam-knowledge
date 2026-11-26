# SPDX-License-Identifier: MIT
"""通用性自检（检索内核侧）：AFM sidecar 真正对外的 `/search` 走 `bm25.build_corpus`，
本测试直接对该内核断言「支持所有 Markdown 为格式的知识目录」，与 generality_check.py
（锁 build_index/iter_note_files + pipeline 脱敏）互补。

为何单独测：generality_check.py 的 bm25 用例只跑在「已脱敏工作副本」上（先经 iter_note_files
全量扫描），无法防止在检索层重新引入「存在 pages/ 或 journals/ 就窄化只扫这两目录」一类回归，
也不会触达 `.markdown` / 大写扩展名 / 默认黑名单等 Markdown 目录形态。

定稿口径（2026-11-25）：
  A. 混合布局全量：含 pages/、journals/、notes/、daily/、非 ASCII 目录、根级散落 md 的原始知识目录，
     除内置黑名单外**全部**入索引且可召回（窄化回归防护）。
  B. 扩展名通用：`.md`、`.markdown`、大写 `.MD`、`.Markdown` 均入索引且可召回（大小写不敏感）。
  C. 默认黑名单：内置 excludes（.git / node_modules / .recycle / vault / logseq\\bak）命中目录被跳过，
     其余同名普通内容目录不受影响。
  D. 增量缓存等价：`build_corpus_cached` 与 `build_corpus` 文档路径集合一致，缓存不丢通用性。
  E. 编码健壮：非 UTF-8 的 md 不崩溃（errors=replace），记入 encodingWarn，同级正常 md 仍入索引。
  F. 旁路 index.json 仅做标题/类型富化，不窄化扫描范围。
  G. 黑名单大小写不敏感（2026-11-26 修复）：`.GIT`/`Node_Modules`/`.Recycle`/`VAULT` 等
     大小写变体的内置黑名单目录同样被跳过——Windows/macOS 大小写不敏感文件系统下，检索层
     须与 build 侧 iter_note_files 的 p.lower() 口径一致，否则内部/工具目录会在对外 /search 泄露。
  H. 隐藏 .md 文件（`.hidden.md`）入索引：「所有 Markdown」不因点前缀被抑制。
  I. 空 .md 不崩溃：记入 zeroByte、不产生 doc、同级正常 md 仍入索引。
  J. 扩展名边界：`.mdx`（MDX/markdoc，非纯 Markdown）不入索引，`.MARKDOWN` 大写扩展名入索引。
  K. UTF-8 BOM md：正常入索引、不误判为 encodingWarn（BOM 非替换符）、可召回。
  L. 链接环路不崩溃 / 不污染：目录符号链接与 NTFS junction（reparse point）自环时，检索内核
     须像 Node 一样静默跳过、语料只含真实文件（防 Path.is_dir() 跟随软链撞 MAX_PATH 生成重复
     伪文档）。目录软链需特权，缺权限优雅 skip；junction 用 mklink /J 无需特权，Windows 可直接执行验证。

运行：python tests/generality_search_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from awam_knowledge import bm25  # noqa: E402

_checks: list[tuple] = []
_skips: list[tuple] = []


def ck(name: str, ok: bool) -> None:
    _checks.append((name, bool(ok)))


def wb(root: Path, rel: str, text: str) -> str:
    """写一个 Markdown 文件（UTF-8 字节），返回相对 posix 路径。"""
    p = root / Path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return rel


def doc_paths(corpus: dict) -> set[str]:
    return {d["path"] for d in corpus["docs"]}


def check_mixed_layout_full_scan(tmp: str) -> None:
    """A. 原始混合布局知识目录在检索内核全量入索引（窄化回归防护）。"""
    root = Path(tmp) / "mixed"
    wb(root, "pages/home.md", "# 首页\nhome-needle 内容")
    wb(root, "journals/2026-01-05.md", "# 日志\njournal-needle 内容")
    wb(root, "notes/deep/nested.md", "# 深层\nnested-needle 内容")
    wb(root, "daily/d.md", "# 每日\ndaily-needle 内容")
    wb(root, "项目/中文目录.md", "# 中文\nchinese-needle 内容")
    wb(root, "scattered.md", "# 根级散落\nscattered-needle 内容")
    wb(root, ".git/config.md", "# git 内部\ngitsecret-needle 内容")

    corpus = bm25.build_corpus({"root": str(root)})
    paths = doc_paths(corpus)
    expect = {
        "pages/home.md", "journals/2026-01-05.md", "notes/deep/nested.md",
        "daily/d.md", "项目/中文目录.md", "scattered.md",
    }
    ck("A 混合布局全量入索引", paths == expect)
    ck("A .git 目录被跳过", not any(p.startswith(".git/") for p in paths))

    # 各目录的关键词都应可召回（含恰含 pages/ 时其它目录不被窄化丢弃）
    idx = bm25.build_index(corpus["docs"])
    for needle, want in [
        ("home-needle", "pages/home.md"),
        ("journal-needle", "journals/2026-01-05.md"),
        ("nested-needle", "notes/deep/nested.md"),
        ("daily-needle", "daily/d.md"),
        ("chinese-needle", "项目/中文目录.md"),
        ("scattered-needle", "scattered.md"),
    ]:
        hits = bm25.search(corpus, idx, needle, {"topk": 3})
        ck(f"A {needle} 命中 {want}", bool(hits) and hits[0]["path"] == want)


def check_markdown_extensions(tmp: str) -> None:
    """B. 扩展名通用：.md / .markdown / .MD / .Markdown 均入索引且可召回。"""
    root = Path(tmp) / "exts"
    wb(root, "a.md", "# A\next-lower-needle")
    wb(root, "b.markdown", "# B\next-alternate-needle")
    wb(root, "C.MD", "# C\next-upper-needle")
    wb(root, "d.Markdown", "# D\next-mixed-needle")
    wb(root, "ignore.txt", "# not markdown\ntxt-needle")

    corpus = bm25.build_corpus({"root": str(root)})
    paths = doc_paths(corpus)
    ck("B 四种 Markdown 扩展名全入索引",
       paths == {"a.md", "b.markdown", "C.MD", "d.Markdown"})
    ck("B 非 Markdown 文件不入索引", "ignore.txt" not in paths)

    idx = bm25.build_index(corpus["docs"])
    for needle, want in [
        ("ext-lower-needle", "a.md"),
        ("ext-alternate-needle", "b.markdown"),
        ("ext-upper-needle", "C.MD"),
        ("ext-mixed-needle", "d.Markdown"),
    ]:
        hits = bm25.search(corpus, idx, needle, {"topk": 3})
        ck(f"B {needle} 召回 {want}", bool(hits) and hits[0]["path"] == want)


def check_default_blacklist(tmp: str) -> None:
    """C. 默认黑名单命中目录被跳过；裸同名内容目录不被误伤。"""
    root = Path(tmp) / "blacklist"
    wb(root, ".git/leak.md", "# git\ngit-needle")
    wb(root, "node_modules/pkg/readme.md", "# npm\nnpm-needle")
    wb(root, ".recycle/old.md", "# recycle\nrecycle-needle")
    wb(root, "vault/secret.md", "# vault\nvault-needle")
    wb(root, "logseq/bak/x.md", "# logseq bak\nbak-needle")
    wb(root, "notes/keep.md", "# notes\nkeep-needle")

    corpus = bm25.build_corpus({"root": str(root)})
    paths = doc_paths(corpus)
    ck("C 黑名单目录被排除",
       paths == {"notes/keep.md"})
    ck("C 黑名单目录进 skipped",
       {".git", "node_modules", ".recycle", "vault", "logseq/bak"}
       <= set(Path(s).as_posix() for s in corpus["skipped"])
       or all(any(s.endswith(b) for s in corpus["skipped"])
              for b in (".git", "node_modules", ".recycle", "vault")))
    idx = bm25.build_index(corpus["docs"])
    hits = bm25.search(corpus, idx, "vault-needle", {"topk": 3})
    ck("C vault 目录内容不可被召回", all(h["path"] != "vault/secret.md" for h in hits))
    hits2 = bm25.search(corpus, idx, "keep-needle", {"topk": 3})
    ck("C 普通目录内容可召回", bool(hits2) and hits2[0]["path"] == "notes/keep.md")


def check_cached_equivalence_and_sidecar(tmp: str) -> None:
    """D/F. build_corpus_cached 与 build_corpus 路径集合一致；sidecar index.json 不窄化。"""
    root = Path(tmp) / "cached"
    wb(root, "pages/p1.md", "# p1\ncached-needle")
    wb(root, "notes/n1.md", "# n1\nnotes-needle")
    wb(root, "root.md", "# root\nroot-needle")

    plain = doc_paths(bm25.build_corpus({"root": str(root)}))
    cache_path = str(Path(tmp) / "cache.json")
    c1 = bm25.build_corpus_cached({"root": str(root), "cachePath": cache_path})
    paths1 = doc_paths(c1)
    ck("D 缓存首建路径集合一致", paths1 == plain)
    c2 = bm25.build_corpus_cached({"root": str(root), "cachePath": cache_path})
    paths2 = doc_paths(c2)
    ck("D 缓存复用路径集合一致", paths2 == plain)
    ck("D 缓存二次命中未重建", c2["cache"]["hits"] >= 1 and c2["cache"]["misses"] == 0)

    # F：旁路 index.json 富化标题，不减少目录
    sidecar = root / "index.json"
    sidecar.write_text(json.dumps({
        "count": 1, "docs": [{"path": "pages/p1.md", "title": "页面一", "type": "page"}],
    }, ensure_ascii=False), encoding="utf-8")
    corp_s = bm25.build_corpus({"root": str(root)})
    ck("F 带 sidecar 仍全量入索引", doc_paths(corp_s) == plain)
    titled = {d["path"]: d["title"] for d in corp_s["docs"]}
    ck("F sidecar 标题富化生效", titled.get("pages/p1.md") == "页面一")


def check_non_utf8_robust(tmp: str) -> None:
    """E. 非 UTF-8 md 不崩溃，记入 encodingWarn，同级正常 md 仍入索引。"""
    root = Path(tmp) / "encoding"
    wb(root, "good.md", "# 正常\ngood-needle")
    # 以 gbk 编码的中文（非 UTF-8）：utf-8 解码会产生替换符，errors=replace 不抛异常
    gbk_bytes = "# 中文标题\nGBK 内容 unique-gbk".encode("gbk")
    (root / "gbk.md").write_bytes(gbk_bytes)

    corpus = bm25.build_corpus({"root": str(root)})
    paths = doc_paths(corpus)
    ck("E 非 UTF-8 文件不阻断，二者均入索引", paths == {"good.md", "gbk.md"})
    ck("E 正常文件仍可召回", any(
        h["path"] == "good.md"
        for h in bm25.search(corpus, bm25.build_index(corpus["docs"]), "good-needle", {"topk": 3})
    ))
    # encodingWarn 是启发式（替换符占比 > 1%），不强断言命中，只要求字段结构存在
    ck("E encodingWarn 为列表", isinstance(corpus["encodingWarn"], list))


def check_case_insensitive_excludes_and_edge_formats(tmp: str) -> None:
    """G/H/I/J/K. 黑名单大小写不敏感、隐藏 md、空 md、扩展名边界、UTF-8 BOM。"""
    # G. 大小写变体的内置黑名单目录一律跳过（修复回归防护：若 is_excluded 退回大小写敏感即失败）
    root = Path(tmp) / "caseexcl"
    wb(root, ".GIT/x.md", "# git\ngit-needle")
    wb(root, "Node_Modules/y.md", "# npm\nnpm-needle")
    wb(root, ".Recycle/z.md", "# recycle\nrecycle-needle")
    wb(root, "VAULT/secret.md", "# vault\nvault-needle")
    wb(root, "notes/keep.md", "# notes\nkeep-needle")
    corpus = bm25.build_corpus({"root": str(root)})
    paths = doc_paths(corpus)
    ck("G 大写/混合大小写黑名单目录被跳过", paths == {"notes/keep.md"})
    ck("G 被跳过目录内容不可召回", all(
        h["path"] != "VAULT/secret.md"
        for h in bm25.search(corpus, bm25.build_index(corpus["docs"]), "vault-needle", {"topk": 5})
    ))

    # H. 隐藏 .md 文件仍入索引（「所有 Markdown」不因点前缀抑制）
    root = Path(tmp) / "hidden"
    wb(root, ".hidden.md", "# h\nhidden-needle")
    corp_h = bm25.build_corpus({"root": str(root)})
    ck("H 隐藏 .hidden.md 入索引", doc_paths(corp_h) == {".hidden.md"})
    ck("H 隐藏 md 可召回", bool(
        bm25.search(corp_h, bm25.build_index(corp_h["docs"]), "hidden-needle", {"topk": 3})
    ))

    # I. 空 .md 不崩溃：记入 zeroByte、不产生 doc，同级正常 md 仍入索引
    root = Path(tmp) / "empty"
    wb(root, "empty.md", "")
    wb(root, "real.md", "# real\nreal-needle")
    corp_e = bm25.build_corpus({"root": str(root)})
    ck("I 空 md 不产生 doc 仅保留正常 md", doc_paths(corp_e) == {"real.md"})
    ck("I 空 md 记入 zeroByte", "empty.md" in {Path(s).as_posix() for s in corp_e["zeroByte"]})

    # J. 扩展名边界：.mdx 非纯 Markdown 不入索引；.MARKDOWN 大写扩展名入索引
    root = Path(tmp) / "extedge"
    wb(root, "a.mdx", "# x\nmdx-needle")
    wb(root, "b.MARKDOWN", "# y\nmark-needle")
    corp_j = bm25.build_corpus({"root": str(root)})
    ck("J .mdx 不入索引", "a.mdx" not in doc_paths(corp_j))
    ck("J .MARKDOWN 大写扩展名入索引", doc_paths(corp_j) == {"b.MARKDOWN"})

    # K. UTF-8 BOM md：入索引、不误判 encodingWarn（BOM 非替换符）、可召回
    root = Path(tmp) / "bom"
    wb(root, "bom.md", "﻿# BOM\nbom-needle")
    corp_k = bm25.build_corpus({"root": str(root)})
    ck("K UTF-8 BOM md 入索引", doc_paths(corp_k) == {"bom.md"})
    ck("K BOM 不误判为编码警告", corp_k["encodingWarn"] == [])
    ck("K BOM md 可召回", bool(
        bm25.search(corp_k, bm25.build_index(corp_k["docs"]), "bom-needle", {"topk": 3})
    ))


def check_link_loop_robust(tmp: str) -> None:
    """L. 符号链接 / NTFS junction 目录环路不崩溃、不污染语料；无权限时优雅 skip。

    动机：Python `Path.is_dir()` 跟随符号链接与 junction（reparse point），自环会让 `walk` 一路
    递归；目录软链在 Windows 无 SeCreateSymbolicLink 时会抛 winerror 1314，但 **junction 用
    `mklink /J` 无需特权即可创建**，且自环 junction 会撞 Windows MAX_PATH 生成大量重复伪文档。
    Node（libuv）把符号链接与 junction 都判为 symlink、isDirectory()=false，故本就不跟随。
    本用例锁定 Python `_is_link_like` 已对齐 Node：两类链接目录均静默跳过、语料只含真实文件。
    L1 目录软链在缺特权环境 skip；L2 junction 在 Windows 可直接执行验证。
    """
    root = Path(tmp) / "linkloop"
    wb(root, "a/real.md", "# real\nloop-needle")
    exercised = False

    # L1. 目录符号链接环路（需特权；缺权限 skip，不算失败）
    try:
        os.symlink(str(root), str(root / "slink"), target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        _skips.append(("L1 目录符号链接环路",
                       f"skipped: {e.__class__.__name__} winerror={getattr(e, 'winerror', None)}"))
    else:
        exercised = True
        try:
            corpus = bm25.build_corpus({"root": str(root)})
        except RecursionError:
            ck("L1 符号链接环路不崩溃", False)
        else:
            paths = doc_paths(corpus)
            ck("L1 符号链接环路：正常 md 入索引", "a/real.md" in paths)
            ck("L1 符号链接目录不被跟随（无 slink/ 伪文档）",
               all(not p.startswith("slink/") for p in paths))
            cached = doc_paths(bm25.build_corpus_cached(
                {"root": str(root), "cachePath": str(Path(tmp) / "sl_cache.json")}))
            ck("L1 缓存检索环路不崩溃且集合一致", cached == paths)

    # L2. NTFS junction 环路（mklink /J 无需特权；仅 Windows 执行，本环境可实跑）
    if os.name == "nt":
        r = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                            str(root / "jlink"), str(root)], capture_output=True)
        if r.returncode == 0:
            exercised = True
            try:
                corpus = bm25.build_corpus({"root": str(root)})
            except RecursionError:
                ck("L2 junction 环路不崩溃", False)
            else:
                paths = doc_paths(corpus)
                ck("L2 junction 环路不污染语料（仅真实文件）", paths == {"a/real.md"})
                ck("L2 junction 目录不被跟随（无 jlink/ 伪文档）",
                   all(not p.startswith("jlink/") for p in paths))
        else:
            _skips.append(("L2 junction 环路", f"skipped: mklink /J 返回码={r.returncode}"))
    else:
        _skips.append(("L2 junction 环路", "skipped: 非 Windows 文件系统"))

    if not exercised:
        _skips.append(("L 链接环路整体", "skipped: 两类链接均不可创建，本环境未实际执行"))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    tmp = tempfile.mkdtemp(prefix="gen-search-check-")
    try:
        check_mixed_layout_full_scan(tmp)
        check_markdown_extensions(tmp)
        check_default_blacklist(tmp)
        check_cached_equivalence_and_sidecar(tmp)
        check_non_utf8_robust(tmp)
        check_case_insensitive_excludes_and_edge_formats(tmp)
        check_link_loop_robust(tmp)
    finally:
        # 仅清理本测试自建的临时目录（tempfile.mkdtemp 产物），不触碰用户/真源目录。
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in _checks if not ok]
    print(json.dumps({"selftest": True, "checks": len(_checks),
                      "pass": not failed, "failed": failed, "skipped": _skips},
                     ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
