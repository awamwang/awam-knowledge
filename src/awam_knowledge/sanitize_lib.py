# SPDX-License-Identifier: MIT
"""sanitize_lib — 脱敏管线公共库（无第三方依赖，仅标准库）。

移植自 knowledge-desensitization/scripts/sanitize_lib.py（M3 融合），核心口径逐字保留。

设计要点
- 稳定 token：token = HMAC-SHA256(salt, "rule\\0value")[:8]，同一原文永远得到同一 token，
  重复运行不会产生新 token，diff 稳定；salt 丢失则永久不可逆。
- 只改块内文本：不触碰 Logseq 的 ((uuid)) 块引用语法本身（uuid 是 36 位十六进制+连字符，
  不匹配任何规则），只替换命中的敏感子串。
- 零原文外泄：token 由 HMAC 派生，报告里只出现 token，不出现原文。

四种召回层（rules.json 里用 mode 区分）
- mode="regex"    L1 结构层：已知厂商格式（sk- / gh*_ / AKIA / JWT / PEM …）
- mode="entropy"  L3 兜底层：高熵随机串启发式，用于兜住「未知格式」的密钥
- mode="literal"  L0 字面量层：由 config/literals.json 展开生成（常用密码字段）
  关键词邻域（L2）用普通 regex 规则表达：关键词 + 分隔符 + 值

2026-10-18 策略更新（用户决策）
- 基线上宁可「过度脱敏」，只要保留「易于恢复」：使用中发现误报，用 restore 把某个
  占位符按需填回原文即可，不必为了少误报而调低召回。
- 整文件/整目录排除（assets、.agent_history 等）由 config/exclude.json 统一下发。
"""
from __future__ import annotations

import collections
import fnmatch
import hashlib
import hmac
import json
import math
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Iterable

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

TEXT_EXTS = {
    ".md", ".org", ".txt", ".json", ".yml", ".yaml", ".env", ".ini", ".cfg",
    ".conf", ".toml", ".csv", ".tsv", ".log", ".py", ".js", ".ts", ".sh",
    ".ps1", ".html", ".htm", ".xml", ".css", ".sql", ".gitignore", ".mdx",
}

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svgz", ".tif",
    ".tiff", ".mp3", ".mp4", ".m4a", ".wav", ".avi", ".mov", ".mkv", ".pdf",
    ".zip", ".7z", ".rar", ".gz", ".tar", ".exe", ".dll", ".so", ".dylib",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".db", ".sqlite", ".sqlite3",
    ".pyc", ".class", ".jar", ".apk", ".iso", ".bin",
}

# 生成物 / 运行时目录：默认不参与脱敏（内容由源重新生成，或属于另一条治理线）
GENERATED_NAMES = {
    "index.json", "block-refs.json", "journal-date-map.json",
}
GENERATED_DIRS = {".logseq", ".git", "node_modules", "__pycache__"}

# Agent 运行时目录（性质与笔记不同，默认排除，需显式开启）
# .agent_history：OpenClaw 文件操作历史（JSON，内含正则/代码片段，非笔记）
RUNTIME_DIRS = {
    ".office-claw", ".agent_history", "messages", "prompt_attachment",
    "agents", "coding_memory", "memory", "skills", "todo", "context",
}

# L3 熵兜底用的「值」切分
GENERIC_TOKEN_RX = re.compile(r"[A-Za-z0-9!@#$%^&*_\-+/.=]{8,}")

# 明显不是密钥的英文词/常量（L3 兜底层过滤）
STOPWORDS = {
    "password", "passwd", "secret", "token", "apikey", "api_key", "accesskey",
    "access_key", "clientid", "client_id", "clientsecret", "client_secret",
    "username", "user_name", "administrator", "localhost", "https", "http",
    "example", "configuration", "application", "development", "production",
    "documentation", "international", "requirements", "environment",
    "false", "true", "none", "null", "undefined", "default", "string",
    "logseq", "github", "docker", "kubernetes", "javascript", "typescript",
    "webhook", "authorization", "bearer", "algorithms", "signature",
    "experience", "professional", "information", "management", "connection",
    "readme", "changelog", "index.html", "package.json", "screenshot",
}


# ----------------------------------------------------------------------------
# 数学辅助
# ----------------------------------------------------------------------------

def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in collections.Counter(s).values())


def char_classes(s: str) -> int:
    n = 0
    if re.search(r"[a-z]", s):
        n += 1
    if re.search(r"[A-Z]", s):
        n += 1
    if re.search(r"\d", s):
        n += 1
    if re.search(r"[^A-Za-z0-9]", s):
        n += 1
    return n


