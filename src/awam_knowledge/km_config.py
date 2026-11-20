# SPDX-License-Identifier: MIT
"""km_config — 「知识库脱敏」的用户级配置读写（两级配置）。

移植自 knowledge-desensitization/scripts/km_config.py（M3 融合），
PyYAML 替换为零依赖 mini_yaml（输出风格与 safe_dump 一致，已交叉验证）。

- 配置两级：全局 + 每源；每源可覆盖全局。
- 用户配置根 = %USERPROFILE%\\.knowledge-manager-skills（或 $KM_CONFIG_HOME）。
- 配置文件**不进 Git**（该根目录本身不是仓库；其中可能含路径等环境信息）。
- 对话中更新 → 作为下次默认值（即写回这里）。

全局 <KM_CONFIG_HOME>/settings.yaml
    version: 1
    data_root: <数据根>              # 运行产物根（工作副本）；缺省留空，由 wizard 首次配置时确定
    secrets_root: <目录>             # 秘密材料根；缺省 <KM_CONFIG_HOME>/.km（永不外传）
    private_backup_dir: <目录>       # 还原材料的单独备份目录（技能执行落盘；建议离线）
    tmp_root: <目录>                 # 临时工作区根；缺省 <KM_CONFIG_HOME>/.tmp（用完即弃）
    defaults:
      sync:     { on_deleted: ask, exclude: [".git/**", ".obsidian/**", ...] }
      sanitize: { include_assets: false, note_dirs: [journals, pages] }
      backup:   { advise: true }

每源 <KM_CONFIG_HOME>/sources/<id>.yaml
    version: 1
    id: logseq-main
    name: logseq                 # 数据集名（工作副本工作目录名；缺省 = id）
    source: <真源目录>                 # 真源（只读）
    work_dir: <可选，覆盖工作副本位置；缺省 <data_root>/<id>>
    sync:     { on_deleted: ask, exclude: [...] }      # 可选，覆盖全局
    sanitize: { include_assets: false, note_dirs: [...] }
    backup:   { advise: true, private_backup_dir: <可选，覆盖全局> }
    state:    {}                 # 运行态由脚本维护；用户不必手改

安全：本模块只读写配置本身；配置中不含任何凭据明文。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys

from . import mini_yaml
from . import km_paths as paths

# ----------------------------------------------------------------------------
# 默认配置
# ----------------------------------------------------------------------------

DEFAULT_SETTINGS: dict = {
    "version": 1,
    "data_root": None,
    "secrets_root": None,
    "private_backup_dir": None,
    "tmp_root": None,
    "defaults": {
        "sync": {
            "on_deleted": "ask",          # ask | keep | remove
            "exclude": [
                ".git/**", ".git",
                ".obsidian/**", ".obsidian",
                "node_modules/**", "node_modules",
                "__pycache__/**", "__pycache__",
            ],
        },
        "sanitize": {
            "include_assets": False,
            "note_dirs": ["journals", "pages"],
            "all_text": False,
        },
        "backup": {
            "advise": True,
        },
    },
}


# ----------------------------------------------------------------------------
# YAML 读写（mini_yaml；与原 PyYAML 版语义一致：读不动 → {}）

def _load_yaml(path: str) -> dict:
    return mini_yaml.load_file(path)


def _save_yaml(path: str, data: dict) -> None:
    mini_yaml.save_file(path, data)


def deep_merge(base: dict, override: dict) -> dict:
    """把 override 深度合并进 base 的副本（dict 递归，其余直接覆盖）。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ----------------------------------------------------------------------------
# settings.yaml
# ----------------------------------------------------------------------------

def load_settings() -> dict:
    """读全局设置（缺失项以默认值补齐）。"""
    raw = _load_yaml(paths.settings_path())
    return deep_merge(DEFAULT_SETTINGS, raw)


def save_settings(settings: dict) -> None:
    _save_yaml(paths.settings_path(), settings)


def update_settings(patch: dict) -> dict:
    """读→深合并 patch→写回，返回合并后的完整设置。"""
    merged = deep_merge(load_settings(), patch)
    save_settings(merged)
    return merged


# ----------------------------------------------------------------------------
# sources/<id>.yaml
# ----------------------------------------------------------------------------

def list_source_ids() -> list[str]:
    d = paths.sources_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith((".yaml", ".yml")):
            out.append(fn.rsplit(".", 1)[0])
    return out


def load_source(sid: str) -> dict:
    """读某源配置原始内容（不含全局默认合并）。"""
    raw = _load_yaml(paths.source_cfg_path(sid))
    if not raw:
        return {}
    return raw


