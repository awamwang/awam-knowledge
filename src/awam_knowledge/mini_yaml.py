# SPDX-License-Identifier: MIT
"""mini_yaml — 零依赖 YAML 子集（load/dump），替代 PyYAML（M3 融合：去第三方依赖）。

支持范围（覆盖 km_config 的 settings.yaml / sources/<id>.yaml 全部真实形态）：
- 块映射 / 块序列（含 PyYAML safe_dump 的「序列不缩进」风格与常见缩进风格）
- 标量：纯文本、单引号（'' 转义）、双引号（反斜杠转义）、null/~/空、true/false、整数、浮点
- 流式集合：[a, b]、{k: v}（含嵌套与引号元素）
- 注释（# 整行与其后缀）、文档结束无要求
不支持：锚点/别名、多文档、块标量 |、>、标签、复杂键。遇到不支持结构抛 MiniYAMLError。

dump 对齐 yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
的输出风格：块式、两空格缩进、序列不缩进、空集合流式（{} / []）、
「像数字/布尔/null/日期的字符串」加引号、含 ": "/" #"/换行/指示符开头的字符串加引号。

用法：
    from awam_knowledge import mini_yaml
    obj = mini_yaml.loads(text)
    text = mini_yaml.dumps(obj)
    python -m awam_knowledge.mini_yaml --selftest
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional, Union

__all__ = ["MiniYAMLError", "loads", "dumps", "load_file", "save_file", "selftest", "main"]


class MiniYAMLError(ValueError):
    """YAML 子集解析失败。"""


# ----------------------------------------------------------------------------
# 词法辅助
# ----------------------------------------------------------------------------

_INT_RX = re.compile(r"^[-+]?\d+$")
_FLOAT_RX = re.compile(r"^[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def _plain_is_bool(s: str) -> Optional[bool]:
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    return None


def _plain_is_null(s: str) -> bool:
    return s in ("", "~", "null", "Null", "NULL", "None")


def _parse_plain(s: str) -> Any:
    """纯文本标量 → Python 值（对齐 PyYAML 的常用隐式类型）。"""
    b = _plain_is_bool(s)
    if b is not None:
        return b
    if _plain_is_null(s):
        return None
    if _INT_RX.match(s):
        try:
            return int(s)
        except ValueError:
            return s
    if _FLOAT_RX.match(s):
        try:
            return float(s)
        except ValueError:
            return s
    return s


# ----------------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------------

class _Parser:
    def __init__(self, text: str) -> None:
        # 预处理：去整行注释与行尾注释（引号外）、记录缩进。
        self.lines: List[tuple] = []      # (indent, content, lineno)
        for i, raw in enumerate(text.splitlines(), 1):
            stripped = raw.rstrip()
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            if stripped.lstrip(" ").startswith("#"):
                continue
            content = self._strip_comment(stripped.strip())
            if not content.strip():
                continue
            if "\t" in stripped[:indent + 1]:
                raise MiniYAMLError(f"line {i}: 制表符缩进不支持")
            self.lines.append((indent, content, i))
        self.pos = 0

    @staticmethod
    def _strip_comment(s: str) -> str:
        out = []
        quote = None
        i = 0
        while i < len(s):
            ch = s[i]
            if quote == "'":
                if ch == "'":
                    if i + 1 < len(s) and s[i + 1] == "'":
                        out.append("''")
                        i += 2
                        continue
                    quote = None
                out.append(ch)
            elif quote == '"':
                if ch == "\\":
                    out.append(s[i:i + 2])
                    i += 2
                    continue
                if ch == '"':
                    quote = None
                out.append(ch)
            else:
                if ch in ("'", '"'):
                    quote = ch
                    out.append(ch)
                elif ch == "#" and (i == 0 or s[i - 1] in (" ", "\t")):
                    break
                else:
                    out.append(ch)
            i += 1
        return "".join(out).rstrip()

    # ---- 游标 ----

    def _peek(self) -> Optional[tuple]:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def _next(self) -> tuple:
        item = self.lines[self.pos]
        self.pos += 1
        return item

    # ---- 入口：解析块节点（映射或序列），要求同级缩进一致 ----

    def parse_block(self, indent: int) -> Any:
        head = self._peek()
        if head is None:
            return None
        if head[1].startswith("- ") or head[1] == "-":
            return self._parse_seq(indent)
        return self._parse_map(indent)

    def _parse_map(self, indent: int) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        while True:
            item = self._peek()
            if item is None or item[0] < indent:
                break
            if item[0] > indent:
                raise MiniYAMLError(f"line {item[2]}: 意外缩进")
            content = item[1]
            split = _split_key(content)
            if split is None:
                raise MiniYAMLError(f"line {item[2]}: 非法映射行: {content[:40]!r}")
            key_raw, rest = split
            self._next()
            key = _parse_scalar(key_raw)
            if not isinstance(key, str):
                raise MiniYAMLError(f"line {item[2]}: 键必须是字符串")
            rest = rest.strip()
            if rest.startswith(("|", ">")):
                raise MiniYAMLError(f"line {item[2]}: 块标量不支持")
            if rest == "":
                # 值在后续行（更深缩进的映射/序列；序列可与键同缩进——PyYAML 风格）
                nxt = self._peek()
                if nxt is None or nxt[0] < indent:
                    out[key] = None
                elif nxt[0] == indent and (nxt[1].startswith("- ") or nxt[1] == "-"):
                    out[key] = self._parse_seq(indent)
                elif nxt[0] > indent:
                    out[key] = self.parse_block(nxt[0])
                else:
                    out[key] = None
            else:
                out[key] = _parse_inline_value(rest)
        return out

    def _parse_seq(self, indent: int) -> List[Any]:
        out: List[Any] = []
        while True:
            item = self._peek()
            if item is None or item[0] != indent:
                break
            content = item[1]
            if not (content.startswith("- ") or content == "-"):
                break
            self._next()
            rest = content[1:].strip() if content != "-" else ""
            if rest == "":
                nxt = self._peek()
                if nxt is not None and nxt[0] > indent:
                    out.append(self.parse_block(nxt[0]))
                else:
                    out.append(None)
                continue
            if rest.startswith(("'", '"', "[", "{")):
                # 流式集合 / 引号标量：整个 rest 是一个值
                out.append(_parse_inline_value(rest))
                continue
            split = _split_key(rest)
            if split is not None:
                # "- key: value"：序列元素是映射。把本行改写为元素缩进后复用 _parse_map，
                # 使后续同缩进的键（含 PyYAML 风格的同缩进子序列）都归入该元素。
                elem_indent = indent + (len(content) - len(rest))
                self.pos -= 1
                self.lines[self.pos] = (elem_indent, rest, item[2])
                out.append(self._parse_map(elem_indent))
                continue
            out.append(_parse_inline_value(rest))
        return out


_KEY_PLAIN_RX = re.compile(r"^([^\s:][^:]*?)\s*:[ \t]*(.*)$")


def _split_key(content: str) -> Optional[tuple]:
    """映射行 → (key_raw, rest)；非映射行返回 None。支持引号键。"""
    if content[:1] in ("'", '"'):
        quote = content[0]
        i = 1
        while i < len(content):
            if quote == "'" and content[i] == "'":
                if i + 1 < len(content) and content[i + 1] == "'":
                    i += 2
                    continue
                break
            if quote == '"' and content[i] == "\\":
                i += 2
                continue
            if quote == '"' and content[i] == '"':
                break
            i += 1
        if i >= len(content) or content[i] != quote:
            return None
        rest = content[i + 1:].lstrip()
        if not rest.startswith(":"):
            return None
        return content[:i + 1], rest[1:].strip()
    m = _KEY_PLAIN_RX.match(content)
    if not m:
        return None
    return m.group(1), m.group(2)


def _split_flow(s: str) -> List[str]:
    """按顶层逗号切分流式集合内容（引号/嵌套感知）。"""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    quote = None
    i = 0
    while i < len(s):
        ch = s[i]
        if quote == "'":
            if ch == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    buf.append("''")
                    i += 2
                    continue
                quote = None
            buf.append(ch)
        elif quote == '"':
            if ch == "\\":
                buf.append(s[i:i + 2])
                i += 2
                continue
            if ch == '"':
                quote = None
            buf.append(ch)
        else:
            if ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif ch in "[{":
                depth += 1
                buf.append(ch)
            elif ch in "]}":
                depth -= 1
                buf.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        i += 1
    if "".join(buf).strip() != "":
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _parse_flow(s: str) -> Any:
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_flow(p) for p in _split_flow(inner)]
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return {}
        out: Dict[str, Any] = {}
        for p in _split_flow(inner):
            split = _split_key(p)
            if split is None:
                raise MiniYAMLError(f"非法流式映射项: {p[:40]!r}")
            out[str(_parse_scalar(split[0]))] = _parse_flow(split[1])
        return out
    return _parse_scalar(s)


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s.startswith(("&", "*", "!")):
        raise MiniYAMLError(f"锚点/别名/标签不支持: {s[:20]!r}")
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        try:
            return json.loads(s)
        except ValueError:
            raise MiniYAMLError(f"非法双引号标量: {s[:40]!r}")
    return _parse_plain(s)


def _parse_inline_value(s: str) -> Any:
    s = s.strip()
    if s.startswith(("[", "{")):
        return _parse_flow(s)
    return _parse_scalar(s)


def loads(text: str) -> Any:
    """解析 YAML 子集文本。空文本返回 None；非映射顶层也允许（返回对应值）。"""
    p = _Parser(text)
    if not p.lines:
        return None
    out = p.parse_block(p.lines[0][0])
    if p.pos != len(p.lines):
        _, content, ln = p.lines[p.pos]
        raise MiniYAMLError(f"line {ln}: 无法解析的残余内容: {content[:40]!r}")
    return out


def load_file(path: Union[str, "os.PathLike"]) -> Dict[str, Any]:
    """读文件 → dict；不存在 / 读失败 / 非映射 → {}（与 km_config._load_yaml 语义一致）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = loads(fh.read())
    except (OSError, MiniYAMLError):
        return {}
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------------------
# dump（对齐 yaml.safe_dump 块式输出）
# ----------------------------------------------------------------------------

