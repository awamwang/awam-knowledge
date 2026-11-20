# AI生成
"""km_sync — 真源 → 工作副本 的**增量同步**（技能触发时执行）。

移植自 knowledge-desensitization/scripts/sync.py（M3 融合，语义不变）。

用户 2026-10-22 定稿
- 同步在「技能触发时」执行；真源**只读**，绝不改写。
- **注意增量**：只拷「新增 / 改动」的文件；内容未变的跳过（靠 size+mtime 快判，
  必要时回退 sha256 精判），避免每次全量重拷。
- 源中**删除**的文件：**批量询问用户**。默认只列出、不删除（on_deleted=ask）；
  用户确认后可 keep（保留）或 remove（软移到秘密材料目录，可恢复）。

用法
    python -m awam_knowledge.km_sync --id <源id> [--apply] [--deleted keep|remove] [--json]
    python -m awam_knowledge.km_sync --source <真源目录> [--data-root <根>] [--apply] ...

缺省 dry-run（只打印计划）；--apply 才真正拷贝。

安全
- 只输出「相对路径 + 状态 + 计数」，不打印任何文件内容。
- 删除一律**软移**（移到 <秘密材料>/superseded/<ts>/），绝不物理删除。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys

from . import km_config
from . import km_paths as paths
from .sanitize_lib import path_excluded


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_source(root: str, excludes: list[str]):
    """生成 (rel_posix, abs_path)；排除 glob 命中的文件与目录。"""
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace("\\", "/")
        keep = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if not path_excluded(rel, excludes):
                keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if path_excluded(rel, excludes):
                continue
            yield rel, os.path.join(dirpath, fn)


def load_manifest(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    paths.add_root_arg(ap)
    ap.add_argument("--id", help="源 id（读 <配置根>/sources/<id>.yaml）")
    ap.add_argument("--source", help="真源目录（临时使用；不读配置，需配合 --data-root）")
    ap.add_argument("--apply", action="store_true", help="真正拷贝（默认 dry-run）")
    ap.add_argument("--deleted", choices=["ask", "keep", "remove"], default=None,
                    help="源中删除的文件如何处理：ask(默认,只列不删) / keep / remove(软移)")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args(argv)

    # ---- 解析源与目标 ----
    if args.id:
        cfg = km_config.resolve_source(args.id)
        src = cfg["source"]
        data_root = args.data_root or cfg["data_root"]
        if not data_root:
            print("缺数据根：给 --data-root，或在 settings.yaml / 源配置里设 data_root",
                  file=sys.stderr)
            return 2
        data_root = os.path.abspath(data_root)
        work = paths.work_dir(data_root, args.id, cfg.get("work_dir"))
        sec = paths.secret_dir(args.id, cfg.get("secrets_root"))
        excludes = cfg["sync"]["exclude"]
        on_deleted = args.deleted or cfg["sync"].get("on_deleted", "ask")
    elif args.source:
        src = os.path.abspath(args.source)
        if not args.data_root:
            print("缺 --data-root（临时源模式需显式指定数据根）", file=sys.stderr)
            return 2
        sid = km_config.suggest_id(src)
        data_root = os.path.abspath(args.data_root)
        work = paths.work_dir(data_root, sid)
        sec = paths.secret_dir(sid, km_config.effective_secrets_root())
        excludes = km_config.load_settings()["defaults"]["sync"]["exclude"]
        on_deleted = args.deleted or "ask"
    else:
        ap.error("需给 --id 或 --source")

    if not src or not os.path.isdir(src):
        print(f"真源不存在：{src}", file=sys.stderr)
        return 2

    manifest_path = os.path.join(sec, "sync-manifest.json")
    prev = load_manifest(manifest_path).get("files", {})

    stats = {"new": 0, "changed": 0, "unchanged": 0, "deleted": 0, "copied": 0}
    plan: list[tuple[str, str, str]] = []      # (rel, action, abs_src)
    src_files: dict[str, str] = {}

    for rel, abs_src in iter_source(src, excludes):
        src_files[rel] = abs_src
        dst = os.path.join(work, rel.replace("/", os.sep))
        st = os.stat(abs_src)
        p = prev.get(rel)
        if p and p.get("size") == st.st_size and p.get("mtime_ns") == st.st_mtime_ns:
            stats["unchanged"] += 1
            continue
        if p and p.get("sha256"):
            cur = sha256(abs_src)
            if cur == p["sha256"]:
                stats["unchanged"] += 1
                continue
            action = "changed"
        else:
            action = "new"
        if action == "new" and os.path.exists(dst) and p is None:
            # 首次同步但工作副本已有同名文件：比内容，相等算 unchanged
            if os.path.getsize(dst) == st.st_size and sha256(dst) == sha256(abs_src):
                action = "unchanged"
        stats[action] += 1
        if action != "unchanged":
            plan.append((rel, action, abs_src))

    # ---- 删除项：源中已不存在但上次同步过 ----
    deleted = sorted(set(prev.keys()) - set(src_files.keys()))
    stats["deleted"] = len(deleted)

    # ---- 输出计划 ----
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    result = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "id": args.id or km_config.suggest_id(src),
        "source": src, "work_dir": work, "secret_dir": sec,
        "apply": args.apply, "on_deleted": on_deleted,
        "counts": {"new": stats["new"], "changed": stats["changed"],
                   "unchanged": stats["unchanged"], "deleted": stats["deleted"]},
        "deleted_files": deleted,
    }

    if not args.apply:
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"真源   : {src}")
            print(f"工作副本: {work}")
            print(f"增量计划：新增 {stats['new']}　改动 {stats['changed']}"
                  f"　未变 {stats['unchanged']}　源中已删除 {stats['deleted']}")
            for rel, action, _ in plan[:200]:
                print(f"  [{'＋' if action=='new' else '↻'}] {rel}")
            if len(plan) > 200:
                print(f"  … 其余 {len(plan)-200} 项省略")
            if deleted:
                print(f"\n⚠️ 源中已删除 {len(deleted)} 个文件（工作副本中仍在）：")
                for rel in deleted[:200]:
                    print(f"  [-] {rel}")
                if len(deleted) > 200:
                    print(f"  … 其余 {len(deleted)-200} 项省略")
                print("  处理方式：默认保留（keep）。要一并移除请加 --deleted remove"
                      "（软移到秘密材料目录，可恢复）。")
            print("\n[dry-run] 未写盘。加 --apply 执行。")
        return 0

    # ---- 执行拷贝 ----
    os.makedirs(work, exist_ok=True)
    os.makedirs(sec, exist_ok=True)
    for rel, action, abs_src in plan:
        dst = os.path.join(work, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(abs_src, dst)
        stats["copied"] += 1

    # ---- 删除项处理 ----
    removed_moved = 0
    if deleted and on_deleted == "remove":
        sup = os.path.join(sec, "superseded", ts)
        for rel in deleted:
            p = os.path.join(work, rel.replace("/", os.sep))
            if os.path.exists(p):
                tgt = os.path.join(sup, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(tgt), exist_ok=True)
                shutil.move(p, tgt)
                removed_moved += 1

    # ---- 写新清单（记录源侧 size/mtime/sha256） ----
    files = {}
    for rel, abs_src in src_files.items():
        st = os.stat(abs_src)
        files[rel] = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "sha256": sha256(abs_src),
        }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump({"updated_at": result["generated_at"], "source": src,
                   "work_dir": work, "files": files}, fh,
                  ensure_ascii=False, indent=2)

    result["copied"] = stats["copied"]
    result["deleted_moved"] = removed_moved
    if args.id:
        km_config.write_source_state(args.id, {
            "last_sync": result["generated_at"],
            "last_sync_counts": result["counts"],
        })

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"已同步 {stats['copied']} 个文件 → {work}")
        print(f"（新增 {stats['new']} / 改动 {stats['changed']} / 未变 {stats['unchanged']}）")
        if deleted:
            verb = "软移" if on_deleted == "remove" else "保留"
            print(f"源中已删除 {len(deleted)} 个文件：{verb}"
                  + (f"（软移到 {os.path.join(sec, 'superseded', ts)}）"
                     if on_deleted == "remove" else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
