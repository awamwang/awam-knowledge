# SPDX-License-Identifier: MIT
"""M3 脱敏管道沙箱端到端自检：wizard → sync → sanitize → materials → build_index → pipeline。

全链在临时 KM_CONFIG_HOME 沙箱内运行（不碰真实用户配置 / 真源 / 秘密材料）。
运行：python tests/km_check.py
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
    entry, km_build_index, km_config, km_materials, km_paths, km_pipeline,
    km_sanitize, km_sync, wizard,
)

# 命中内置规则 llm_api_key（sk-[A-Za-z0-9_\-]{20,}）
SECRET = "sk-TestSecretAb12Cd34Ef56"
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
    except SystemExit as e:  # argparse error 等
        code = e.code
        return code if isinstance(code, int) else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    tmp = tempfile.mkdtemp(prefix="km-check-")
    home = os.path.join(tmp, "home")
    src = os.path.join(tmp, "real-source")
    data_root = os.path.join(tmp, "data")
    backup = os.path.join(tmp, "backup")
    os.makedirs(home)

    # ---- 沙箱隔离：配置根 / 数据根环境变量 ----
    old_env = {k: os.environ.get(k) for k in
               ("KM_CONFIG_HOME", "KM_DATA_ROOT", "DESENS_DATA_ROOT")}
    os.environ["KM_CONFIG_HOME"] = home
    for k in ("KM_DATA_ROOT", "DESENS_DATA_ROOT"):
        os.environ.pop(k, None)

    try:
        # ---- 假真源（只读）----
        w(os.path.join(src, "journals", "2026-01-05.md"),
          f"- 今天调试 [[我的页面]]\n- 密钥 {SECRET} 记在这里\n")
        w(os.path.join(src, "pages", "我的页面.md"),
          "title:: 我的知识页\n- 一些普通内容\n")
        w(os.path.join(src, "docs", "excluded.md"), "- 会被 exclude 排除的内容\n")
        os.makedirs(os.path.join(src, "assets"), exist_ok=True)
        with open(os.path.join(src, "assets", "logo.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nfake-binary")

        # 1) wizard 建源
        rc = run_step(wizard.main, [
            "--source", src, "--data-root", data_root,
            "--private-backup-dir", backup])
        ck("wizard rc=0", rc == 0)
        sid = km_config.suggest_id(src)
        work = km_paths.work_dir(data_root, sid)
        sec = km_paths.secret_dir(sid)
        ck("源配置落盘", os.path.isfile(km_paths.source_cfg_path(sid)))
        ck("settings 落盘", os.path.isfile(km_paths.settings_path()))
        ck("全局模板种子", os.path.isfile(os.path.join(home, "config", "rules.json")))
        ck("workspace.json", os.path.isfile(km_paths.workspace_path(sec)))
        ck("护栏 .gitignore", os.path.isfile(os.path.join(sec, ".gitignore")))

        # 2) entry --list（分发入口可列出）
        rc = run_step(entry.main, ["--list"])
        ck("entry --list rc=0", rc == 0)

        # 3) pipeline dry-run（全链只打印计划）
        rc = run_step(km_pipeline.main, ["--id", sid])
        ck("pipeline dry-run rc=0", rc == 0)

        # 4) pipeline --apply（全链执行）
        rc = run_step(km_pipeline.main, ["--id", sid, "--apply"])
        ck("pipeline apply rc=0", rc == 0)

        # sync：工作副本已建并拷贝
        ck("sync 拷贝笔记", os.path.isfile(os.path.join(work, "journals", "2026-01-05.md")))
        ck("sync 拷贝附件", os.path.isfile(os.path.join(work, "assets", "logo.png")))
        ck("manifest 落盘", os.path.isfile(os.path.join(sec, "sync-manifest.json")))

        # sanitize：密钥已替换为稳定 token，原文只进 vault
        j = r(os.path.join(work, "journals", "2026-01-05.md"))
        ck("密钥已脱敏", SECRET not in j)
        ck("token 形如 [REDACTED:...]", bool(TOKEN_RX.search(j)))
        ck("普通内容保留", "[[我的页面]]" in j)
        map_path = os.path.join(sec, "vault", "map", "map.json")
        ck("map.json 落盘", os.path.isfile(map_path))
        mapj = json.loads(r(map_path))
        ck("map 含原文映射", any(v.get("value") == SECRET
                                 for v in mapj["tokens"].values()))
        ck("salt 落盘", os.path.isfile(os.path.join(sec, "vault", "map", "salt.bin")))
        ck("bw-import 落盘", any(fn.startswith("bw-import-")
                                 for fn in os.listdir(os.path.join(sec, "vault", "map"))))
        ck("审计报告落盘", os.path.isdir(os.path.join(sec, "reports")))

        # materials：单独备份目录
        man_path = os.path.join(backup, sid, "MANIFEST.json")
        ck("MANIFEST 落盘", os.path.isfile(man_path))
        man = json.loads(r(man_path))
        ck("备份含 salt.bin", any(f["path"].endswith("salt.bin") for f in man["files"]))
        ck("备份含 map.json", any(f["path"].endswith("map.json") for f in man["files"]))
        ck("RESTORE.md 落盘", os.path.isfile(os.path.join(backup, sid, "RESTORE.md")))

        # build_index：旁路 Index（不改写 .md）
        idx_path = os.path.join(work, "index.json")
        ck("index.json 落盘", os.path.isfile(idx_path))
        idx = json.loads(r(idx_path))
        ck("索引 2 篇", idx["count"] == 2)
        ck("索引类型齐全", {d["type"] for d in idx["docs"]} == {"journal", "page"})
        ck("日记日期识别", any(d.get("date") == "2026-01-05" for d in idx["docs"]))

        # 5) 幂等：重跑 apply，token / 文本 / map 稳定
        tokens_before = sorted(mapj["tokens"].keys())
        j_before = j
        rc = run_step(km_pipeline.main, ["--id", sid, "--apply"])
        ck("重跑 apply rc=0", rc == 0)
        mapj2 = json.loads(r(map_path))
        ck("map token 不漂移", sorted(mapj2["tokens"].keys()) == tokens_before)
        ck("脱敏文本稳定", r(os.path.join(work, "journals", "2026-01-05.md")) == j_before)

        # 6) 增量：真源新增文件 → 下次同步补拷并纳入脱敏/索引
        w(os.path.join(src, "journals", "2026-01-06.md"),
          f"- 新的一天，另一个密钥 {SECRET}x\n")
        rc = run_step(km_pipeline.main, ["--id", sid, "--apply"])
        ck("增量重跑 rc=0", rc == 0)
        new_j = os.path.join(work, "journals", "2026-01-06.md")
        ck("增量文件已拷", os.path.isfile(new_j))
        ck("增量文件已脱敏", SECRET + "x" not in r(new_j))
        idx2 = json.loads(r(idx_path))
        ck("索引随增量更新", idx2["count"] == 3)

        # 7) entry --source → wizard 分发（dry-run + 已存在源需 --force，不写盘）
        rc = run_step(entry.main, [
            "--source", src, "--data-root", data_root,
            "--private-backup-dir", backup, "--dry-run", "--force"])
        ck("entry→wizard dry-run rc=0", rc == 0)
        rc = run_step(entry.main, ["--source", src])  # 已有源且无 --force → 拒绝
        ck("wizard 拒绝重建已有源", rc == 2)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in _checks if not ok]
    print(json.dumps({"selftest": True, "checks": len(_checks),
                      "pass": not failed, "failed": failed},
                     ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