def resolve_source(sid: str) -> dict:
    """返回该源的「生效配置」：全局 defaults 与每源覆盖合并后的视图。

    结构：
      { id, name, source, work_dir, sync:{...}, sanitize:{...}, backup:{...}, state:{} }
    """
    settings = load_settings()
    raw = load_source(sid)
    if not raw:
        raise SystemExit(f"找不到源配置：{paths.source_cfg_path(sid)}\n先执行："
                         f"python -m awam_knowledge.wizard --source <真源目录>")
    base = {
        "version": 1,
        "id": sid,
        "name": raw.get("name") or sid,
        "source": raw.get("source"),
        "work_dir": raw.get("work_dir"),
        "data_root": raw.get("data_root") or settings.get("data_root"),
        "secrets_root": (raw.get("secrets_root") or settings.get("secrets_root")
                         or paths.default_secrets_root()),
        "private_backup_dir": (raw.get("backup", {}) or {}).get("private_backup_dir")
                              or settings.get("private_backup_dir"),
        "sync": settings["defaults"]["sync"],
        "sanitize": settings["defaults"]["sanitize"],
        "backup": settings["defaults"]["backup"],
        "state": raw.get("state", {}) or {},
    }
    for section in ("sync", "sanitize", "backup"):
        if isinstance(raw.get(section), dict):
            base[section] = deep_merge(base[section], raw[section])
    return base


def save_source(sid: str, cfg: dict) -> None:
    _save_yaml(paths.source_cfg_path(sid), cfg)


def effective_secrets_root() -> str:
    """秘密材料根的**生效值**：settings.secrets_root 优先，否则默认 <KM_CONFIG_HOME>/.km。"""
    root = load_settings().get("secrets_root") or paths.default_secrets_root()
    return os.path.abspath(root)


def effective_tmp_root() -> str:
    """临时工作区根的**生效值**：settings.tmp_root 优先，否则默认 <KM_CONFIG_HOME>/.tmp。

    目录不存在时按需创建（用完即弃；永不入 Git）。
    """
    root = load_settings().get("tmp_root") or paths.default_tmp_root()
    return os.path.abspath(root)


def global_config_dir() -> str:
    """用户级「全局配置」目录（与 settings.yaml 同级，不进 Git、不随包发布）。

    用于存放全局的 rules.json / whitelist.json / exclude.json / literals.json，
    作为所有源的默认；每源 `<秘密材料根>/<id>/config/` 可覆盖。
    """
    return os.path.join(paths.km_config_home(), "config")


def list_sources() -> list[dict]:
    """列出所有已保存的源（供入口询问「要处理哪个」）。读不动的源会被跳过。"""
    out: list[dict] = []
    for sid in list_source_ids():
        try:
            c = resolve_source(sid)
        except SystemExit:
            continue
        st = c.get("state") or {}
        out.append({
            "id": sid,
            "name": c.get("name"),
            "source": c.get("source"),
            "work_dir": c.get("work_dir"),
            "data_root": c.get("data_root"),
            "private_backup_dir": c.get("private_backup_dir"),
            "last_sync": st.get("last_sync"),
        })
    return out


def write_source_state(sid: str, updates: dict) -> None:
    """更新某源的 state 段（运行态；用户不必手改）。"""
    raw = load_source(sid) or {"version": 1, "id": sid}
    st = raw.setdefault("state", {})
    st.update(updates)
    save_source(sid, raw)


# ----------------------------------------------------------------------------
# 由「输入源路径」推导 id / 数据集名 / 数据根子目录
# ----------------------------------------------------------------------------

_SLUG_RX = re.compile(r"[^A-Za-z0-9._\-]+")


def _slug(s: str) -> str:
    return _SLUG_RX.sub("-", s).strip("-") or "source"


def suggest_subdir(source: str) -> str:
    """按输入源路径推荐数据根下的子目录名：取末两级路径拼接。

    例：…\\logseq\\main        →  logseq-main
        …\\Knowledge\\logseq   →  Knowledge-logseq
    """
    p = os.path.abspath(source).rstrip("\\/")
    parts = [x for x in re.split(r"[\\/]+", p) if x]
    tail = parts[-2:] if len(parts) >= 2 else parts
    return _slug("-".join(tail))


def suggest_id(source: str) -> str:
    """推荐源 id（= 数据根子目录名的安全 slug）。"""
    return _slug(suggest_subdir(source))


def suggest_name(source: str) -> str:
    """推荐数据集名（默认取源目录叶子名）。"""
    leaf = os.path.basename(os.path.abspath(source).rstrip("\\/")) or "notes"
    return _slug(leaf)