def shape(s: str) -> str:
    """把值抽象成「形状」（a=小写 A=大写 9=数字 其余保留符号）。

    用于诊断/还原时人工判断「像不像密钥」，全程不泄露原值。
    """
    out = []
    for ch in s:
        if "a" <= ch <= "z":
            out.append("a")
        elif "A" <= ch <= "Z":
            out.append("A")
        elif ch.isdigit():
            out.append("9")
        else:
            out.append(ch)
    return "".join(out)


# ----------------------------------------------------------------------------
# 校验器
# ----------------------------------------------------------------------------

def _luhn_ok(s: str) -> bool:
    digits = [int(c) for c in s if c.isdigit()]
    if not (16 <= len(digits) <= 19):
        return False
    total, alt = 0, False
    for d in reversed(digits):
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


_CN_ID_W = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_CN_ID_CC = "10X98765432"


def _cn_id_ok(s: str) -> bool:
    s = s.strip()
    if len(s) != 18:
        return False
    body = s[:17]
    if not body.isdigit():
        return False
    total = sum(int(body[i]) * _CN_ID_W[i] for i in range(17))
    return _CN_ID_CC[total % 11] == s[17].upper()


VALIDATORS = {"none": None, "luhn": _luhn_ok, "cn_id": _cn_id_ok}


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------

@dataclass
class Rule:
    name: str
    label: str
    enabled: bool
    credential: bool
    mode: str = "regex"          # regex | entropy
    pattern: str = ""
    flags: str = ""
    replace_group: int = 0
    validator: str = "none"
    extract_hint: str = ""
    # --- entropy 模式专用 ---
    min_len: int = 0
    min_entropy: float = 0.0
    min_classes: int = 0
    exclude_regex: str = ""
    source: str = ""             # 来源标注：rules.json / literals.json:<id>
    rx: re.Pattern = field(init=False, repr=False)

    def __post_init__(self):
        if self.mode == "entropy":
            self.rx = GENERIC_TOKEN_RX
            return
        f = 0
        if "i" in self.flags:
            f |= re.IGNORECASE
        if "m" in self.flags:
            f |= re.MULTILINE
        if "s" in self.flags:
            f |= re.DOTALL
        self.rx = re.compile(self.pattern, f)


@dataclass
class Hit:
    rule: Rule
    start: int
    end: int
    token: str
    line: int


# ----------------------------------------------------------------------------
# 字面量层（config/literals.json → Rule 列表）
# ----------------------------------------------------------------------------

def _expand_literal(tpl: str, base: str, prefixes, suffixes) -> list[str]:
    """把 {base} 占位符展开为「前缀 × base × 后缀」的全部变体。"""
    variants = set()
    for p in (prefixes or [""]):
        for s in (suffixes or [""]):
            variants.add(tpl.replace("{base}", f"{p}{base}{s}"))
    return sorted(variants, key=len, reverse=True)


