# SPDX-License-Identifier: MIT
"""密钥来源（写入闸共享密钥）——逐字移植自 Node code/secret-provider.js。

优先级：AWAM_SECRET（直接给值）→ AWAM_SECRET_CMD（外部命令，如 Bitwarden CLI）
→ <root>/secret.key（首次生成；POSIX 下 0600，Windows 上 chmod 不生效，只依赖状态目录 ACL）。
签发端（companion）与校验端（mcp_server）必须走同一套解析，否则长度/空白处理不一致会让闸失效。
命令继承当前进程环境，因此 bw 的 BW_SESSION 直接透传。
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict, Optional

MIN_LEN = 16
ANSI_RE = re.compile(r"\u001b\[[0-9;?]*[A-Za-z]")

# 未解锁的 bw 会先弹交互式密码提示，stdin 已置 ignore，故它会立即退出而不是挂住；
# 但 stderr 里混着 ANSI 控制序列与提示语，需要清掉并翻译成可操作的说明。
_HINTS = [
    (re.compile(r"master password|vault is locked", re.IGNORECASE),
     "vault 未解锁：先 `bw unlock --raw` 解锁，并把 BW_SESSION 放进 server 进程的环境"),
    (re.compile(r"not found", re.IGNORECASE),
     "Bitwarden 里没有该条目：检查 AWAM_SECRET_CMD 中的条目名"),
]


def _classify(msg: str) -> str:
    for pat, hint in _HINTS:
        if pat.search(msg):
            return "（" + hint + "）"
    return ""


def check_secret(raw: Any, source: str) -> str:
    s = str("" if raw is None else raw).strip()
    if not s:
        raise ValueError(source + " 输出为空")
    if len(s) < MIN_LEN:
        raise ValueError(source + " 至少 " + str(MIN_LEN) + " 字符（当前 " + str(len(s)) + "）")
    return s


def _run_command(cmd: str) -> str:
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, timeout=15,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise ValueError("AWAM_SECRET_CMD 执行失败（超时）：" + _classify(str(e))) from e
    if out.returncode != 0:
        stderr = ANSI_RE.sub("", out.stderr.decode("utf-8", "replace")).strip()
        first = next((ln.strip() for ln in stderr.splitlines() if ln.strip()),
                     "unknown error")
        raise ValueError("AWAM_SECRET_CMD 执行失败（退出码 " + str(out.returncode)
                         + "）：" + first + _classify(first))
    return out.stdout.decode("utf-8", "replace")


def _read_file(abs_path: str, create: bool) -> str:
    if os.path.exists(abs_path):
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                return check_secret(f.read(), "secret.key")
        except ValueError as e:
            raise ValueError(str(e) + "，请删除 " + abs_path
                             + " 后重启，或改用 AWAM_SECRET") from e
    if not create:
        raise ValueError("secret_missing：未配置 AWAM_SECRET / AWAM_SECRET_CMD，且 "
                         + abs_path + " 不存在")
    import secrets as _secrets
    secret = _secrets.token_hex(32)
    fd = os.open(abs_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secret)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return secret


def resolve_secret(opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    o = opts or {}
    env = o.get("env") or os.environ
    if env.get("AWAM_SECRET") not in (None, ""):
        return {"secret": check_secret(env.get("AWAM_SECRET"), "AWAM_SECRET"), "source": "env"}
    if env.get("AWAM_SECRET_CMD"):
        return {"secret": check_secret(_run_command(env["AWAM_SECRET_CMD"]), "AWAM_SECRET_CMD"),
                "source": "cmd"}
    root = o.get("root") or env.get("AWAM_STATE_DIR") or env.get("SPIKE_ROOT") \
        or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    abs_path = os.path.join(root, "secret.key")
    return {"secret": _read_file(abs_path, o.get("create") is not False),
            "source": "file", "path": abs_path}
