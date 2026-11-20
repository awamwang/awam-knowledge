# AI生成
"""km_pipeline — 一个源的全流程编排（技能触发时的主入口）。

移植自 knowledge-desensitization/scripts/pipeline.py（M3 融合）。差异点：
- **进程内编排**：直接调用各步骤模块的 main(argv)，不再 subprocess
  （包形态下脚本路径不稳定，进程内调用跨 wheel / PyInstaller 形态一致）。

用户 2026-10-22 定稿流程（顺序固定）
    1) km_sync        真源 → 工作副本（**增量**同步；源中删除的文件批量询问）
    2) km_sanitize    工作副本**就地**脱敏（输入=输出；稳定 token、幂等、可逆）
    3) km_materials   还原材料落盘到「单独备份目录」（技能执行；建议离线）
    4) km_build_index 旁路 Index（缺省传 --dry-run；--apply 时真正写 Index；默认不改写 .md）
    5) 输出**备份建议**（工作副本 → 网盘/私有 Git；材料 → 私密离线；技能不代上传）
    6) 汇总：任一步非零退出 → 逐条点名失败步骤与各步落盘情况，不谎报「全流程完成」

用法
    python -m awam_knowledge.km_pipeline --id <源id> [--apply] \
        [--deleted keep|remove] [--skip-sync] [--skip-sanitize] [--skip-materials] [--skip-index]

缺省 dry-run（每一步都只打印计划）；--apply 才真正执行。

安全
- 每一步都不打印文件内容；秘密材料只在 .km/ 与单独备份目录之间流动。
- 真源全程只读；删除一律软移。
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback

from . import km_build_index, km_materials, km_sanitize, km_sync
from . import km_config
from . import km_paths as paths


def _q(s: str) -> str:
    return f'"{s}"' if (" " in s or "\\" in s) else s


def run(module, module_name: str, args: list[str]) -> int:
    """进程内执行一步；SystemExit / 异常折算为返回码（对应原 subprocess 语义）。"""
    print(f"\n$ python -m awam_knowledge.{module_name} "
          f"{' '.join(_q(a) for a in args)}")
    try:
        return module.main(args)
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    except BaseException:
        traceback.print_exc()
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="awam_knowledge.km_pipeline",
        description="一个源的全流程编排：sync → sanitize → materials → build_index")
    ap.add_argument("--id", required=True, help="源 id")
    ap.add_argument("--data-root", help="数据根；缺省读源配置 / settings.yaml")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    ap.add_argument("--dry-run", action="store_true",
                    help="显式 dry-run（默认即 dry-run；仅为入口一致性）")
    ap.add_argument("--deleted", choices=["ask", "keep", "remove"], default=None,
                    help="源中删除的文件如何处理（传给 sync）；缺省用源配置")
    ap.add_argument("--with-assets", action="store_true", help="脱敏时连附件一起处理")
    ap.add_argument("--skip-sync", action="store_true")
    ap.add_argument("--skip-sanitize", action="store_true")
    ap.add_argument("--skip-materials", action="store_true")
    ap.add_argument("--skip-index", action="store_true")
    args = ap.parse_args(argv)

    cfg = km_config.resolve_source(args.id)
    data_root = args.data_root or cfg["data_root"]
    if not data_root:
        print("缺数据根：先在 settings.yaml / 源配置设 data_root，或跑 wizard。",
              file=sys.stderr)
        return 2
    data_root = os.path.abspath(data_root)
    work = paths.work_dir(data_root, args.id, cfg.get("work_dir"))
    sec = paths.secret_dir(args.id, cfg.get("secrets_root"))

    print("=" * 66)
    print(f"源 id   : {args.id}（{cfg.get('name')}）  模式：{'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"真源    : {cfg['source']}")
    print(f"工作副本: {work}")
    print(f"秘密材料: {sec}")
    print(f"单独备份: {cfg.get('private_backup_dir') or '（未指定）'}")
    print("=" * 66)

    rc = 0
    failed: list[tuple[str, int]] = []
    common = (["--apply"] if args.apply else [])

    def step(label: str, module, module_name: str, script_args: list[str]) -> None:
        nonlocal rc
        code = run(module, module_name, script_args)
        if code:
            failed.append((label, code))
        rc |= code

    # 1) 同步（增量）
    if not args.skip_sync:
        a = ["--id", args.id, "--data-root", data_root] + common
        if args.deleted:
            a += ["--deleted", args.deleted]
        step("1 同步 sync", km_sync, "km_sync", a)

    # 2) 就地脱敏
    if not args.skip_sanitize:
        a = ["--data-root", sec, "--root", work, "--out", work, "--in-place"] + common
        nd = cfg["sanitize"].get("note_dirs")
        if nd:
            a += ["--note-dirs", ",".join(nd)]
        if cfg["sanitize"].get("all_text"):
            a += ["--all-text"]
        if args.with_assets or cfg["sanitize"].get("include_assets"):
            a += ["--with-assets"]
        step("2 脱敏 sanitize", km_sanitize, "km_sanitize", a)

    # 3) 还原材料落盘
    if not args.skip_materials:
        step("3 材料落盘 materials", km_materials, "km_materials",
             ["--id", args.id, "--data-root", data_root] + common)

    # 4) 旁路 Index（build_index 用 --dry-run 表示不写；无 --apply 旗标）
    if not args.skip_index:
        a = ["--id", args.id, "--data-root", data_root]
        if not args.apply:
            a += ["--dry-run"]
        step("4 索引 build_index", km_build_index, "km_build_index", a)

    print("\n" + "=" * 66)
    if not args.apply:
        print("[dry-run] 以上均为计划。加 --apply 真正执行。")
    elif failed:
        print(f"未完成：{len(failed)} 步失败，其余步骤已落盘。"
              "请按各步自身输出单独补救后重跑对应步骤。")
        for label, code in failed:
            print(f"  ✗ {label}  (rc={code})")
    else:
        print("全流程完成。请按上面的「备份建议」处置工作副本与还原材料。")
    print("=" * 66)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
