# AI生成
"""wizard — 入口引导：为**一个输入源**逐步建立配置与目录（技能执行的起点）。

移植自 knowledge-desensitization/scripts/guide.py（M3 融合）。差异点：
- 技能模板目录改为随包分发的内置配置（km_paths.builtin_config_dir()）；
- 默认秘密材料根 / 临时区锚定用户配置根（<KM_CONFIG_HOME>/.km、/.tmp）；
- YAML 输出用 mini_yaml（零依赖，不再用 PyYAML）。

用户 2026-10-22 定稿
- 技能输入只是一个**源目录**（"请帮我处理 X 路径"），其余逐步引导。
- 真源**只读**；工作副本 = 脱敏「就地」对象（可配）；秘密材料与工作副本分开。
- 用户配置根 = %USERPROFILE%\\.knowledge-manager-skills（或 $KM_CONFIG_HOME）；
  本模块在其中落一份 sources/<id>.yaml，并把 data_root 等写入 settings.yaml
  （对话中给的更新 → 作为下次默认值）。
- **数据根无默认值**：由本模块在首次配置时确定（--data-root 或已在 settings 里）。

建出的结构
    <KM_CONFIG_HOME>/
        settings.yaml                全局默认（data_root / private_backup_dir / …）
        sources/<id>.yaml            本源配置（真源 / 工作副本 / 覆盖项）
        config/{rules,whitelist,exclude}.json   全局配置层（模板种子；改完即生效）
    <data_root>/<id>/                工作副本（就地脱敏；建议上云）
    <secrets_root>/<id>/             秘密材料与运行态（永不外传）
                                     缺省 <KM_CONFIG_HOME>/.km/<id>（可用 --secrets-root 覆盖）
        .desensitization-root  workspace.json  .gitignore
        config/literals.example.json  # 示例（无明文）；真实字面量见下方「配置分层」
        config/literals.json          # 可选：仅本源覆盖时才有（缺省不创建）
        vault/{keys,map,preview}  reports/

配置分层（生效顺序，后者覆盖前者）
    1. 内置模板 <包>/config/rules.json（随包升级）
    2. 全局 <KM_CONFIG_HOME>/config/{rules,whitelist,exclude,literals}.json  ← 默认层，改完即生效
    3. 每源 <秘密材料根>/<id>/config/…                                      ← 仅本源覆盖
    本模块把内置模板**种子到全局目录**（缺则建）；**不再**往每源拷快照，
    以免快照遮蔽以后对全局配置的修改（改了不生效）。

用法
    python -m awam_knowledge.wizard --source <真源目录> [--data-root <数据根>] \
        [--id <源id>] [--name <数据集名>] [--work-dir <工作副本>] \
        [--private-backup-dir <单独备份目录>] [--on-deleted ask|keep|remove] \
        [--note-dirs journals,pages] [--all-text] [--dry-run] [--force]

缺省 dry-run（只打印将建的结构与配置）；--force 覆盖已存在的源配置。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import km_config
from . import km_paths as paths
from . import mini_yaml

# 秘密材料目录的隐私护栏（写入 <secret_dir>/.gitignore）
GITIGNORE = """\
# === 隐私护栏（知识库脱敏技能自动生成）===
# 本目录是「秘密材料与运行态」根：任何含明文/映射/密钥的内容都不得进入 Git。

# 本地秘密区：映射表、salt、密钥材料、预览（含原文片段）
vault/