_PLAIN_FORBIDDEN_START = set("-?:,[]{}#&*!|>'\"%@`")


def _needs_quote(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if _plain_is_bool(s) is not None or _plain_is_null(s):
        return True
    if _INT_RX.match(s) or _FLOAT_RX.match(s):
        return True
    if s.startswith(tuple(_PLAIN_FORBIDDEN_START)):
        return True
    if ": " in s or s.endswith(":"):
        return True
    if " #" in s:
        return True
    if "\n" in s or "\r" in s or "\t" in s:
        return True
    # 形如日期/时间戳的字符串（PyYAML 会引号包裹，避免隐式类型歧义）
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return True
    return False


def _quote(s: str) -> str:
    if "\n" in s or "\r" in s or "'" in s:
        return json.dumps(s, ensure_ascii=False)
    return "'" + s + "'"


def _dump_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return _quote(s) if _needs_quote(s) else s


def _dump(v: Any, indent: int) -> List[str]:
    pad = " " * indent
    out: List[str] = []
    if isinstance(v, dict):
        if not v:
            return [pad + "{}"]
        for k, val in v.items():
            ks = _dump_scalar(str(k))
            if isinstance(val, dict) and val:
                out.append(f"{pad}{ks}:")
                out += _dump(val, indent + 2)
            elif isinstance(val, list) and val:
                out.append(f"{pad}{ks}:")
                # PyYAML 风格：序列项不额外缩进
                out += _dump(val, indent)
            elif isinstance(val, dict):
                out.append(f"{pad}{ks}: {{}}")
            elif isinstance(val, list):
                out.append(f"{pad}{ks}: []")
            else:
                out.append(f"{pad}{ks}: {_dump_scalar(val)}")
    elif isinstance(v, list):
        if not v:
            return [pad + "[]"]
        for it in v:
            if isinstance(it, dict) and it:
                # 序列内映射：首键跟在 "- " 后，其余键对齐
                items = list(it.items())
                first_ks = _dump_scalar(str(items[0][0]))
                fv = items[0][1]
                if isinstance(fv, dict) and fv:
                    out.append(f"{pad}- {first_ks}:")
                    out += _dump(fv, indent + 2)
                elif isinstance(fv, list) and fv:
                    out.append(f"{pad}- {first_ks}:")
                    out += _dump(fv, indent)
                elif isinstance(fv, dict):
                    out.append(f"{pad}- {first_ks}: {{}}")
                elif isinstance(fv, list):
                    out.append(f"{pad}- {first_ks}: []")
                else:
                    out.append(f"{pad}- {first_ks}: {_dump_scalar(fv)}")
                sub = dict(items[1:])
                if sub:
                    out += _dump(sub, indent + 2)
            elif isinstance(it, dict):
                out.append(f"{pad}- {{}}")
            elif isinstance(it, list) and it:
                out.append(f"{pad}-")
                out += _dump(it, indent + 2)
            elif isinstance(it, list):
                out.append(f"{pad}- []")
            else:
                out.append(f"{pad}- {_dump_scalar(it)}")
    else:
        out.append(pad + _dump_scalar(v))
    return out


def dumps(data: Any) -> str:
    lines = _dump(data, 0)
    return "\n".join(lines) + "\n"


def save_file(path: Union[str, "os.PathLike"], data: Any) -> None:
    import os
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dumps(data))


