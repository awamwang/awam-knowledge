# SPDX-License-Identifier: MIT
"""索引根约定（RAG 侧）。逐字移植自 Node code/index-root.js。

安全要点：默认可索引输入必须是**脱敏工作副本**（KM_DATA_ROOT/<源 id>）；真源（KM_RAW_ROOTS）
在未显式 --allow-raw-source 时**直接拒绝**，防误把未脱敏原文喂给召回/评测。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_SOURCE_ID = "logseq-main"

_KEYVAL = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*(.+?)\s*$")
_COMMENT = re.compile(r"^\s*#.*$")


def normalize_path(p: Optional[str]) -> str:
    s = str(p or "").strip()
    s = s.replace("\\", "/")
    s = re.sub(r"/+", "/", s)
    s = re.sub(r"/$", "", s)
    return s.lower()


def raw_roots() -> List[str]:
    return [x for x in (normalize_path(s) for s in os.environ.get("KM_RAW_ROOTS", "").split(";")) if x]


def settings_path() -> str:
    if os.environ.get("KM_SETTINGS"):
        return os.environ["KM_SETTINGS"]
    return str(Path.home() / ".knowledge-manager-skills" / "settings.yaml")


def expand_home(p: Optional[str]) -> str:
    s = str(p or "").strip()
    if not s:
        return ""
    home = str(Path.home())
    # 用函数替换，避免 home 里的反斜杠被当成 re 替换模板的转义（C:\\Users 的 \\U）。
    s = re.sub(r"^~(?=$|[\\/])", lambda _m: home, s)
    s = re.sub(r"%USERPROFILE%", lambda _m: home, s, flags=re.IGNORECASE)
    return s


def read_settings(file: Optional[str] = None) -> Dict[str, str]:
    """极简 YAML：只认顶层 key: value（去注释、去引号），够读 data_root / source_id。"""
    try:
        text = Path(file or settings_path()).read_text(encoding="utf-8")
    except OSError:
        return {}
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = _COMMENT.sub("", raw)
        m = _KEYVAL.match(line)
        if not m:
            continue
        v = m.group(2).strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1].strip()
        if v in ("", "~", "null"):
            continue
        out[m.group(1)] = v
    return out


def is_forbidden_raw_source(root: str) -> bool:
    n = normalize_path(root)
    return any(n == f or n.startswith(f + "/") for f in raw_roots())


def resolve_default_root() -> Optional[str]:
    settings = read_settings()
    data_root = (os.environ.get("KM_DATA_ROOT") or os.environ.get("DESENS_DATA_ROOT")
                 or expand_home(settings.get("data_root")))
    if not data_root:
        return None
    sid = os.environ.get("KM_SOURCE_ID") or settings.get("source_id") or settings.get("default_source") or DEFAULT_SOURCE_ID
    return os.path.join(expand_home(data_root), str(sid)).replace("\\", "/")


def validate_index_root(explicit: Optional[str], allow_raw: bool = False,
                        label: str = "ROOT") -> Dict[str, Any]:
    allow = allow_raw or ("--allow-raw-source" in __import__("sys").argv)
    root = (str(explicit).strip() if (explicit and str(explicit).strip()) else resolve_default_root())
    if not root:
        return {"ok": False, "reason": "no_root",
                "error": "未指定索引根：请传 --root，或设置 KM_DATA_ROOT（+ 可选 KM_SOURCE_ID）。"
                         "默认应指向脱敏工作副本，不要指向真源。"}
    if is_forbidden_raw_source(root) and not allow:
        return {"ok": False, "reason": "raw_source", "root": root,
                "error": f"禁止使用真源作为索引/评测根（{label}）：{root}"
                         "\n请改用脱敏工作副本（KM_DATA_ROOT/<源 id>）；确需读真源时显式加 --allow-raw-source。"}
    return {"ok": True, "root": root}