# 用户在本机填写的字面量清单（含真实片段）与其备份
config/literals.json
config/literals.json.bak-*
config/*.local.json

# 可能被导出的名单/残留表
mapping*.json
residual*.json
bw-import-*.json
bw-items.json
restore-log.jsonl

# 运行期派生
reports/
logs/
__pycache__/
*.pyc

# OS / 编辑器
.DS_Store
Thumbs.db
"""


def copy_if_absent(src: str, dst: str) -> bool:
    if os.path.exists(dst) or not os.path.exists(src):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="awam_knowledge.wizard",
        description="为「一个输入源」逐步建立配置与目录（技能执行的起点）")
    ap.add_argument("--source", required=True, help="真源目录（只读）")
    ap.add_argument("--id", help="源 id；缺省由源路径推导（如 logseq-main）")
    ap.add_argument("--name", help="数据集名；缺省取源目录叶子名")
    ap.add_argument("--data-root", help="数据根；缺省读 settings.yaml（无默认值）")
    ap.add_argument("--work-dir", help="工作副本目录；缺省 <data_root>/<id>")
    ap.add_argument("--private-backup-dir", help="还原材料单独备份目录（建议离线）")
    ap.add_argument("--on-deleted", choices=["ask", "keep", "remove"], default="ask",
                    help="源中删除的文件如何处理（默认 ask：批量询问用户）")
    ap.add_argument("--note-dirs", default=None, help="笔记目录名（逗号分隔）；缺省 journals,pages")
    ap.add_argument("--all-text", action="store_true", help="把所有文本文件都当笔记（非 Logseq 源）")
    ap.add_argument("--exclude", default=None, help="同步额外排除 glob（逗号分隔）")
    ap.add_argument("--secrets-root", help="秘密材料根；缺省 <KM_CONFIG_HOME>/.km（永不外传）")
    ap.add_argument("--tmp-root", help="临时工作区根；缺省 <KM_CONFIG_HOME>/.tmp")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的源配置")
    args = ap.parse_args(argv)

    src = os.path.abspath(args.source)
    if not os.path.isdir(src):
        print(f"真源目录不存在：{src}", file=sys.stderr)
        return 2

    sid = args.id or km_config.suggest_id(src)
    name = args.name or km_config.suggest_name(src)
    settings = km_config.load_settings()
    data_root = args.data_root or settings.get("data_root")
    private_backup = args.private_backup_dir or settings.get("private_backup_dir")
    tmp_root = args.tmp_root or settings.get("tmp_root")   # 显式指定（命令行 / 已存 settings）
    tmp_effective = tmp_root or paths.default_tmp_root()   # 缺省：<KM_CONFIG_HOME>/.tmp
    secrets_root = (args.secrets_root or settings.get("secrets_root")
                    or paths.default_secrets_root())       # 缺省：<KM_CONFIG_HOME>/.km

    if not data_root:
        print("缺数据根（无默认值）。请给 --data-root <目录>（会在首次配置时确定）。",
              file=sys.stderr)
        return 2
    data_root = os.path.abspath(data_root)

    work = paths.work_dir(data_root, sid, args.work_dir)
    sec = paths.secret_dir(sid, secrets_root)
    cfg_path = paths.source_cfg_path(sid)

    if os.path.exists(cfg_path) and not args.force:
        print(f"源配置已存在：{cfg_path}\n如需重建请加 --force。", file=sys.stderr)
        return 2

    note_dirs = [x.strip() for x in args.note_dirs.split(",") if x.strip()] \
        if args.note_dirs else settings["defaults"]["sanitize"]["note_dirs"]
    extra_exclude = [x.strip() for x in args.exclude.split(",") if x.strip()] if args.exclude else []

    src_cfg = {
        "version": 1,
        "id": sid,
        "name": name,
        "source": src,
        "work_dir": work if args.work_dir else None,
        "sync": {"on_deleted": args.on_deleted},
        "sanitize": {"note_dirs": note_dirs, "all_text": bool(args.all_text)},
        "backup": {},
        "state": {},
    }
    if extra_exclude:
        src_cfg["sync"]["exclude"] = extra_exclude
    if args.private_backup_dir:
        src_cfg["backup"]["private_backup_dir"] = os.path.abspath(args.private_backup_dir)

    # 去掉空值，保持配置简洁（空 `backup: {}` 会在这一步被剔除）
    src_cfg = {k: v for k, v in src_cfg.items() if v not in (None, {}, [])}

    if args.dry_run:
        print("=== [dry-run] 将建立 ===")
        print(f"用户配置根 : {paths.km_config_home()}")
        print(f"源 id      : {sid}    数据集名: {name}")
        print(f"真源(只读) : {src}")
        print(f"工作副本   : {work}")
        print(f"秘密材料   : {sec}")
        print(f"单独备份   : {private_backup or '（未指定；建议设置）'}")
        print(f"临时区     : {tmp_effective}"
              + ("" if tmp_root else "（缺省：<KM_CONFIG_HOME> 下 .tmp）"))
        print(f"settings   : data_root={data_root}")
        print(f"源配置     : {cfg_path}")
        print("\n源配置内容：")
        print(mini_yaml.dumps(src_cfg))
        print("[dry-run] 未写盘。去掉 --dry-run 执行。")
        return 0

    # ---- 写盘 ----
    os.makedirs(work, exist_ok=True)
    os.makedirs(tmp_effective, exist_ok=True)   # 默认即在用户配置根下「产生」临时工作区
    for sub in ("config", "reports",
                os.path.join("vault", "keys"), os.path.join("vault", "map"),
                os.path.join("vault", "preview")):
        os.makedirs(os.path.join(sec, sub), exist_ok=True)

    with open(os.path.join(sec, paths.MARKER), "w", encoding="utf-8") as fh:
        fh.write("此目录是「知识库脱敏」秘密材料与运行态根（永不外传）。\n")

    ws_path = paths.workspace_path(sec)
    with open(ws_path, "w", encoding="utf-8") as fh:
        json.dump({"source": src, "mirror": work, "name": name}, fh,
                  ensure_ascii=False, indent=2)

    gi = os.path.join(sec, ".gitignore")
    with open(gi, "w", encoding="utf-8") as fh:
        fh.write(GITIGNORE)

    # 配置分层（B）：**全局配置目录 = 默认的家**；每源目录 = 覆盖。
    # 不做「每源种子快照」，否则快照会遮蔽全局配置的后续修改（改了不生效）。
    generated = []
    gcfg = km_config.global_config_dir()
    os.makedirs(gcfg, exist_ok=True)
    builtin_cfg = paths.builtin_config_dir()
    for name_, skill_name in (("rules.json", "rules.json"),
                              ("whitelist.json", "whitelist.example.json"),
                              ("exclude.json", "exclude.example.json")):
        if copy_if_absent(os.path.join(builtin_cfg, skill_name),
                          os.path.join(gcfg, name_)):
            generated.append(f"全局/{name_}")
    # 每源只放「示例字面量」做参考（无明文）；真实字面量写在全局或本源覆盖目录
    copy_if_absent(os.path.join(builtin_cfg, "literals.example.json"),
                   os.path.join(sec, "config", "literals.example.json"))

    km_config.save_source(sid, src_cfg)

    patch = {"data_root": data_root}
    if private_backup:
        patch["private_backup_dir"] = private_backup
    if args.secrets_root:
        patch["secrets_root"] = args.secrets_root
    if args.tmp_root:
        patch["tmp_root"] = args.tmp_root
    km_config.update_settings(patch)

    print(f"已配置源：{sid}（{name}）")
    print(f"  真源(只读) : {src}")
    print(f"  工作副本   : {work}")
    print(f"  秘密材料   : {sec}")
    print(f"  单独备份   : {private_backup or '（未指定，建议 --private-backup-dir 设一个离线目录）'}")
    print(f"  临时工作区 : {tmp_effective}"
          + ("" if tmp_root else "（缺省：<KM_CONFIG_HOME> 下 .tmp，未入 Git）"))
    print(f"  生成配置   : {', '.join(generated) if generated else '（均已存在）'}")
    print(f"  源配置     : {cfg_path}")
    print("  提示：真实字面量写在「全局配置目录」的 config/literals.json"
          "（所有源共用，改完即生效）；")
    print(f"        只想本源覆盖时，写 {os.path.join(sec, 'config', 'literals.json')}"
          "；示例见 config/literals.example.json。")
    print("\n下一步（一条命令跑完整流程）：")
    print(f'  python -m awam_knowledge.km_pipeline --id "{sid}" --apply')
    print("  或分步：km_sync → km_sanitize → km_materials → km_build_index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
