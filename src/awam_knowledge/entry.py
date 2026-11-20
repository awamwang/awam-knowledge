# AI生成
"""entry — 技能统一入口（"请帮我处理 X 路径" 从这里开始）。

移植自 knowledge-desensitization/scripts/entry.py（M3 融合，进程内分发）。

用户 2026-10-22 定稿
- 技能输入可以只是一个**源目录**；也可以只给**源 id**（处理已配置过的源）。
- 调用时**没给目标**时：
    · 配置里已保存过源 → **列出它们，询问要处理哪一个**；
    · 从未处理过 → **要求用户输入**（源目录 + 数据根）。
- **数据根无默认值**：由首次配置（wizard）在对话中确定。

用法
    python -m awam_knowledge.entry                                  # 选择 / 引导
    python -m awam_knowledge.entry --list                           # 仅列出已保存的源
    python -m awam_knowledge.entry --source <目录> [--data-root <根>] [--dry-run] ...   # 新增/配置一个源
    python -m awam_knowledge.entry --id <源id> [--apply]                          # 处理该源（全流程）

本入口只做「选择 + 分发」：新增源 → wizard；处理源 → km_pipeline。
"""
from __future__ import annotations

import argparse

from . import km_config
from . import km_pipeline
from . import wizard


def print_sources() -> None:
    srcs = km_config.list_sources()
    if not srcs:
        print("尚无已保存的源。请提供：")
        print("  · 源目录（要处理的知识库，只读）")
        print("  · 数据根（存放工作副本与秘密材料；无默认值）")
        print("\n示例：")
        print('  python -m awam_knowledge.entry --source "<真源目录>" --data-root "<数据根>"')
        return
    print(f"已保存 {len(srcs)} 个源，要处理哪一个？（用 --id <源id> 指定）\n")
    print(f"{'id':<20} {'数据集名':<12} {'真源':<44} 上次同步")
    print("-" * 100)
    for s in srcs:
        print(f"{s['id']:<20} {(s.get('name') or ''):<12} {(s.get('source') or ''):<44} "
              f"{s.get('last_sync') or '（未同步）'}")
    print("\n示例：python -m awam_knowledge.entry --id <源id> --apply")
    print("新增：python -m awam_knowledge.entry --source <目录> --data-root <数据根>")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="awam_knowledge.entry", add_help=True,
        description="知识库脱敏统一入口：选择 + 分发（新增源 → wizard；处理源 → km_pipeline）")
    ap.add_argument("--list", action="store_true", help="仅列出已保存的源")
    ap.add_argument("--id", help="源 id（处理该源的全流程）")
    ap.add_argument("--source", help="源目录（新增/配置一个源）")
    # 传给 wizard（新增源）
    ap.add_argument("--data-root")
    ap.add_argument("--name")
    ap.add_argument("--work-dir")
    ap.add_argument("--private-backup-dir")
    ap.add_argument("--on-deleted", choices=["ask", "keep", "remove"])
    ap.add_argument("--note-dirs")
    ap.add_argument("--all-text", action="store_true")
    ap.add_argument("--exclude")
    ap.add_argument("--secrets-root")
    ap.add_argument("--tmp-root")
    ap.add_argument("--force", action="store_true")
    # 传给 km_pipeline（处理源）
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--deleted", choices=["ask", "keep", "remove"])
    ap.add_argument("--with-assets", action="store_true")
    ap.add_argument("--skip-sync", action="store_true")
    ap.add_argument("--skip-sanitize", action="store_true")
    ap.add_argument("--skip-materials", action="store_true")
    ap.add_argument("--skip-index", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        print_sources()
        return 0

    if args.source:
        wargv = ["--source", args.source]
        for flag in ("data-root", "name", "work-dir", "private-backup-dir",
                     "on-deleted", "note-dirs", "exclude", "secrets-root", "tmp-root"):
            v = getattr(args, flag.replace("-", "_"))
            if v:
                wargv += [f"--{flag}", v]
        if args.id:
            wargv += ["--id", args.id]
        if args.all_text:
            wargv += ["--all-text"]
        if args.dry_run:
            wargv += ["--dry-run"]
        if args.force:
            wargv += ["--force"]
        return wizard.main(wargv)

    if args.id:
        pargv = ["--id", args.id]
        if args.data_root:
            pargv += ["--data-root", args.data_root]
        if args.apply:
            pargv += ["--apply"]
        if args.deleted:
            pargv += ["--deleted", args.deleted]
        if args.with_assets:
            pargv += ["--with-assets"]
        for flag in ("skip-sync", "skip-sanitize", "skip-materials", "skip-index"):
            if getattr(args, flag.replace("-", "_")):
                pargv += [f"--{flag}"]
        return km_pipeline.main(pargv)

    # 没给目标：列出已保存的源（询问），或要求输入
    print_sources()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