def _pair_rules(it: dict, label: str) -> list[Rule]:
    """组合型字面量：密码「含片段 A、B（任意顺序）」或「单独为 A / B」。

    场景（用户 2026-10-18 需求）：常见两个字符串 A、B，密码可能是
      - 单独 A；单独 B；
      - .*A…B.*（A 在 B 前）；.*B…A.*（B 在 A 前）。

    字段
      parts       片段列表（≥2），例如 ["AAA", "BBB"]
      orders      "both"（默认，覆盖 A…B 与 B…A）或 ["ab","ba"] 形式
      wclass      组成密码的字符集（正则片段），默认含常见符号
      pad         片段两侧/之间允许的额外字符数上限（默认 32）
      single      是否同时生成「单独片段」规则（默认 true）
      require_keyword  组合规则是否要求同行出现关键词（默认 true）。
                  设为 false → 直接匹配「同行含 A…B / B…A」的裸组合
                  （对应用户「.*AB.* / .*BA.*」的场景）。
      single_mode 单独片段规则的锚定方式：
                  "keyword"（默认，需关键词同行）/ "line"（整行恰为该片段）/
                  "anywhere"（同行任意位置）。require_keyword=false 时默认 "line"。
      keywords / separator / whole_word 同 context 模式
    """
    raw = [p for p in (it.get("parts") or []) if p]
    if len(raw) < 2:
        return []
    C = it.get("wclass", r"[A-Za-z0-9!@#$%^&*_\-+/.=@]")
    pad = int(it.get("pad", 32))
    side = f"{C}{{0,{pad}}}"
    mid = f"{C}{{0,{pad}}}"
    esc = [re.escape(p) for p in raw]
    orders = it.get("orders", "both")
    seqs: list[list[str]] = []
    if orders == "both":
        seqs = [esc, list(reversed(esc))] if len(esc) == 2 else [esc]
    elif isinstance(orders, list):
        for o in orders:
            idxs = [ord(c) - 97 for c in str(o).lower() if c.isalpha()]
            if idxs and all(0 <= i < len(esc) for i in idxs):
                seqs.append([esc[i] for i in idxs])
    if not seqs:
        seqs = [esc]
    alts = [f"{side}{mid.join(seq)}{side}" for seq in seqs]
    alt = "|".join(f"(?:{a})" for a in alts)
    whole = it.get("whole_word", True)
    lb = r"(?<![A-Za-z0-9])" if whole else ""
    rb = r"(?![A-Za-z0-9])" if whole else ""
    kw = it.get("keywords") or ["密码", "口令", "密钥", "password", "pwd", "passwd"]
    kws = "|".join(re.escape(k) for k in kw)
    sep = it.get("separator", r"\s*[:：=＝]?\s*")
    gid = it.get("id", "pair")
    req_kw = bool(it.get("require_keyword", True))
    pair_pat = (f"(?:{kws}){sep}({lb}(?:{alt}){rb})" if req_kw
                else f"({lb}(?:{alt}){rb})")
    out = [Rule(
        name=f"literal:{gid}", label=label, enabled=True,
        credential=bool(it.get("credential", True)),
        mode="regex", pattern=pair_pat, flags="",
        replace_group=1, validator="none",
        extract_hint=it.get("note", "用户字面量（密码片段组合：含 A、B 或二者任意顺序）"),
        source=f"literals.json:{gid}",
    )]
    if it.get("single", True):
        single_mode = it.get("single_mode") or ("keyword" if req_kw else "line")
        for i, p in enumerate(raw):
            ep = re.escape(p)
            if single_mode == "line":
                pat, flags = rf"^[ \t]*(?:[-*+][ \t]*)?({ep})[ \t]*$", "m"
            elif single_mode == "anywhere":
                pat, flags = f"({lb}{ep}{rb})", ""
            else:
                pat, flags = f"(?:{kws}){sep}({lb}{ep}{rb})", ""
            out.append(Rule(
                name=f"literal:{gid}:p{i}", label=label, enabled=True,
                credential=bool(it.get("credential", True)),
                mode="regex", pattern=pat,
                flags=flags, replace_group=1, validator="none",
                extract_hint="用户字面量（单个密码片段）",
                source=f"literals.json:{gid}",
            ))
    return out


def literals_to_rules(items: list[dict]) -> list[Rule]:
    """把字面量配置展开成规则。

    字段
      id / label / credential / enabled
      match       "context"（推荐，仅在关键词行内生效）| "literal"（全文生效，要求 base 长度≥8）
                  | "pair"（组合型：密码含 A、B 任意顺序，见 _pair_rules）
      base        常用密码本体，例如 "abc"（context/literal 用）
      prefixes    前缀变体，例如 ["", "Abc", "ABC"]
      suffixes    后缀变体，例如 ["", "123", "!", "2024"]
      keywords    context 模式下的触发关键词，例如 ["密码","password","pwd"]
      separator   关键词与值之间的分隔符（正则片段），默认 "\\s*[:：=＝]?\\s*"
      whole_word  是否要求两侧非字母数字（默认 true）
    """
    out: list[Rule] = []
    for it in items:
        if not it.get("enabled", True):
            continue
        match = it.get("match", "context")
        label = it.get("label", "literal")
        if match == "pair":
            out.extend(_pair_rules(it, label))
            continue
        base = it.get("base", "")
        if not base:
            continue
        name = f"literal:{it.get('id', base)}"
        variants = _expand_literal("{base}", base, it.get("prefixes"), it.get("suffixes"))
        alt = "|".join(re.escape(v) for v in variants)
        whole = it.get("whole_word", True)
        lb = r"(?<![A-Za-z0-9])" if whole else ""
        rb = r"(?![A-Za-z0-9])" if whole else ""
        if match == "literal":
            if len(base) < it.get("min_len_guard", 8):
                # 短字面量全文匹配会误伤整个知识库，直接拒绝
                continue
            pattern = f"{lb}(?:{alt}){rb}"
            group = 0
        else:
            kw = it.get("keywords") or ["密码", "口令", "密钥", "password", "pwd", "passwd"]
            kws = "|".join(re.escape(k) for k in kw)
            sep = it.get("separator", r"\s*[:：=＝]?\s*")
            pattern = f"(?:{kws}){sep}({alt}){rb}"
            group = 1
        out.append(Rule(
            name=name, label=label,
            enabled=True, credential=bool(it.get("credential", True)),
            mode="regex", pattern=pattern, flags="",
            replace_group=group, validator="none",
            extract_hint=it.get("note", "用户字面量（常用密码/固定口令）"),
            source=f"literals.json:{it.get('id', base)}",
        ))
    return out


