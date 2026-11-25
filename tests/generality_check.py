# SPDX-License-Identifier: MIT
"""通用性自检：非 Logseq 的纯 Markdown 知识目录全链支持（2026-11-25 定稿口径）。

口径：默认全量 + 白/黑名单配置。覆盖：
  A. iter_note_files 混合布局修复——恰含 pages/ 的普通 Markdown 目录不再被窄化为
     journals/pages 两目录（旧缺陷：pages/ 存在即只扫 pages/，其余静默丢弃）。
  B. 非 Logseq 纯 Markdown 目录（平铺 + 多级子目录 + 非 ASCII 目录 + 根级散落 md）
     走 entry→wizard（默认全量，无 --all-text）→ pipeline --apply → build_index
     → bm25 检索：所有目录的 md 均脱敏、均入索引、均可被召回。
  C. 白名单：sanitize.note_dirs / build_index.include_dirs 提供后仅白名单目录处理/索引。
  D. 黑名单：build_index.skip_dirs 排除指定目录。
全链在临时 KM_CONFIG_HOME 沙箱内运行（不碰真实用户配置 / 真源 / 秘密材料）。
运行：python tests/generality_check.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from awam_knowledge import (  # noqa: E402
    bm25, km_build_index, km_config, km_pipeline, km_sanitize, wizard,
)

# 命中内置规则 llm_api_key（sk-[A-Za-z0-9_\-]{20,}）
SECRET = "sk-" "GenSecretAb12Cd34Ef56"
SECRET_2 = "sk-" "GenSecretZx98Yw76Vu54"
TOKEN_RX = re.compile(r"\[REDACTED:[^\]]{1,40}:([0-9a-f]{8})\]")

_checks: list[tuple] = []


def ck(name: str, ok: bool) -> None:
    _checks.append((name, bool(ok)))


def w(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def r(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def run_step(fn, argv: list[str]) -> int:
    try:
        return fn(argv)
    except SystemExit as e:
        code = e.code
        return code if isinstance(code, int) else 1


def check_iter_note_files_mixed_layout(tmp: str) -> None:
    """A. 旧缺陷回归：pages/ 存在不再窄化扫描范围；白/黑名单参数生效。"""
    root = Path(tmp) / "mixed"
    (root / "pages").mkdir(parents=True)
    (root / "notes").mkdir(parents=True)
    (root / "daily").mkdir(parents=True)
    (root / ".git").mkdir(parents=True)
    (root / "pages" / "p.md").write_text("# page\nhello", encoding="utf-8")
    (root / "notes" / "n.md").write_text("# note\nworld", encoding="utf-8")
    (root / "daily" / "d.md").write_text("# daily\nfoo", encoding="utf-8")
    (root / "README.md").write_text("# readme", encoding="utf-8")
    (root / ".git" / "g.md").write_text("# git internal", encoding="utf-8")

    got = sorted(rel for _, rel in km_build_index.iter_note_files(root))
    ck("A 混合布局全量扫描", got == ["README.md", "daily/d.md", "notes/n.md", "pages/p.md"])
    ck("A .git 仍被内置黑名单跳过", ".git/g.md" not in got)

    inc = sorted(rel for _, rel in km_build_index.iter_note_files(root, include_dirs={"notes"}))
    ck("A 白名单只扫 notes(+根md)", inc == ["README.md", "notes/n.md"])

    skp = sorted(rel for _, rel in km_build_index.iter_note_files(root, skip_dirs={"daily"}))
    ck("A 黑名单排除 daily", skp == ["README.md", "notes/n.md", "pages/p.md"])

    # classify：默认全量；白名单严格；assets 不受影响
    ck("A classify 全量未知目录为 note", km_sanitize.classify("任意目录/x.md") == "note")
    ck("A classify 白名单外 skip", km_sanitize.classify("任意目录/x.md", {"notes"}) == "skip")
    ck("A classify 白名单内 note", km_sanitize.classify("notes/x.md", {"notes"}) == "note")
    ck("A classify assets 恒为 asset", km_sanitize.classify("assets/a.txt") == "asset")
    ck("A classify 根级 md 恒为 note", km_sanitize.classify("README.md", {"notes"}) == "note")


def check_generic_source_full_flow(tmp: str) -> dict:
    """B. 非 Logseq 纯 Markdown 目录：默认全量全链。"""
    home = os.path.join(tmp, "home")
    src = os.path.join(tmp, "plain-md")
    data_root = os.path.join(tmp, "data")
    os.makedirs(home)
    old_env = {k: os.environ.get(k) for k in
               ("KM_CONFIG_HOME", "KM_DATA_ROOT", "DESENS_DATA_ROOT")}
    os.environ["KM_CONFIG_HOME"] = home
    for k in ("KM_DATA_ROOT", "DESENS_DATA_ROOT"):
        os.environ.pop(k, None)
    try:
        # 假真源：非 Logseq 布局（无 journals/ 组织约定）
        w(os.path.join(src, "notes", "a.md"),
          f"# 笔记 A\n- 嵌套目录里的密钥 {SECRET}\n")
        w(os.path.join(src, "notes", "sub", "b.md"),
          "# 笔记 B\n- 更深层的内容 knowledge base content\n")
        w(os.path.join(src, "项目", "中文目录.md"),
          f"# 中文项目\n- 这里的密钥 {SECRET_2} 也要脱敏\n")
        w(os.path.join(src, "pages", "p.md"), "# 恰好叫 pages 的目录\n- page content\n")
        w(os.path.join(src, "daily", "2026-01-05.md"), "# 每日记录\n- daily note\n")
        w(os.path.join(src, "README.md"), f"# 根级散落\n- 根级密钥 {SECRET}\n")
        os.makedirs(os.path.join(src, "assets"), exist_ok=True)
        with open(os.path.join(src, "assets", "logo.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nfake-binary")
        w(os.path.join(src, ".git", "g.md"), "# git internal\n")

        # wizard 默认（无 --all-text / --note-dirs）→ 默认全量
        rc = run_step(wizard.main, ["--source", src, "--data-root", data_root,
                                    "--private-backup-dir", os.path.join(tmp, "backup")])
        ck("B wizard rc=0", rc == 0)
        sid = km_config.suggest_id(src)
        cfg = km_config.resolve_source(sid)
        ck("B 源配置 note_dirs 空=全量", cfg["sanitize"]["note_dirs"] == [])
        work = os.path.join(data_root, sid)
        # 交叉验证 work 目录推导
        ck("B 工作副本存在", os.path.isdir(work))

        rc = run_step(km_pipeline.main, ["--id", sid, "--apply"])
        ck("B pipeline apply rc=0", rc == 0)

        # 脱敏覆盖所有目录（全量）
        for rel, secret in [("notes/a.md", SECRET), ("项目/中文目录.md", SECRET_2),
                            ("README.md", SECRET)]:
            p = os.path.join(work, rel.replace("/", os.sep))
            ck(f"B 脱敏覆盖 {rel}", os.path.isfile(p) and secret not in r(p)
               and bool(TOKEN_RX.search(r(p))))
        ck("B 普通内容保留", "knowledge base content" in r(
            os.path.join(work, "notes", "sub", "b.md")))
        ck("B assets 二进制原样", os.path.isfile(os.path.join(work, "assets", "logo.png")))

        # 索引：默认全量，.git 被内置黑名单排除
        idx = json.loads(r(os.path.join(work, "index.json")))
        paths = {d["path"] for d in idx["docs"]}
        expect = {"notes/a.md", "notes/sub/b.md", "项目/中文目录.md",
                  "pages/p.md", "daily/2026-01-05.md", "README.md"}
        ck("B 索引全量覆盖", paths == expect)
        ck("B 非 journals 布局无 journal 假设",
           all(d["type"] == "page" for d in idx["docs"]))
        ck("B .git 不入索引", not any(p.startswith(".git/") for p in paths))

        # bm25 检索：嵌套/中文目录可召回（与 http /search 同一内核）
        corpus = bm25.build_corpus({"root": work})
        cidx = bm25.build_index(corpus["docs"])
        hits = bm25.search(corpus, cidx, "knowledge base content", {"topk": 3})
        ck("B bm25 可召回嵌套目录", bool(hits) and hits[0]["path"] == "notes/sub/b.md")
        hits_cn = bm25.search(corpus, cidx, "中文项目", {"topk": 3})
        ck("B bm25 可召回中文目录", bool(hits_cn)
           and hits_cn[0]["path"] == "项目/中文目录.md")
        return {"sid": sid, "work": work, "home": home, "src": src}
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def check_whitelist_blacklist(tmp: str, ctx: dict) -> None:
    """C/D. 白名单（note_dirs / include_dirs）与黑名单（skip_dirs）配置。"""
    home = ctx["home"]
    old = os.environ.get("KM_CONFIG_HOME")
    os.environ["KM_CONFIG_HOME"] = home
    try:
        # C. 新源走白名单：sanitize 只处理 notes/；build_index 只索引 notes/
        src2 = os.path.join(tmp, "plain-md-wl")
        data_root2 = os.path.join(tmp, "data-wl")
        w(os.path.join(src2, "notes", "a.md"),
          f"# 白名单笔记\n- 密钥 {SECRET}\n")
        w(os.path.join(src2, "raw", "c.md"),
          f"# 白名单外笔记\n- 这里的密钥 {SECRET_2} 不应被处理\n")
        rc = run_step(wizard.main, [
            "--source", src2, "--data-root", data_root2, "--note-dirs", "notes",
            "--private-backup-dir", os.path.join(tmp, "backup-wl")])
        ck("C wizard --note-dirs rc=0", rc == 0)
        sid2 = km_config.suggest_id(src2)

        # 源配置 build_index.include_dirs 白名单
        raw = km_config.load_source(sid2)
        raw["build_index"] = {"include_dirs": ["notes"], "skip_dirs": []}
        km_config.save_source(sid2, raw)

        rc = run_step(km_pipeline.main, ["--id", sid2, "--apply"])
        ck("C pipeline apply rc=0", rc == 0)
        work2 = os.path.join(data_root2, sid2)
        a = r(os.path.join(work2, "notes", "a.md"))
        c = r(os.path.join(work2, "raw", "c.md"))
        ck("C 白名单内已脱敏", SECRET not in a and bool(TOKEN_RX.search(a)))
        ck("C 白名单外原样保留", SECRET_2 in c)
        idx2 = json.loads(r(os.path.join(work2, "index.json")))
        ck("C include_dirs 只索引白名单",
           {d["path"] for d in idx2["docs"]} == {"notes/a.md"})

        # D. 黑名单：include_dirs 清空 + skip_dirs=raw → 索引含 notes 不含 raw
        raw = km_config.load_source(sid2)
        raw["build_index"] = {"include_dirs": [], "skip_dirs": ["raw"]}
        km_config.save_source(sid2, raw)
        rc = run_step(km_build_index.main, ["--id", sid2])
        ck("D build_index --id rc=0", rc == 0)
        idx3 = json.loads(r(os.path.join(work2, "index.json")))
        paths3 = {d["path"] for d in idx3["docs"]}
        ck("D 黑名单排除 raw", "raw/c.md" not in paths3)
        ck("D 黑名单下其余全量", "notes/a.md" in paths3)
    finally:
        if old is None:
            os.environ.pop("KM_CONFIG_HOME", None)
        else:
            os.environ["KM_CONFIG_HOME"] = old


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    tmp = tempfile.mkdtemp(prefix="gen-check-")
    try:
        check_iter_note_files_mixed_layout(tmp)
        ctx = check_generic_source_full_flow(tmp)
        check_whitelist_blacklist(tmp, ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in _checks if not ok]
    print(json.dumps({"selftest": True, "checks": len(_checks),
                      "pass": not failed, "failed": failed},
                     ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
