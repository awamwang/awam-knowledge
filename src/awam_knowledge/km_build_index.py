# AI生成
# -*- coding: utf-8 -*-
"""km_build_index — 为脱敏工作副本生成旁路 Index（不改写 .md 正文）。

移植自 knowledge-desensitization/scripts/build_index.py（M3 融合，语义不变）。

设计
- 只写旁路 JSON（index.json / journal-date-map.json / block-refs.json / 可选
  index-report.md）到工作副本根；**不改写任何 .md**，以免破坏脱敏幂等。
- Index 落点始终在工作副本根（或 `--out-dir` 指定的其下子目录）。
- --write-frontmatter 可开关、默认关；开启时才往 md 头部插入 frontmatter（会改写
  正文，建议只在临时副本上用）。

用法
    python -m awam_knowledge.km_build_index --id logseq-main
    python -m awam_knowledge.km_build_index --root <data_root>/logseq-main
    python -m awam_knowledge.km_build_index --id logseq-main --dry-run
    python -m awam_knowledge.km_build_index --id logseq-main --out-dir <子目录>   # 仍须在工作副本下

输入解析优先级
    1) --root <工作副本目录>
    2) --id + (--data-root | KM_DATA_ROOT | settings.yaml data_root) → <data_root>/<id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from . import km_config
from . import km_paths as paths

PAGE_PROP_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)::\s?(.*)$")
BLOCK_ID_RE = re.compile(r"^\s*id::\s*([0-9a-fA-F-]{36})\s*$")
LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
BLOCKREF_RE = re.compile(r"\(\(([0-9a-fA-F-]{36})\)\)")
ASSET_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)>\s]+)>?\s*\)")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# 扫描时跳过的目录名（任一路径段命中即跳过）
SKIP_DIR_NAMES = {
    ".km", "vault", ".git", "bak", ".recycle", "recycle",
    "node_modules", "__pycache__", ".obsidian", "logseq",
}

NOTE_EXTS = {".md", ".markdown"}


def ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def journal_alias(date_str: str) -> str:
    y, m, d = date_str.split("-")
    return f"{MONTHS[int(m) - 1]} {ordinal(int(d))}, {y}"


def cn_journal_alias(date_str: str) -> str:
    y, m, d = date_str.split("-")
    return f"{y}年{int(m)}月{int(d)}日"


def journal_date(stem: str) -> str | None:
    try:
        parts = stem.replace("_", "-").split("-")
        if len(parts) != 3:
            return None
        y, m, d = parts
        return f"{y}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return None


def split_page_props(text: str) -> tuple[dict, str]:
    """Return (props, body). Props = leading contiguous non-bullet key:: lines."""
    lines = text.split("\n")
    props: dict = {}
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "" or ln.lstrip().startswith("-"):
            break
        m = PAGE_PROP_RE.match(ln.strip())
        if not m:
            break
        props[m.group(1)] = m.group(2).strip()
        i += 1
    return props, "\n".join(lines[i:])


def strip_wikilink(v: str) -> str:
    m = LINK_RE.fullmatch(v.strip())
    return m.group(1).strip() if m else v.strip()


def should_skip_dir(rel_parts: tuple[str, ...]) -> bool:
    return any(p in SKIP_DIR_NAMES or p.lower() in SKIP_DIR_NAMES for p in rel_parts)


def yaml_scalar(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(str(x), ensure_ascii=False) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def dump_frontmatter(d: dict) -> str:
    out = ["---"]
    for k, v in d.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, dict):
            out.append(f"{k}:")
            for kk, vv in v.items():
                out.append(f"  {kk}: {yaml_scalar(vv)}")
        else:
            out.append(f"{k}: {yaml_scalar(v)}")
    out.append("---")
    return "\n".join(out)


def resolve_work_root(args: argparse.Namespace) -> Path:
    if args.root:
        return Path(os.path.abspath(args.root))
    if not args.id:
        raise SystemExit("须指定 --id 或 --root")
    cfg = km_config.resolve_source(args.id)
    data_root = args.data_root or cfg.get("data_root")
    if not data_root:
        data_root = os.environ.get("KM_DATA_ROOT") or os.environ.get("DESENS_DATA_ROOT")
    if not data_root:
        raise SystemExit(
            "缺数据根：给 --data-root / 设 KM_DATA_ROOT / 或先跑 wizard 写 settings.yaml"
        )
    work = paths.work_dir(os.path.abspath(data_root), args.id, cfg.get("work_dir"))
    return Path(work)


def iter_note_files(root: Path):
    """Yield (abs_path, rel_posix) for md notes under journals/ pages/ (and root md)."""
    preferred = []
    for sub in ("journals", "pages"):
        d = root / sub
        if d.is_dir():
            preferred.append(d)
    # 也扫根下散落 md（不含子目录已覆盖的）
    search_roots = preferred if preferred else [root]
    seen = set()
    for base in search_roots:
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in NOTE_EXTS:
                continue
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts
            if should_skip_dir(parts[:-1]):
                continue
            key = rel.as_posix()
            if key in seen:
                continue
            seen.add(key)
            yield p, key


def extract_assets(text: str, asset_names: set[str]) -> list[str]:
    found = []
    for a in ASSET_RE.findall(text):
        a = a.strip("\"'")
        if a.startswith(("http://", "https://")) or a.startswith("[["):
            continue
        if "assets/" in a:
            nm = a.split("assets/")[-1].lstrip("./")
            if nm in asset_names or any(n.startswith(nm) for n in asset_names):
                found.append(nm)
    # 廉价：文件名直接出现在正文
    for name in asset_names:
        if f"assets/{name}" in text and name not in found:
            found.append(name)
    return sorted(set(found))


def build(root: Path, write_frontmatter: bool, dry_run: bool) -> dict:
    asset_dir = root / "assets"
    asset_names = set()
    if asset_dir.is_dir():
        asset_names = {p.name for p in asset_dir.iterdir() if p.is_file()}

    index: list[dict] = []
    block_ids: dict = {}
    journal_alias_map: dict = {}
    empty_journals: list[str] = []
    skipped_decode = 0
    frontmatter_written = 0

    files = list(iter_note_files(root))

    # pass 1: titles / aliases（廉价）
    title_set: set[str] = set()
    for f, rel in files:
        if rel.startswith("journals/"):
            date = journal_date(Path(rel).stem)
            if date:
                title_set.add(date)
                journal_alias_map[journal_alias(date)] = date
                journal_alias_map[cn_journal_alias(date)] = date
                journal_alias_map[date] = date
        else:
            try:
                raw = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            props, _ = split_page_props(raw)
            title_set.add(props.get("title") or Path(rel).stem.replace("___", "/"))

    # pass 2: extract
    for f, rel in files:
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped_decode += 1
            continue

        props, body = split_page_props(raw)
        is_journal = rel.startswith("journals/")
        links = sorted(set(LINK_RE.findall(raw)))
        ids_here = [m.group(1) for ln in raw.split("\n") if (m := BLOCK_ID_RE.match(ln))]
        for bid in ids_here:
            block_ids[bid] = {"file": rel}

        entry: dict = {
            "path": rel,
            "title": None,
            "type": "journal" if is_journal else "page",
        }

        if is_journal:
            date = journal_date(Path(rel).stem)
            entry["title"] = date or Path(rel).stem
            if date:
                entry["date"] = date
            if body.strip() in ("", "-"):
                empty_journals.append(rel)
        else:
            entry["title"] = props.get("title") or Path(rel).stem.replace("___", "/")
            if "date" in props:
                entry["date"] = strip_wikilink(props["date"])
            if props.get("tags"):
                tags = [t for t in re.split(r"[,\s]+", props["tags"]) if t]
                if tags:
                    entry["tags"] = tags

        if links:
            entry["links"] = links
        if ids_here:
            entry["block_ids"] = ids_here
        assets = extract_assets(raw, asset_names)
        if assets:
            entry["assets"] = assets

        index.append(entry)

        if write_frontmatter and not dry_run:
            fm = {
                "title": entry["title"],
                "type": entry["type"],
                "date": entry.get("date"),
                "tags": entry.get("tags", []),
                "links": links,
                "block_ids": ids_here,
                "assets": assets,
                "source_file": rel,
            }
            # 仅当文件尚无 YAML frontmatter 时写入，避免重复套娃
            if not raw.lstrip().startswith("---"):
                new_text = dump_frontmatter(fm) + "\n\n" + body
                f.write_text(new_text, encoding="utf-8")
                frontmatter_written += 1

    return {
        "root": str(root).replace("\\", "/"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(index),
        "docs": index,
        "block_ids": block_ids,
        "journal_alias_map": journal_alias_map,
        "empty_journals": empty_journals,
        "skipped_decode": skipped_decode,
        "frontmatter_written": frontmatter_written,
        "asset_files": len(asset_names),
        "files_scanned": len(files),
    }


def write_outputs(out_dir: Path, result: dict, dry_run: bool, with_report: bool) -> list[str]:
    written = []
    index_payload = {
        "generated_from": result["root"],
        "generated_at": result["generated_at"],
        "count": result["count"],
        "docs": result["docs"],
        "note": "旁路 Index；默认不改写 .md。见 awam_knowledge.km_build_index",
    }
    targets = {
        "index.json": index_payload,
        "journal-date-map.json": result["journal_alias_map"],
        "block-refs.json": result["block_ids"],
    }
    for name, payload in targets.items():
        path = out_dir / name
        written.append(str(path).replace("\\", "/"))
        if dry_run:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if with_report:
        n_j = sum(1 for d in result["docs"] if d["type"] == "journal")
        n_p = sum(1 for d in result["docs"] if d["type"] == "page")
        rep = [
            "# Index 生成简报",
            "",
            f"- 工作副本: `{result['root']}`",
            f"- 生成时间: {result['generated_at']}",
            f"- 扫描 md: {result['files_scanned']}",
            f"- 日记: {n_j}  页面: {n_p}  合计: {result['count']}",
            f"- block ids: {len(result['block_ids'])}",
            f"- journal 别名映射: {len(result['journal_alias_map'])}",
            f"- assets 目录文件数: {result['asset_files']}",
            f"- 空日记: {len(result['empty_journals'])}",
            f"- 解码失败跳过: {result['skipped_decode']}",
            f"- frontmatter 写入: {result['frontmatter_written']}（默认应为 0）",
            "",
            "## 约定",
            "",
            "- 本模块默认**只写旁路 JSON**，不改写正文，不破坏脱敏幂等。",
            "- Index 落点在脱敏工作副本根（或 `--out-dir` 子目录）。",
            "- awam 默认召回 BM25 + 标题×4；hybrid 非默认。",
            "",
            "## 下一步",
            "",
            "交给 awam-knowledge：`--root` 指向本工作副本（含 `index.json`）。",
        ]
        rpath = out_dir / "index-report.md"
        written.append(str(rpath).replace("\\", "/"))
        if not dry_run:
            rpath.write_text("\n".join(rep) + "\n", encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="awam_knowledge.km_build_index",
        description="为脱敏工作副本生成旁路 Index（默认不改写 .md）"
    )
    ap.add_argument("--id", help="源 id（如 logseq-main）")
    ap.add_argument("--root", help="工作副本根目录（优先于 --id）")
    paths.add_root_arg(ap)
    ap.add_argument(
        "--out-dir",
        help="Index 输出目录；缺省 = 工作副本根。须位于工作副本之下。",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    ap.add_argument(
        "--write-frontmatter",
        action="store_true",
        help="向 md 写入 YAML frontmatter（默认关；会改写正文，可能影响脱敏幂等）",
    )
    ap.add_argument("--no-report", action="store_true", help="不写 index-report.md")
    args = ap.parse_args(argv)

    if not args.root and not args.id:
        ap.error("须指定 --id 或 --root")

    root = resolve_work_root(args)
    if not root.is_dir():
        print(f"工作副本不存在: {root}", file=sys.stderr)
        return 2

    out_dir = Path(os.path.abspath(args.out_dir)) if args.out_dir else root

    result = build(root, write_frontmatter=args.write_frontmatter, dry_run=args.dry_run)
    written = write_outputs(
        out_dir, result, dry_run=args.dry_run, with_report=not args.no_report
    )

    n_j = sum(1 for d in result["docs"] if d["type"] == "journal")
    n_p = sum(1 for d in result["docs"] if d["type"] == "page")
    summary = {
        "mode": "dry-run" if args.dry_run else "write",
        "root": result["root"],
        "out_dir": str(out_dir).replace("\\", "/"),
        "files_scanned": result["files_scanned"],
        "journals": n_j,
        "pages": n_p,
        "docs": result["count"],
        "block_ids": len(result["block_ids"]),
        "journal_aliases": len(result["journal_alias_map"]),
        "empty_journals": len(result["empty_journals"]),
        "frontmatter_written": result["frontmatter_written"],
        "write_frontmatter": bool(args.write_frontmatter),
        "outputs": written,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
