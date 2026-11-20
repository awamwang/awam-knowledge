# SPDX-License-Identifier: MIT
"""km_paths — 目录无关解析：用户配置根 / 数据根 / 工作副本 / 秘密材料目录。

移植自 knowledge-desensitization/scripts/paths.py（M3 融合），差异点：
- 默认秘密材料根 / 临时区从「技能目录」改为锚定**用户配置根**：
  `<KM_CONFIG_HOME>/.km`、`<KM_CONFIG_HOME>/.tmp`。原版锚定技能目录在
  wheel / PyInstaller onefile 形态下会漂移（临时解包目录），锚定用户配置根后
  默认值跨安装形态稳定；显式配置（settings.yaml / 每源）优先级不变。
- 内置模板目录（rules.json 等）随包分发：builtin_config_dir()。

用户级配置根（跨源复用，不进 Git）：
    KM_CONFIG_HOME（env）或 %USERPROFILE%\\.knowledge-manager-skills
    ├─ settings.yaml           全局默认
    └─ sources/<id>.yaml       每个真源一份（可覆盖全局）

一个「源」= 一个只读真源 + 一份工作副本 + 一份秘密材料目录：
- 真源：只读，不破坏、不上传。
- 工作副本：脱敏「就地」对象；输入与输出（已脱敏）是同一份。
- 秘密材料：salt / map / literals 等，与工作副本物理分开——
  工作副本可上云，秘密材料绝不可。

秘密材料根解析优先级（**有默认值 = <KM_CONFIG_HOME>/.km**）
    1) 命令行 --secrets-root <dir>
    2) 每源配置 sources/<id>.yaml 的 secrets_root
    3) 用户级 settings.yaml 的 secrets_root
    4) 默认 <KM_CONFIG_HOME>/.km

数据根解析优先级（**无默认值**）
    1) 命令行 --data-root <dir>
    2) 环境变量 KM_DATA_ROOT（旧变量 DESENS_DATA_ROOT 亦兼容）
    3) 用户级 settings.yaml 的 data_root（由 wizard 首次配置时写入）

安全：本模块只做路径计算与读取配置，绝不读取或打印任何凭据内容。
"""
from __future__ import annotations

import json
import os

MARKER = ".desensitization-root"

# 秘密材料相对数据根的隐藏目录（工作副本的同级兄弟，绝不随工作副本上传）
SECRETS_DIRNAME = ".km"

# 临时工作区：处理「其他数据」与脱敏过程临时文件（用完即弃；永不入 Git）
TMP_DIRNAME = ".tmp"

# Bitwarden 条目约定（统一命名，避免各脚本写死）：
#   name   形如 "<ITEM_PREFIX>/<来源> · <摘要>"
#   fields 里以 TOKEN_FIELD 记录 token，供 backfill 建立 token→条目 映射
ITEM_PREFIX = "notes"
TOKEN_FIELD = "notes-token"
VAULT_REF = "见保险库："  # 回填写进镜像的引用前缀
#   引用形如 "<前缀><条目名> [vault:<label>:<hash8>]"：方括号内是唯一主键
#   （map.json 的 token 去掉 REDACTED 前缀）→ 恢复不依赖条目名唯一性；
#   主键不复用 [REDACTED:...]，backfill 重跑不会二次替换（幂等）。


