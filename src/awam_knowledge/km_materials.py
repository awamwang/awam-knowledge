# AI生成
"""km_materials — 把**还原材料**落盘到「单独备份目录」，并输出备份建议。

移植自 knowledge-desensitization/scripts/materials.py（M3 融合，语义不变）。

用户 2026-10-22 定稿
- 还原所需材料（salt / map / bw-items / literals）**必须**落到一个
  「单独备份目录」（可配），由**技能执行**落盘；强烈建议该目录**私密离线**。
- 工作副本本身**不由技能上云/Git**，只给**建议**（→ 网盘 / 私有 Git）。
- **C+D 暂不做增量**：每次覆盖写，保证材料与当前 map/salt 一致。

产出结构（<dest> = 单独备份目录，<id> = 源 id）
    <dest>/<id>/
        materials/           ← 离线密材（C+D）：泄露即全盘明文
            vault/map/salt.bin
            vault/map/map.json
            vault/map/bw-items.json            （存在时）
            vault/map/restore-log.jsonl        （存在时）
            config/literals.json (+ .bak-*)    （存在时；含明文）
            global/literals.json (+ .bak-*)    （全局配置层的字面量，存在时；含明文）
            bitwarden/<export>.json            （--bitwarden-export 时）
        meta/                ← 非密辅助：rules/whitelist/exclude
            global/          ← 全局配置层的 rules/whitelist/exclude（配置分层的「默认层」）
        MANIFEST.json        ← 每个文件的 role/bytes/sha256
        RESTORE.md           ← 恢复步骤 + 备份建议

用法
    python -m awam_knowledge.km_materials --id <源id> [--apply] [--verify] [--json] \
        [--dest <单独备份目录>] [--bitwarden-export <文件>]

缺省 dry-run；--apply 才写盘。

安全
- 只输出「路径 + 字节 + sha256」，绝不打印任何文件内容（可能是明文）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import shutil
import sys

from . import km_config
from . import km_paths as paths


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_materials(root: str) -> list[str]:
    """C+D 离线密材（相对秘密材料目录的 rel）。"""
    rels = [
        os.path.join("vault", "map", "salt.bin"),
        os.path.join("vault", "map", "map.json"),
        os.path.join("vault", "map", "bw-items.json"),
        os.path.join("vault", "map", "restore-log.jsonl"),
        os.path.join("config", "literals.json"),
    ]
    rels += [os.path.relpath(p, root)
             for p in sorted(glob.glob(os.path.join(root, "config", "literals.json.bak-*")))]
    return [r for r in rels if os.path.isfile(os.path.join(root, r))]


def collect_meta(root: str) -> list[str]:
    rels = [
        os.path.join("config", "rules.json"),
        os.path.join("config", "whitelist.json"),
        os.path.join("config", "exclude.json"),
    ]
    return [r for r in rels if os.path.isfile(os.path.join(root, r))]


def collect_global(gcfg: str) -> list[tuple[str, str, str]]:
    """全局配置目录（配置分层的「默认层」）→ (role, dest_rel, abs_src)。

    配置分层后，生效规则 = 技能模板 → 全局 → 每源；因此备份必须**同时**收全局层，
    否则换机恢复时规则/字面量会缺一层，还原不出等价环境。
    含明文的 literals 仍落 materials/（密材）层，其余落 meta/。
    """
    out: list[tuple[str, str, str]] = []
    if not gcfg or not os.path.isdir(gcfg):
        return out
    for name in ("rules.json", "whitelist.json", "exclude.json"):
        p = os.path.join(gcfg, name)
        if os.path.isfile(p):
            out.append(("meta", os.path.join("meta", "global", name), p))
    lp = os.path.join(gcfg, "literals.json")
    if os.path.isfile(lp):
        out.append(("materials",
                    os.path.join("materials", "global", "literals.json"), lp))
    for p in sorted(glob.glob(os.path.join(gcfg, "literals.json.bak-*"))):
        out.append(("materials",
                    os.path.join("materials", "global", os.path.basename(p)), p))
    return out


def backup_advice(cfg: dict, work: str, dest: str) -> str:
    """给出「工作副本 → 网盘/私有 Git；密材 → 私密离线」的建议文案（不含内容）。"""
    return (
        "备份建议（技能只给建议，不代执行上传/入 Git）：\n"
        f"  ① 工作副本（已脱敏，可直接喂向量库）：\n"
        f"       {work}\n"
        "       → 建议上传 网盘 / 私有 Git 仓库（`.km/` 秘密目录不在其中，放心上传）。\n"
        f"  ② 还原材料（salt/map/密钥/literals）：\n"
        f"       {dest}\n"
        "       → 强烈建议放入**私密离线**介质（U 盘×2 / 密码管理器 / 保险柜）；\n"
        "          禁止上云、禁止入任何 Git 仓库。丢失即永久不可逆。\n"
        "  ③ Bitwarden 本体：请在你自己的终端做**加密导出**后另行保管。"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    paths.add_root_arg(ap)
    ap.add_argument("--id", required=True, help="源 id")
    ap.add_argument("--dest", help="单独备份目录；缺省读源配置 private_backup_dir / settings.yaml")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认 dry-run）")
    ap.add_argument("--verify", action="store_true", help="拷后重算哈希核对")
    ap.add_argument("--bitwarden-export", metavar="FILE",
                    help="你在终端生成的 Bitwarden 加密导出，一并落到 materials/bitwarden/")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = km_config.resolve_source(args.id)
    data_root = args.data_root or cfg["data_root"]
    if not data_root:
        print("缺数据根：给 --data-root，或在 settings.yaml / 源配置设 data_root",
              file=sys.stderr)
        return 2
    data_root = os.path.abspath(data_root)
    sec = paths.secret_dir(args.id, cfg.get("secrets_root"))
    work = paths.work_dir(data_root, args.id, cfg.get("work_dir"))
    dest_base = os.path.abspath(args.dest) if args.dest else cfg.get("private_backup_dir")
    if not dest_base:
        print("缺「单独备份目录」：给 --dest，或在 settings.yaml 设 private_backup_dir，"
              "或源配置 backup.private_backup_dir", file=sys.stderr)
        return 2
    if not os.path.isdir(sec):
        print(f"秘密材料目录不存在：{sec}（先执行 wizard / 跑一次 sanitize）", file=sys.stderr)
        return 2

    outdir = os.path.join(dest_base, args.id)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    items: list[tuple[str, str, str]] = []     # (role, rel_in_dest, abs_src)
    for rel in collect_materials(sec):
        items.append(("materials", os.path.join("materials", rel), os.path.join(sec, rel)))
    for rel in collect_meta(sec):
        items.append(("meta", os.path.join("meta", rel), os.path.join(sec, rel)))
    items += collect_global(km_config.global_config_dir())
    if args.bitwarden_export:
        p = os.path.abspath(args.bitwarden_export)
        if os.path.isfile(p):
            items.append(("materials", os.path.join("materials", "bitwarden",
                                                    os.path.basename(p)), p))
        else:
            print(f"⚠️ --bitwarden-export 文件不存在：{p}", file=sys.stderr)

    if not items:
        print("没有可落盘的还原材料（vault/ 是否已初始化？）", file=sys.stderr)
        return 2

    if not args.apply:
        print(f"源 id     : {args.id}")
        print(f"秘密材料  : {sec}")
        print(f"备份目标  : {outdir}")
        print("将落盘：")
        for role, rel, src in items:
            print(f"  [{role}] {rel}  ({os.path.getsize(src)} B)")
        print("\n[dry-run] 未写盘。加 --apply 执行。")
        print()
        print(backup_advice(cfg, work, outdir))
        return 0

    manifest = {"created": dt.datetime.now().isoformat(timespec="seconds"),
                "id": args.id, "secret_dir": sec, "dest": outdir, "files": []}
    stats = {"written": 0, "unchanged": 0}
    total = 0
    for role, rel, src in items:
        dst = os.path.join(outdir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        h = sha256(src)
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src) \
                and sha256(dst) == h:
            stats["unchanged"] += 1
            status = "unchanged"
        else:
            shutil.copy2(src, dst)
            stats["written"] += 1
            status = "written"
        size = os.path.getsize(src)
        total += size
        ok = True
        if args.verify:
            ok = (sha256(dst) == h)
        manifest["files"].append({"role": role, "path": rel, "bytes": size,
                                  "sha256": h, "status": status, "verified": ok})
        flag = "" if not args.verify else (" ✓" if ok else " ✗")
        print(f"  [{role}] {rel}  ({size} B)  {status}{flag}")

    with open(os.path.join(outdir, "MANIFEST.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    restore_md = f"""# 还原材料备份 / 恢复说明

