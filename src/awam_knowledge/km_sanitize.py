# AI生成
"""km_sanitize — 脱敏主程序：生成 sanitized 镜像 + 映射表 + Bitwarden 导入清单。

移植自 knowledge-desensitization/scripts/sanitize.py（M3 融合，语义不变）。

默认 dry-run，必须显式 --apply 才写盘。

安全设计
- 源目录只读，绝不原地修改。
- 原文只写入 vault/（映射表、Bitwarden 导入清单），不进 reports/，不打印。
- stdout 只输出计数。

策略（2026-10-18 用户决策）
- 宁可「过度脱敏」，配套恢复脚本一键填回误报，不牺牲召回。
- assets/ 与 .agent_history/ 默认不纳入（见 config/exclude.json 与 --with-assets）。

目录无关（用户 2026-10-18 需求）
- 真实源目录与镜像目录从对话里配置，落在 <data-root>/workspace.json；
  也可在命令行用 --root/--out 覆盖。数据根用 --data-root / $KM_DATA_ROOT 指定。

用法
    python -m awam_knowledge.km_sanitize --data-root <数据根> [--root <源目录>] [--out <镜像目录>]
                                          [--apply] [--with-assets] [--rules <path>]
                                          [--exclude-config <path>]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import shutil
import sys

from .sanitize_lib import (
    GENERATED_DIRS, GENERATED_NAMES, RUNTIME_DIRS,
    apply_hits, find_hits, get_salt, load_path_excludes, load_rules,
    path_excluded, read_text,
)
from . import km_paths as paths

# 顶层身份/运行时文件，按用户决策排除
EXCLUDED_ROOT_FILES = {
    "IDENTITY.md", "USER.md", "SOUL.md", "AGENT.md", "AGENTS.md", "HEARTBEAT.md",
}
ASSET_DIRS = {"assets"}


def classify(rel: str, note_dirs: set[str] | None = None,
             unknown_as_note: bool = True) -> str:
    """返回 'note' | 'asset' | 'skip'。通用性约定（2026-11-25 定稿：默认全量 + 白/黑名单）：

    note_dirs  提供且非空 → **白名单模式**：仅白名单目录按笔记处理，其余目录 skip
               （与 unknown_as_note 无关——白名单是显式收窄）。
    note_dirs  空/None   → **全量模式**（默认）：所有未命中黑名单
               （GENERATED_DIRS / RUNTIME_DIRS / EXCLUDED_ROOT_FILES）的目录都按笔记处理，
               不再默认 journals/pages（Logseq 布局只是普通知识目录之一）。
    unknown_as_note  兼容参数；全量模式下恒为 note，保留供旧调用方传 False 复现旧行为。
    黑名单：exclude.json 的 path glob（load_path_excludes）在 classify 之前生效。
    """
    nd = {d for d in (note_dirs or []) if d}
    parts = rel.replace("\\", "/").split("/")
    head = parts[0]
    if len(parts) == 1:
        if parts[0] in GENERATED_NAMES or parts[0] in EXCLUDED_ROOT_FILES:
            return "skip"
        return "note"
    if head in GENERATED_DIRS or head in RUNTIME_DIRS:
        return "skip"
    if head in nd:
        return "note"
    if head in ASSET_DIRS:
        return "asset"
    if nd:
        return "skip"
    return "note" if unknown_as_note else "skip"


def human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.0f}TB"


def context_label(line: str, hits, keep_idx: int) -> str:
    """把该行里其它命中的敏感片段挖掉，剩下的当作 Bitwarden 条目名提示。"""
    out, cursor = [], 0
    for i, h in enumerate(hits):
        if i == keep_idx:
            cursor = h.end
            continue
        if h.start < cursor:
            continue
        out.append(line[cursor:h.start])
        cursor = h.end
    out.append(line[cursor:])
    s = "".join(out).strip().strip("-*# >").strip()
    s = " ".join(s.split())
    return s[:70]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    paths.add_root_arg(ap)
    ap.add_argument("--root", help="源目录（只读）；缺省读 <data-root>/workspace.json 的 source")
    ap.add_argument("--out", help="sanitized 镜像根目录；缺省读 workspace.json 的 mirror")
    ap.add_argument("--rules", default=None, help="规则文件；缺省 <data-root>/config/rules.json")
    ap.add_argument("--exclude-config", default=None,
                    help="整文件/整目录排除 glob；缺省 <data-root>/config/exclude.json")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认 dry-run）")
    ap.add_argument("--with-assets", action="store_true",
                    help="连同 assets/ 附件一起复制（默认不纳入：只处理笔记）")
    ap.add_argument("--in-place", action="store_true",
                    help="就地脱敏：直接覆写 --root（源即工作副本）；与 --out 同路径时自动启用")
    ap.add_argument("--note-dirs", default=None,
                    help="笔记目录白名单（逗号分隔）；提供后仅这些目录按笔记处理。"
                         "缺省全量（所有目录按笔记）。可来自源配置 sanitize.note_dirs")
    ap.add_argument("--all-text", action="store_true",
                    help="（兼容旗标）把 assets/ 文本也当笔记处理；目录范围默认已全量")
    args = ap.parse_args(argv)

    root = paths.resolve_data_root(args.data_root)
    source_root = args.root or paths.workspace_source(root)
    if not source_root:
        ap.error("缺源目录：给 --root，或在 workspace.json 配置 source（先跑 wizard）")
    mirror_root = args.out or paths.workspace_mirror(root)

    note_dirs = None
    if args.note_dirs:
        note_dirs = {x.strip() for x in args.note_dirs.split(",") if x.strip()}
    in_place = args.in_place or (
        os.path.abspath(source_root) == os.path.abspath(mirror_root))
    rules_path = args.rules or os.path.join(root, "config", "rules.json")
    exclude_path = args.exclude_config or os.path.join(root, "config", "exclude.json")

    rules = load_rules(rules_path)
    excludes = load_path_excludes(exclude_path)
    salt = get_salt(os.path.join(root, "vault", "map", "salt.bin"))

    tokens: dict[str, dict] = {}
    per_rule = collections.Counter()
    per_file = collections.defaultdict(collections.Counter)
    n_note = n_asset = n_skip = n_bytes = 0
    applied: list[tuple] = []   # src, dst, kind, eol[, new_text]（元组长度按 kind 不同，保持原版）
    pending_creds: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames.sort()
        for fn in sorted(filenames):
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, source_root)
            if path_excluded(rel, excludes):
                n_skip += 1
                continue
            kind = classify(rel, note_dirs)
            if args.all_text and kind == "asset":
                kind = "note"
            if kind == "skip":
                n_skip += 1
                continue
            dst = src if in_place else os.path.join(mirror_root, rel)

            # 附件目录：默认不纳入（用户 2026-10-18 决策）。
            # 历史原因：assets/ 内存在 .txt/.json 等文本，扫描会命中高熵串。
            # 镜像是向量库输入，向量库本就排除 assets/，因此整目录排除最干净。
            if kind == "asset" and not args.with_assets:
                n_skip += 1
                continue

            text = read_text(src)
            if text is None:
                # 真二进制或非 UTF-8：原样保留/复制，避免损坏
                n_asset += 1
                n_bytes += os.path.getsize(src)
                if not in_place:
                    applied.append((src, dst, "asset", "", None))
                continue

            n_note += 1
            n_bytes += len(text.encode("utf-8"))
            # 迭代到不动点：只替换捕获组的规则（如 conn_string_password）会留下
            # 未替换残段；若残段本身也是敏感串，单遍扫描会漏掉。每轮把已替换的
            # token 视为保护区，因此循环必然收敛（token 不会再被匹配）。
            cur = text
            for _pass in range(4):
                hits = find_hits(cur, rules, salt)
                if not hits:
                    break
                for i, h in enumerate(hits):
                    per_rule[h.rule.name] += 1
                    per_file[rel][h.rule.name] += 1
                    entry = tokens.setdefault(h.token, {
                        "rule": h.rule.name,
                        "label": h.rule.label,
                        "credential": h.rule.credential,
                        "value": None,
                        "occurrences": [],
                    })
                    if entry["value"] is None:
                        entry["value"] = cur[h.start:h.end]
                    entry["occurrences"].append({"path": rel, "line": h.line})
                    if h.rule.credential:
                        lines = cur.splitlines()
                        line = lines[h.line - 1] if h.line - 1 < len(lines) else ""
                        line_hits = [x for x in hits if x.line == h.line]
                        idx = line_hits.index(h) if h in line_hits else 0
                        pending_creds.append({
                            "token": h.token,
                            "rule": h.rule.name,
                            "label": h.rule.label,
                            "path": rel,
                            "line": h.line,
                            "line_index": idx,
                            "context": context_label(line, line_hits, idx),
                            "value": cur[h.start:h.end],
                            "raw": line,
                        })
                cur = apply_hits(cur, hits)
            new_text = cur
            eol = "crlf" if "\r\n" in text else "lf"
            applied.append((src, dst, "note", eol, new_text))

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    stats = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "root": source_root,
        "out": mirror_root,
        "apply": args.apply,
        "in_place": in_place,
        "with_assets": args.with_assets,
        "exclude_patterns": excludes,
        "notes_processed": n_note,
        "assets_copied": n_asset,
        "skipped": n_skip,
        "counts_by_rule": dict(per_rule),
        "counts_by_file": {k: dict(v) for k, v in sorted(per_file.items())},
        "unique_tokens": len(tokens),
        "credential_occurrences": len(pending_creds),
    }

    if not args.apply:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        print("\n[dry-run] 未写盘。加 --apply 执行。")
        return 0

    # ---------- 写盘 ----------
    os.makedirs(os.path.join(root, "reports"), exist_ok=True)
    os.makedirs(os.path.join(root, "vault", "map"), exist_ok=True)

    for item in applied:
        src, dst = item[0], item[1]
        kind = item[2]
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if kind == "note":
            new_text = item[4]
            with open(dst, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)
        elif not in_place:
            shutil.copy2(src, dst)
        try:
            st = os.stat(src)
            os.utime(dst, (st.st_atime, st.st_mtime))
        except OSError:
            pass

    # 映射表（含原文，仅 vault）
    map_path = os.path.join(root, "vault", "map", "map.json")
    existing = {}
    if os.path.exists(map_path):
        with open(map_path, encoding="utf-8") as fh:
            existing = json.load(fh)
    merged = existing.get("tokens", {})
    for k, v in tokens.items():
        if k in merged:
            occ = merged[k].setdefault("occurrences", [])
            for o in v["occurrences"]:
                if o not in occ:
                    occ.append(o)
        else:
            merged[k] = v
    with open(map_path, "w", encoding="utf-8") as fh:
        json.dump({
            "updated_at": stats["generated_at"],
            "salt_sha256_prefix": hashlib.sha256(salt).hexdigest()[:12],
            "tokens": merged,
        }, fh, ensure_ascii=False, indent=2)

    # Bitwarden 导入清单（Bitwarden 官方 JSON 格式，含原文，仅 vault）
    items = []
    for i, c in enumerate(pending_creds, 1):
        name = c["context"] or f"{c['path']} · {c['label']}"
        items.append({
            "id": None,
            "organizationId": None,
            "folderId": None,
            "type": 1,
            "reprompt": 0,
            "name": f"{paths.ITEM_PREFIX}/{c['path'].replace(chr(92), '/')} · {name}"[:120],
            "notes": f"来源: {c['path']} 第 {c['line']} 行\n类型: {c['label']}\n占位: {c['token']}",
            "favorite": False,
            "login": {"username": None, "password": c["value"], "uris": []},
            "fields": [{"name": paths.TOKEN_FIELD, "value": c["token"], "type": 0}],
        })
    bw_path = os.path.join(root, "vault", "map", f"bw-import-{ts}.json")
    with open(bw_path, "w", encoding="utf-8") as fh:
        json.dump({"encrypted": False, "folders": [], "items": items},
                  fh, ensure_ascii=False, indent=2)

    # 审计报告（无原文）
    lines = [
        "# 脱敏执行报告", "",
        f"- 时间：{stats['generated_at']}",
        f"- 源目录：`{source_root}`（只读）",
        f"- 输出：`{mirror_root}`",
        f"- 笔记处理：{n_note}　附件复制：{n_asset}　跳过：{n_skip}（含路径排除）",
        f"- 唯一 token：{len(tokens)}　凭据命中：{len(pending_creds)}", "",
        "## 按类型", "", "| 规则 | 条数 |", "|---|---|",
    ]
    for k, v in per_rule.most_common():
        lines.append(f"| {k} | {v} |")
    lines += ["", "## 按文件", "", "| 文件 | 命中 | 明细 |", "|---|---|---|"]
    for f, c in sorted(per_file.items(), key=lambda x: -sum(x[1].values())):
        detail = "、".join(f"{k}×{v}" for k, v in c.most_common())
        lines.append(f"| `{f}` | {sum(c.values())} | {detail} |")
    lines += ["", "> 映射表与 Bitwarden 导入清单在 `vault/`，含原文，未在本报告中出现。"]
    md = os.path.join(root, "reports", f"sanitize-{ts}.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(root, "reports", f"sanitize-{ts}.json"),
              "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=2)

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print("\nwritten:")
    print("  mirror :", mirror_root)
    print("  map    :", map_path, "(含原文)")
    print("  bw     :", bw_path, "(含原文)")
    print("  report :", md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