def load_literals(path: str) -> list[Rule]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return literals_to_rules(cfg.get("items", []))


# ----------------------------------------------------------------------------
# 整文件/整目录排除（config/exclude.json）
# ----------------------------------------------------------------------------

def load_path_excludes(path: str, layered: bool = True) -> list[str]:
    """加载「整文件/整目录排除」的路径 glob 列表。

    layered=True（默认）时**叠加全局配置** config/exclude.json（并集、去重）；
    即「全局为默认、每源再补充」，两块合起来才是生效的排除集。
    """
    out: list[str] = []
    g = global_config_dir() if layered else None
    if g:
        p = os.path.join(g, "exclude.json")
        try:
            with open(p, encoding="utf-8") as fh:
                out += [x for x in json.load(fh).get("paths", []) if x]
        except (OSError, json.JSONDecodeError):
            pass
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                out += [x for x in json.load(fh).get("paths", []) if x]
        except (OSError, json.JSONDecodeError):
            pass
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def path_excluded(rel: str, patterns: list[str]) -> bool:
    """rel 为相对根的路径；按 glob 匹配（正/反斜杠都归一为 /）。"""
    if not patterns:
        return False
    norm = rel.replace("\\", "/")
    if any(fnmatch.fnmatch(norm, p) for p in patterns):
        return True
    # 目录形如 "assets" 时，也视为排除其下全部内容
    return any(norm == p or norm.startswith(p.rstrip("/") + "/") for p in patterns)


# ----------------------------------------------------------------------------
# salt 与 token
# ----------------------------------------------------------------------------