- 生成时间：{manifest['created']}
- 源 id：`{args.id}`
- 秘密材料目录：`{sec}`
- 工作副本：`{work}`（**已脱敏**，可上云；本文件不在此目录内）
- 文件数：{len(manifest['files'])}，合计 {total} 字节
  （本次写入 {stats['written']} / 未变 {stats['unchanged']}）

## 目录含义

| 目录 | 级别 | 内容 | 去路 |
|---|---|---|---|
| `materials/` | C+D | `vault/map/{{salt.bin,map.json,bw-items.json}}`、（`literals.json`） | **私密离线**：U 盘×2 / 密码管理器；**禁止上云/入 Git** |
| `meta/` | — | 配置（rules/whitelist/exclude） | 非密，可留存；**不与 materials 混放** |
| `MANIFEST.json` | — | 每个文件的 role/sha256 | 校验用 |

## 恢复步骤（本备份 + 脱敏工作副本 → 原始知识库）

前置：`recover.py`（在 knowledge-desensitization 仓）需要 `map.json`
（本备份 materials 层）+ 脱敏工作副本。

1. 把工作副本拷回：`<work>`（即已脱敏镜像）。
2. 把 `materials/vault/`、`meta/config/` 覆盖回**秘密材料目录** `<sec>/`（保持相对路径）。
3. 校验：`python scripts/recover.py --data-root "{sec}" --verify`
4. 重建：`python scripts/recover.py --data-root "{sec}" --apply`
   （`recover.py` 用 `map.json` + salt 还原，**不依赖 Bitwarden**）

## 安全

- `materials/` 含秘密材料：salt 丢失 / map 泄露均会造成不可逆风险；务必离线多份。
- 含 `literals.json` 时还含明文片段，介质本身必须安全。

## Bitwarden 本体（D 级，不是文件）

在你自己的终端执行加密导出（需主密码，Agent 不代做）：
```
bw export --format encrypted_json --password "<导出口令>" --output bw-export.json
```
生成后用 `--bitwarden-export <文件>` 重跑本命令，落到 `materials/bitwarden/`。
**不要**用 `--format json`（明文导出）。
"""
    with open(os.path.join(outdir, "RESTORE.md"), "w", encoding="utf-8") as fh:
        fh.write(restore_md)

    bad = [f for f in manifest["files"] if f.get("verified") is False]
    print(f"\n已落盘 {len(manifest['files'])} 个文件 → {outdir}"
          f"（写入 {stats['written']} / 未变 {stats['unchanged']}，合计 {total} 字节）")
    if args.verify:
        print("校验：", "全部通过 ✓" if not bad else f"有 {len(bad)} 个不匹配 ✗")
    print()
    print(backup_advice(cfg, work, outdir))
    km_config.write_source_state(args.id, {"last_materials": manifest["created"]})
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