# ----------------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------------

def selftest() -> int:
    checks: List[tuple] = []

    def ck(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))

    # 1) 线上 settings.yaml 实样（PyYAML safe_dump 输出：序列不缩进）
    settings_text = """\
version: 1
data_root: G:/Knowledge/data
private_backup_dir: I:/Backup/knowledge
tmp_root: null
defaults:
  sync:
    on_deleted: ask
    exclude:
    - .git/**
    - .git
    - .obsidian/**
  sanitize:
    include_assets: false
    note_dirs:
    - journals
    - pages
    all_text: false
  backup:
    advise: true
"""
    s = loads(settings_text)
    ck("settings: version=1(int)", s.get("version") == 1 and isinstance(s.get("version"), int))
    ck("settings: data_root", s.get("data_root") == "G:/Knowledge/data")
    ck("settings: tmp_root=None", s.get("tmp_root") is None)
    ck("settings: exclude 列表", s["defaults"]["sync"]["exclude"] == [".git/**", ".git", ".obsidian/**"])
    ck("settings: include_assets=False", s["defaults"]["sanitize"]["include_assets"] is False)
    ck("settings: all_text=False", s["defaults"]["sanitize"]["all_text"] is False)
    ck("settings: advise=True", s["defaults"]["backup"]["advise"] is True)
    ck("settings: note_dirs", s["defaults"]["sanitize"]["note_dirs"] == ["journals", "pages"])

    # 2) 线上 sources/<id>.yaml 实样（反斜杠纯文本 + 单引号时间戳）
    src_text = """\
version: 1
id: logseq-main
name: main
source: G:\\Data\\knowledge\\logseq\\main
sync:
  on_deleted: ask
state:
  last_sync: '2026-10-29T14:14:39'
  last_sync_counts:
    new: 1
    changed: 1
    unchanged: 744
    deleted: 0
"""
    c = loads(src_text)
    ck("source: 反斜杠纯文本", c["source"] == "G:\\Data\\knowledge\\logseq\\main")
    ck("source: 单引号时间戳保持字符串", c["state"]["last_sync"] == "2026-10-29T14:14:39")
    ck("source: 嵌套计数", c["state"]["last_sync_counts"]["unchanged"] == 744)

    # 3) 引号与转义
    ck("单引号 '' 转义", loads("k: 'it''s'")["k"] == "it's")
    ck("双引号反斜杠", loads('k: "G:\\\\x"')["k"] == "G:\\x")
    ck("行尾注释", loads("k: v # 注释\nj: w")["j"] == "w")
    ck("整行注释", loads("# c\nk: v")["k"] == "v")
    ck("注释里的 #", loads("k: 'a#b'")["k"] == "a#b")

    # 4) 流式集合
    ck("流式列表", loads("k: [a, b, 'c d']")["k"] == ["a", "b", "c d"])
    ck("流式映射", loads("k: {on_deleted: ask, n: 3}")["k"] == {"on_deleted": "ask", "n": 3})
    ck("空映射", loads("k: {}")["k"] == {})
    ck("空列表", loads("k: []")["k"] == [])
    ck("嵌套流式", loads("k: [{a: 1}, {b: 2}]")["k"] == [{"a": 1}, {"b": 2}])

    # 5) 布尔/数字/空
    ck("yes=True", loads("k: yes")["k"] is True)
    ck("off=False", loads("k: off")["k"] is False)
    ck("~ 为 None", loads("k: ~")["k"] is None)
    ck("空值 None", loads("k:")["k"] is None)
    ck("负整数", loads("k: -5")["k"] == -5)
    ck("浮点", loads("k: 3.25")["k"] == 3.25)
    ck("字符串 3.25 不变", loads("k: '3.25'")["k"] == "3.25")

    # 6) 缩进序列风格（手写常见形态）
    ck("缩进序列风格", loads("sanitize:\n  note_dirs:\n    - journals\n    - pages\n") ==
       {"sanitize": {"note_dirs": ["journals", "pages"]}})

    # 7) dump 风格对齐 PyYAML safe_dump
    obj = {"version": 1, "data_root": "G:/Knowledge/data", "tmp_root": None,
           "defaults": {"sync": {"on_deleted": "ask", "exclude": [".git/**", ".git"]},
                        "sanitize": {"include_assets": False, "note_dirs": ["journals", "pages"]},
                        "backup": {"advise": True}},
           "state": {}}
    d = dumps(obj)
    expect = """\
version: 1
data_root: G:/Knowledge/data
tmp_root: null
defaults:
  sync:
    on_deleted: ask
    exclude:
    - .git/**
    - .git
  sanitize:
    include_assets: false
    note_dirs:
    - journals
    - pages
  backup:
    advise: true
state: {}
"""
    ck("dump 与 PyYAML 风格一致", d == expect)

    # 8) dump 引号规则（像数字/布尔/日期的字符串必须加引号）
    d2 = dumps({"a": "2026-10-29T14:14:39", "b": "true", "c": "1", "d": "G:\\x\\y",
                "e": "带: 冒号", "f": "", "g": "plain 中文"})
    r2 = loads(d2)
    ck("dump→load roundtrip 保留类型", r2 == {"a": "2026-10-29T14:14:39", "b": "true", "c": "1",
                                              "d": "G:\\x\\y", "e": "带: 冒号", "f": "", "g": "plain 中文"})
    ck("日期串被引号", "'2026-10-29T14:14:39'" in d2)
    ck("布尔串被引号", "'true'" in d2)
    ck("数字串被引号", "'1'" in d2)

    # 9) roundtrip：真实配置形态 dump→load 等价
    ck("roundtrip settings", loads(dumps(s)) == s)
    ck("roundtrip source", loads(dumps(c)) == c)

    # 10) 不支持结构报错（不静默）
    for bad in ("k: &anchor v", "a: 1\n---\nb: 2", "k: |\n  块标量"):
        try:
            loads(bad)
            ck(f"应报错: {bad[:12]!r}", False)
        except MiniYAMLError:
            ck(f"应报错: {bad[:12]!r}", True)

    # 11) 序列内映射
    seqmap = loads("items:\n- id: a\n  base: xy\n- id: b\n  base: zw\n")
    ck("序列内映射", seqmap["items"] == [{"id": "a", "base": "xy"}, {"id": "b", "base": "zw"}])

    failed = [n for n, ok in checks if not ok]
    print(json.dumps({"selftest": True, "checks": len(checks), "pass": not failed,
                      "failed": failed}, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="零依赖 YAML 子集（load/dump）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--roundtrip", metavar="FILE", help="读文件→dump→再解析，输出是否等价")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.roundtrip:
        with open(args.roundtrip, encoding="utf-8") as fh:
            data = loads(fh.read())
        again = loads(dumps(data))
        print(json.dumps({"equal": again == data, "type": type(data).__name__},
                         ensure_ascii=False))
        return 0 if again == data else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