def builtin_config_dir() -> str:
    """内置模板目录（rules.json / *.example.json，随包分发的基线配置）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


# ----------------------------------------------------------------------------
# 用户配置根
# ----------------------------------------------------------------------------

def km_config_home() -> str:
    """用户级配置根：<KM_CONFIG_HOME> 或 %USERPROFILE%\\.knowledge-manager-skills。"""
    env = os.environ.get("KM_CONFIG_HOME")
    if env:
        return os.path.abspath(env)
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, ".knowledge-manager-skills")


def default_tmp_root() -> str:
    """临时工作区的默认根：<KM_CONFIG_HOME>/.tmp。"""
    return os.path.join(km_config_home(), TMP_DIRNAME)


def default_secrets_root() -> str:
    """秘密材料的默认根：<KM_CONFIG_HOME>/.km（永不外传；可被配置覆盖）。"""
    return os.path.join(km_config_home(), SECRETS_DIRNAME)


def settings_path() -> str:
    return os.path.join(km_config_home(), "settings.yaml")


def sources_dir() -> str:
    return os.path.join(km_config_home(), "sources")


def source_cfg_path(sid: str) -> str:
    return os.path.join(sources_dir(), f"{sid}.yaml")


# ----------------------------------------------------------------------------
# 数据根解析
# ----------------------------------------------------------------------------

def resolve_data_root(explicit: str | None = None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    for env_name in ("KM_DATA_ROOT", "DESENS_DATA_ROOT"):
        env = os.environ.get(env_name)
        if env:
            return os.path.abspath(env)
    # 新架构：数据根来自用户级 settings.yaml（无默认值）
    from . import km_config
    root = km_config.load_settings().get("data_root")
    if root:
        return os.path.abspath(root)
    raise SystemExit(
        "未指定数据根（data_root）。三选一：\n"
        "  1) 命令加 --data-root <目录>\n"
        "  2) 设置环境变量 KM_DATA_ROOT=<目录>\n"
        "  3) 先执行：python -m awam_knowledge.wizard --source <真源目录>\n"
        "（数据根是存放工作副本与秘密材料的工作区，不是本包安装目录）")


def add_root_arg(parser) -> None:
    """给子命令加统一的 --data-root 参数。"""
    parser.add_argument(
        "--data-root", metavar="DIR",
        help="数据根；缺省读 $KM_DATA_ROOT（或 $DESENS_DATA_ROOT）或 settings.yaml")


# ----------------------------------------------------------------------------
# 每个源的三个关键目录
# ----------------------------------------------------------------------------

def work_dir(data_root: str, sid: str, override: str | None = None) -> str:
    """工作副本目录：显式 override 优先，否则 <data_root>/<id>。"""
    if override:
        return os.path.abspath(override)
    return os.path.join(data_root, sid)


def secret_dir(sid: str, secrets_root: str | None = None) -> str:
    """某源的秘密材料与运行态目录：<秘密材料根>/<id>（永不外传）。"""
    root = os.path.abspath(secrets_root or default_secrets_root())
    return os.path.join(root, sid)


# ----------------------------------------------------------------------------
# 标准子目录（相对秘密材料目录）
# ----------------------------------------------------------------------------

def config_dir(root: str) -> str:
    return os.path.join(root, "config")


def vault_dir(root: str) -> str:
    return os.path.join(root, "vault")


def map_dir(root: str) -> str:
    return os.path.join(root, "vault", "map")


def reports_dir(root: str) -> str:
    return os.path.join(root, "reports")


# ---- workspace.json（秘密材料目录内的源指针） ----

def workspace_path(root: str) -> str:
    return os.path.join(root, "workspace.json")


def load_workspace(root: str) -> dict:
    p = workspace_path(root)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def workspace_source(root: str) -> str | None:
    """真源路径：读 workspace.json 的 source（**无默认值**，缺失返回 None）。"""
    return load_workspace(root).get("source")


def workspace_mirror(root: str) -> str:
    """工作副本路径：**只认 workspace.json 的 mirror（无默认）**。

    缺 mirror 即报错——调用方应显式传 --mirror / --out，或先跑 wizard。
    """
    m = load_workspace(root).get("mirror")
    if not m:
        raise SystemExit(
            f"{workspace_path(root)} 里没有 mirror（工作副本路径）。\n"
            "请显式指定 --mirror / --out <工作副本目录>，或先执行："
            "python -m awam_knowledge.wizard --source <真源目录>")
    return m