def get_salt(path: str, create: bool = True) -> bytes:
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    if not create:
        raise FileNotFoundError(path)
    salt = secrets.token_bytes(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(salt)
    os.chmod(path, 0o600)
    return salt


def make_token(salt: bytes, rule: Rule, value: str) -> str:
    digest = hmac.new(salt, f"{rule.name}\0{value}".encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return f"[REDACTED:{rule.label}:{digest[:8]}]"


# ----------------------------------------------------------------------------
# 配置分层：全局配置目录（用户级）为默认；每源 <秘密材料根>/<id>/config/ 为覆盖
#   - rules.json / literals.json：按 name / id 合并，**后者覆盖前者**（每源胜全局）
#   - whitelist.json / exclude.json：取**并集**（都是「越排除越多」的加法语义）
# 这样用户级 <KM_CONFIG_HOME>/config/*.json 的修改**实时生效**，不再是建源时的一次性种子。
# ----------------------------------------------------------------------------

def global_config_dir() -> str | None:
    """用户级全局配置目录；懒加载 km_config 以避免导入环。不可用时返回 None。"""
    from . import km_config
    return km_config.global_config_dir()


def _read_json(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def _merge_by_key(layers: list[list], key: str) -> list:
    """多层列表按 key 合并：后者覆盖前者（覆盖不改变首次出现的位置）。"""
    od: dict = {}
    for layer in layers:
        for it in layer or []:
            if isinstance(it, dict) and it.get(key) is not None:
                od[it[key]] = it
    return list(od.values())


def merge_literals(paths: list[str]) -> list[Rule]:
    """按 id 合并多个 literals.json → 规则（后者覆盖前者）。"""
    return literals_to_rules(_merge_by_key([_read_json(p).get("items", []) for p in paths], "id"))


def merge_rule_dicts(paths: list[str]) -> list[dict]:
    """按 name 合并多个 rules.json 的 rules 列表（后者覆盖前者）。"""
    return _merge_by_key([_read_json(p).get("rules", []) for p in paths], "name")


def load_whitelist_multi(paths: list[str]) -> set[str]:
    """白名单取并集（指纹集合，文件里不出现明文）。"""
    global _WHITELIST
    fps: set[str] = set()
    for p in paths:
        v = _read_json(p).get("fingerprints", [])
        if isinstance(v, list):
            fps |= set(v)
    _WHITELIST = fps
    return _WHITELIST


# ----------------------------------------------------------------------------
# 白名单（按「值的指纹」排除，文件里不出现明文）
# ----------------------------------------------------------------------------

_WHITELIST: set[str] = set()


def value_fp(v: str) -> str:
    """值的指纹：sha256 前 8 位。白名单/RHS 用它标识一个值而不泄露内容。"""
    return hashlib.sha256(v.encode("utf-8")).hexdigest()[:8]


def load_whitelist(path: str) -> set[str]:
    global _WHITELIST
    if not os.path.exists(path):
        _WHITELIST = set()
        return _WHITELIST
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    _WHITELIST = set(cfg.get("fingerprints", []))
    return _WHITELIST


# ----------------------------------------------------------------------------
# 规则加载
# ----------------------------------------------------------------------------

def load_rules(path: str, literals_path: str | None = None,
               layered: bool = True) -> list[Rule]:
    """加载规则。字面量规则**排在前面**（优先级更高，专用规则优先于通用规则）。

    配置分层（layered=True，默认；后者覆盖前者）：
      1. 内置基线模板 `<包>/config/rules.json`（随包升级）
      2. **全局配置目录**（用户级 `<KM_CONFIG_HOME>/config/`）—— 所有源的默认
      3. 每源目录（`path` 所在目录 `<秘密材料根>/<id>/config/`）—— 源级覆盖
    rules.json 按 name 合并；literals.json 按 id 合并（全局 → 每源）；
    whitelist.json 取并集。**因此改全局配置会立即生效**，不再是建源时的一次性种子。
    传 layered=False 可退回「只看每源目录」的旧行为。

    literals_path 缺省为 rules.json 同目录下的 literals.json（存在才加载）。
    白名单按「值的指纹」匹配，因此配置文件里不含任何明文。
    """
    from . import km_paths
    base = os.path.dirname(os.path.abspath(path))
    if literals_path is None:
        literals_path = os.path.join(base, "literals.json")
    g = global_config_dir() if layered else None

    builtin_cfg = km_paths.builtin_config_dir()
    rule_paths = [os.path.join(builtin_cfg, "rules.json")] if layered else []
    if g:
        rule_paths.append(os.path.join(g, "rules.json"))
    rule_paths.append(path)
    raw = merge_rule_dicts(rule_paths) if layered else _read_json(path).get("rules", [])

    if layered:
        lit_paths = ([os.path.join(g, "literals.json")] if g else []) + [literals_path]
        lit = merge_literals(lit_paths)
        load_whitelist_multi((
            ([os.path.join(g, "whitelist.json")] if g else [])
            + [os.path.join(base, "whitelist.json")]))
    else:
        load_whitelist(os.path.join(base, "whitelist.json"))
        lit = load_literals(literals_path)

    rules = [
        Rule(**{k: v for k, v in r.items()
                if k in Rule.__dataclass_fields__ and k != "rx"})
        for r in raw
    ]
    return lit + rules


# ----------------------------------------------------------------------------
# 文件遍历
# ----------------------------------------------------------------------------

def is_text_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in BINARY_EXTS:
        return False
    if ext in TEXT_EXTS:
        return True
    # 无扩展名的文件按小型文本尝试
    return ext == ""


def iter_files(root: str, include_runtime: bool = False,
               include_generated: bool = False) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            if d in GENERATED_DIRS:
                continue
            if not include_runtime and d in RUNTIME_DIRS:
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            if not include_generated and fn in GENERATED_NAMES:
                continue
            p = os.path.join(dirpath, fn)
            if is_text_file(p):
                yield p


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except (UnicodeDecodeError, OSError):
        return None


# ----------------------------------------------------------------------------
# 匹配与替换
# ----------------------------------------------------------------------------

def _entropy_candidates(rule: Rule, text: str):
    """L3：高熵串候选。返回 (start, end) 列表。"""
    excl = re.compile(rule.exclude_regex) if rule.exclude_regex else None
    for m in GENERIC_TOKEN_RX.finditer(text):
        v = m.group(0)
        if len(v) < rule.min_len:
            continue
        if v.lower() in STOPWORDS:
            continue
        if v.isalpha():                      # 纯字母 → 英文词，跳过
            continue
        if char_classes(v) < rule.min_classes:
            continue
        if shannon_entropy(v) < rule.min_entropy:
            continue
        if excl and excl.search(v):
            continue
        yield m.span()


# 已生成的占位符：绝不允许被任何规则二次匹配（否则 token 会自我嵌套，
# 破坏幂等性与回扫验证 —— 例如 [REDACTED:secret:1a2b3c4d] 里的 "secret:..."
# 会被关键词规则再抓一次）。
TOKEN_RX = re.compile(r"\[REDACTED:[A-Za-z0-9_\-]+:[0-9a-f]{8}\]")

# 回填引用里的唯一主键（见 km_paths.VAULT_REF）：与 TOKEN_RX 分开命名空间，
# 扫描时一并保护，避免对已回填的工作副本再次脱敏时误伤主键。
VAULT_ID_RX = re.compile(r"\[vault:([A-Za-z0-9_\-]+):([0-9a-f]{8})\]")


# ----------------------------------------------------------------------------
# 资源路径保护区（脱敏不改「资源文件名/路径」，避免 markdown 链接断裂）
# ----------------------------------------------------------------------------

_MD_EMBED_RX = re.compile(r"!?\[\[([^\]\[\n]*)\]")
# 链接目标：允许文件名里出现一层括号（如 `(Av505310891,P13)`），否则会截断保护区间
_MD_TARGET_RX = re.compile(r"\]\(((?:[^()\n]|\([^()\n]*\))*)\)")
# markdown 图片 ALT：`![<文件名.ext>](...)` —— ALT 里若是资源文件名，同样是「资源名」，不脱敏
_MD_ALT_RX = re.compile(r"!\[([^\]\n]*?\.[A-Za-z0-9]{2,6})\](?=\()")
_ASSET_PATH_RX = re.compile(r"(?:\.{1,2}/)+assets/[^\s)\]\[]+")
_ASSET_FILE_RX = re.compile(
    r"\b[0-9A-Za-z_\-]{12,}\.(?:png|jpe?g|gif|svg|webp|bmp|avif|mp4|mov|webm|mp3|wav|pdf|zip|canvas)\b")
_URL_USERINFO_RX = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/\s]*@")


def resource_spans(text: str) -> list[tuple[int, int]]:
    """markdown 嵌入名 / 链接目标 / assets 路径的字符区间（脱敏时跳过）。

    为什么：`![[xx.png]]` 与 `](../assets/xx.png)` 里的名字如果被替换，
    而 `assets/` 本身不参与改名 → 链接必然断裂。
    远程 URL 也保护；**唯一例外**是 URL 里带用户凭据段（`账号:口令@主机`）——
    那种 URL 可能藏密钥，仍交给规则扫描。
    """
    spans: list[tuple[int, int]] = []
    for m in _MD_ALT_RX.finditer(text):
        spans.append(m.span(1))
    for m in _MD_EMBED_RX.finditer(text):
        spans.append(m.span(1))
    for m in _MD_TARGET_RX.finditer(text):
        if _URL_USERINFO_RX.match(m.group(1).lstrip()):
            continue   # 带用户凭据段（账号:口令@）的 URL —— 可能藏密钥，不保护
        spans.append(m.span(1))
    for m in _ASSET_PATH_RX.finditer(text):
        spans.append(m.span())
    for m in _ASSET_FILE_RX.finditer(text):
        spans.append(m.span())
    return spans


def find_hits(text: str, rules: list[Rule], salt: bytes,
              only_enabled: bool = True) -> list[Hit]:
    """返回去重、去重叠、按位置排序的命中列表。

    优先级 = rules 列表顺序（字面量规则在最前）；同规则取最长；再做包含去重。
    保护区 = 既有占位符（保幂等）+ 资源路径/文件名（防链接断裂）。
    """
    protected = [(m.start(), m.end()) for m in TOKEN_RX.finditer(text)]
    protected += [(m.start(), m.end()) for m in VAULT_ID_RX.finditer(text)]
    protected += resource_spans(text)
    raw: list[Hit] = []
    for idx, rule in enumerate(rules):
        if only_enabled and not rule.enabled:
            continue
        spans = (_entropy_candidates(rule, text) if rule.mode == "entropy"
                 else None)
        for m in (spans if spans is not None else rule.rx.finditer(text)):
            if spans is not None:
                s, e = m
            elif rule.replace_group:
                try:
                    s, e = m.span(rule.replace_group)
                except IndexError:
                    continue
            else:
                s, e = m.span()
            if s < 0 or e <= s:
                continue
            if protected and any(s < pe and ps < e for ps, pe in protected):
                continue  # 落在既有占位符内部 → 跳过（保幂等）
            value = text[s:e]
            if _WHITELIST and value_fp(value) in _WHITELIST:
                continue
            validator = VALIDATORS.get(rule.validator)
            if validator and not validator(value):
                continue
            raw.append(Hit(rule=rule, start=s, end=e,
                           token=make_token(salt, rule, value),
                           line=text.count("\n", 0, s) + 1))
    # 规则顺序 = 优先级；同优先取最长；再按出现位置排序
    order = {id(r): i for i, r in enumerate(rules)}
    raw.sort(key=lambda h: (order[id(h.rule)], -(h.end - h.start), h.start))
    chosen: list[Hit] = []
    for h in raw:
        if any(h.start < c.end and c.start < h.end for c in chosen):
            continue
        chosen.append(h)
    chosen.sort(key=lambda h: h.start)
    return chosen


def apply_hits(text: str, hits: list[Hit]) -> str:
    out = text
    for h in sorted(hits, key=lambda x: x.start, reverse=True):
        out = out[:h.start] + h.token + out[h.end:]
    return out


# ----------------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------------

def selftest() -> int:
    import tempfile
    checks: list[tuple] = []

    def ck(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))

    salt1 = b"S" * 32
    salt2 = b"T" * 32
    r_secret = Rule(name="sk_key", label="secret", enabled=True, credential=True,
                    pattern=r"sk-[A-Za-z0-9]{20,}")
    text = "api 密钥 sk-" "AbCdEf123456789012345678 用于测试"

    # 1) token 稳定与盐敏感
    hits = find_hits(text, [r_secret], salt1)
    ck("命中 1 处", len(hits) == 1)
    ck("token 格式", bool(re.fullmatch(r"\[REDACTED:secret:[0-9a-f]{8}\]", hits[0].token)))
    ck("token 稳定", hits[0].token == find_hits(text, [r_secret], salt1)[0].token)
    ck("换盐换 token", hits[0].token != find_hits(text, [r_secret], salt2)[0].token)
    ck("行号", hits[0].line == 1)

    # 2) 幂等：已替换文本不再产生新命中
    once = apply_hits(text, hits)
    ck("替换成功", "sk-AbCdEf" not in once)
    ck("幂等：二遍零命中", find_hits(once, [r_secret], salt1) == [])

    # 3) 熵兜底：高熵命中、英文词/低熵跳过
    r_ent = Rule(name="entropy", label="entropy", enabled=True, credential=True,
                 mode="entropy", min_len=20, min_entropy=3.5, min_classes=3)
    ent_text = "normal words here and x9G$kQ2#pLz8@vR4mW7!token plus sk is fine"
    ehits = find_hits(ent_text, [r_ent], salt1)
    ck("熵规则命中高熵串", any("x9G" in ent_text[h.start:h.end] for h in ehits))
    ck("熵规则不误伤英文", not any("normal" in ent_text[h.start:h.end] for h in ehits))

    # 4) 字面量 context 模式：仅关键词行内生效
    rules = literals_to_rules([{"id": "pw", "label": "literal", "credential": True,
                                "base": "abc123", "keywords": ["密码", "password"]}])
    ck("字面量规则已生成", len(rules) >= 1)
    lhits = find_hits("密码: abc123", rules, salt1)
    ck("关键词邻域命中", len(lhits) == 1)
    ck("裸串不命中（context）", find_hits("随便一句 abc123 结尾", rules, salt1) == [])

    # 5) pair 组合：任意顺序 + 关键词；single 默认关闭时无单独规则
    prules = _pair_rules({"id": "pp", "parts": ["Alpha1", "Beta2"], "require_keyword": True}, "literal")
    ck("pair 规则含组合", any(r.name == "literal:pp" for r in prules))
    ck("pair A→B 命中", len(find_hits("密码: xAlpha1yyBeta2z", prules, salt1)) >= 1)
    ck("pair B→A 命中", len(find_hits("密码: qBeta2xxAlpha1w", prules, salt1)) >= 1)
    ck("pair 无关键词不命中", find_hits("xAlpha1yyBeta2z", prules, salt1) == [])
    # require_keyword=false → 裸组合 + single line 模式
    prules2 = _pair_rules({"id": "pq", "parts": ["Alpha1", "Beta2"],
                           "require_keyword": False}, "literal")
    ck("裸组合命中", len(find_hits("备注 xAlpha1yyBeta2z 尾", prules2, salt1)) >= 1)

    # 6) 路径排除
    ck("glob 排除", path_excluded("assets/a.png", ["assets/**"]))
    ck("目录前缀排除", path_excluded("assets/sub/a.png", ["assets"]))
    ck("反斜杠归一", path_excluded("assets\\a.png", ["assets/**"]))
    ck("未排除不命中", not path_excluded("journals/a.md", ["assets/**"]))

    # 7) 资源保护区
    md = "![[shot_20240101.png]]\n[链接](https://user:pass@example.com/x)\n![alt.png](../assets/alt.png)"
    spans = resource_spans(md)
    ck("嵌入名受保护", any(md[s:e] == "shot_20240101.png" for s, e in spans))
    ck("带凭据 URL 不保护", not any("user:pass" in md[s:e] for s, e in spans))
    ck("assets 路径受保护", any("../assets/alt.png" in md[s:e] for s, e in spans))

    # 8) 白名单（指纹）
    global _WHITELIST
    old_wl = set(_WHITELIST)
    try:
        _WHITELIST = {value_fp("sk-" "Zzzzzzzzzzzzzzzzzzzzzzzz")}
        rx_rule = Rule(name="sk2", label="secret", enabled=True, credential=True,
                       pattern=r"sk-[A-Za-z]{24}")
        ck("白名单命中排除", find_hits("v sk-" "Zzzzzzzzzzzzzzzzzzzzzzzz w", [rx_rule], salt1) == [])
        ck("非白名单仍命中", len(find_hits("v sk-" "Yyyyyyyyyyyyyyyyyyyyyyyy w", [rx_rule], salt1)) == 1)
    finally:
        _WHITELIST = old_wl

    # 9) 校验器
    ck("luhn 合法卡号", _luhn_ok("4111111111111111"))
    ck("luhn 非法卡号", not _luhn_ok("4111111111111112"))
    ck("cn_id 校验位", _cn_id_ok("11010519491231002X"))
    ck("cn_id 非法", not _cn_id_ok("110105194912310021"))

    # 10) get_salt 建立与读取一致
    d = tempfile.mkdtemp(prefix="sl-salt-")
    sp = os.path.join(d, "vault", "map", "salt.bin")
    s_a = get_salt(sp)
    s_b = get_salt(sp)
    ck("salt 建立后读取一致", s_a == s_b and len(s_a) == 32)

    # 11) 内置模板存在且可加载（分层基线）
    from . import km_paths
    ck("内置 rules.json 存在", os.path.isfile(os.path.join(km_paths.builtin_config_dir(), "rules.json")))
    bi = merge_rule_dicts([os.path.join(km_paths.builtin_config_dir(), "rules.json")])
    ck("内置规则可加载", len(bi) >= 20)

    # 12) 分层合并：内置 → 全局 → 每源（后者覆盖），literals 按 id 合并，whitelist 并集
    old_home = os.environ.get("KM_CONFIG_HOME")
    tmp = tempfile.mkdtemp(prefix="sl-cfg-")
    os.environ["KM_CONFIG_HOME"] = tmp
    try:
        g = os.path.join(tmp, "config")
        os.makedirs(g, exist_ok=True)
        with open(os.path.join(g, "rules.json"), "w", encoding="utf-8") as fh:
            json.dump({"rules": [{"name": "sk_key", "label": "overridden", "enabled": True,
                                  "credential": True, "pattern": r"sk-[A-Za-z0-9]{20,}"}]}, fh)
        with open(os.path.join(g, "literals.json"), "w", encoding="utf-8") as fh:
            json.dump({"items": [{"id": "glit", "label": "literal", "credential": True,
                                  "base": "globalpw1", "keywords": ["密码"]}]}, fh)
        with open(os.path.join(g, "whitelist.json"), "w", encoding="utf-8") as fh:
            json.dump({"fingerprints": [value_fp("sk-" "Whitelisted0000000000000")]}, fh)
        per = os.path.join(tmp, "src-config")
        os.makedirs(per, exist_ok=True)
        with open(os.path.join(per, "rules.json"), "w", encoding="utf-8") as fh:
            json.dump({"rules": [{"name": "local_rule", "label": "local", "enabled": True,
                                  "credential": True, "pattern": r"LOCAL-[0-9]{6}"}]}, fh)
        with open(os.path.join(per, "literals.json"), "w", encoding="utf-8") as fh:
            json.dump({"items": [{"id": "glit", "label": "literal", "credential": True,
                                  "base": "perSourcePW", "keywords": ["密码"]},
                                 {"id": "loc", "label": "literal", "credential": True,
                                  "base": "anotherpw", "keywords": ["密码"]}]}, fh)
        with open(os.path.join(per, "whitelist.json"), "w", encoding="utf-8") as fh:
            json.dump({"fingerprints": [value_fp("LOCAL-999999")]}, fh)
        layered = load_rules(os.path.join(per, "rules.json"))
        names = [r.name for r in layered]
        ck("字面量规则排最前", names[0].startswith("literal:"))
        ck("每源 literals 覆盖全局同 id", any(r.name == "literal:glit" and "perSourcePW" in r.pattern for r in layered))
        ck("每源 literals 新增", any(r.name == "literal:loc" for r in layered))
        ck("全局覆盖内置同 name", any(r.name == "sk_key" and r.label == "overridden" for r in layered))
        ck("内置其余规则保留", len([r for r in layered if not r.name.startswith("literal:")]) > 5)
        ck("每源新增规则", any(r.name == "local_rule" for r in layered))
        # 白名单并集生效（全局 + 每源）
        ck("全局白名单生效", find_hits("v sk-" "Whitelisted0000000000000 w", layered, salt1) == [])
        ck("每源白名单生效", find_hits("v LOCAL-999999 w", layered, salt1) == [])
        ck("非白名单仍命中", len(find_hits("v LOCAL-123456 w", layered, salt1)) == 1)
    finally:
        if old_home is None:
            os.environ.pop("KM_CONFIG_HOME", None)
        else:
            os.environ["KM_CONFIG_HOME"] = old_home

    failed = [n for n, ok in checks if not ok]
    print(json.dumps({"selftest": True, "checks": len(checks), "pass": not failed,
                      "failed": failed}, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