# ----------------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------------

def selftest() -> int:
    import tempfile
    checks: list[tuple] = []

    def ck(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))

    old_home = os.environ.get("KM_CONFIG_HOME")
    tmp = tempfile.mkdtemp(prefix="kmcfg-")
    os.environ["KM_CONFIG_HOME"] = tmp
    try:
        # deep_merge
        base = {"a": {"x": 1, "y": 2}, "b": 1}
        ck("deep_merge 深合并", deep_merge(base, {"a": {"y": 3}}) == {"a": {"x": 1, "y": 3}, "b": 1})
        ck("deep_merge 不改原对象", base == {"a": {"x": 1, "y": 2}, "b": 1})

        # 默认 settings
        s = load_settings()
        ck("默认 on_deleted=ask", s["defaults"]["sync"]["on_deleted"] == "ask")
        ck("默认 note_dirs", s["defaults"]["sanitize"]["note_dirs"] == ["journals", "pages"])
        ck("默认 include_assets=False", s["defaults"]["sanitize"]["include_assets"] is False)

        # update_settings 写回 + 覆盖
        update_settings({"data_root": os.path.join(tmp, "d"), "defaults": {"sync": {"on_deleted": "keep"}}})
        s2 = load_settings()
        ck("update_settings 落盘", s2["data_root"] == os.path.join(tmp, "d"))
        ck("update_settings 局部覆盖", s2["defaults"]["sync"]["on_deleted"] == "keep")
        ck("update_settings 保留其余默认", s2["defaults"]["sanitize"]["note_dirs"] == ["journals", "pages"])
        ck("settings.yaml 文件存在", os.path.isfile(paths.settings_path()))

        # suggest_*
        src = os.path.join(tmp, "Knowledge", "logseq")
        ck("suggest_id", suggest_id(src) == "Knowledge-logseq")
        ck("suggest_name 叶子名", suggest_name(src) == "logseq")
        ck("suggest_id 非法字符 slug", suggest_id(os.path.join(tmp, "a b", "c d!")) == "a-b-c-d")

        # save/resolve source
        sid = "test-src"
        save_source(sid, {
            "version": 1, "id": sid, "name": "测试",
            "source": os.path.join(tmp, "raw"),
            "sync": {"on_deleted": "remove"},
            "state": {"last_sync": "2026-11-20T00:00:00"},
        })
        ids = list_source_ids()
        ck("list_source_ids", sid in ids)
        c = resolve_source(sid)
        ck("resolve_source name", c["name"] == "测试")
        ck("resolve_source 每源覆盖 sync", c["sync"]["on_deleted"] == "remove")
        ck("resolve_source 继承全局 exclude", ".git/**" in c["sync"]["exclude"])
        ck("resolve_source 继承 sanitize", c["sanitize"]["note_dirs"] == ["journals", "pages"])
        ck("resolve_source data_root 来自 settings", c["data_root"] == os.path.join(tmp, "d"))
        ck("resolve_source state", c["state"]["last_sync"] == "2026-11-20T00:00:00")
        ck("resolve_source work_dir 缺省", c["work_dir"] is None)

        # write_source_state
        write_source_state(sid, {"last_sync": "2026-11-20T12:00:00"})
        c2 = resolve_source(sid)
        ck("write_source_state 更新", c2["state"]["last_sync"] == "2026-11-20T12:00:00")

        # list_sources
        ls = list_sources()
        ck("list_sources 含 id", any(x["id"] == sid for x in ls))
        row = [x for x in ls if x["id"] == sid][0]
        ck("list_sources last_sync", row["last_sync"] == "2026-11-20T12:00:00")

        # 默认秘密根锚定用户配置根
        ck("default_secrets_root 锚定配置根", paths.default_secrets_root() == os.path.join(tmp, ".km"))
        ck("effective_secrets_root 未配置时同默认", effective_secrets_root() == os.path.join(tmp, ".km"))

        # resolve 不存在的源 → SystemExit
        try:
            resolve_source("no-such")
            ck("缺源报 SystemExit", False)
        except SystemExit:
            ck("缺源报 SystemExit", True)
    finally:
        if old_home is None:
            os.environ.pop("KM_CONFIG_HOME", None)
        else:
            os.environ["KM_CONFIG_HOME"] = old_home

    failed = [n for n, ok in checks if not ok]
    print(json.dumps({"selftest": True, "checks": len(checks), "pass": not failed,
                      "failed": failed}, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用户级配置读写（两级配置）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true", help="列出已保存的源")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.list:
        rows = list_sources()
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
