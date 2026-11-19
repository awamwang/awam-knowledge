# SPDX-License-Identifier: MIT
"""文件与进程工具（stdlib 零依赖）。逐字移植自 Node code/fs-util.js：原子写、JSONL、目录锁。"""
from __future__ import annotations

import errno
import json
import os
import secrets
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union


def ensure_dir(d: Union[str, os.PathLike]) -> None:
    Path(d).mkdir(parents=True, exist_ok=True)


def atomic_write(file: Union[str, os.PathLike], data: Union[str, bytes]) -> int:
    file = str(file)
    ensure_dir(os.path.dirname(file))
    tmp = f"{file}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    buf = data if isinstance(data, bytes) else str(data).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, buf)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, file)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(buf)


def write_json_atomic(file: Union[str, os.PathLike], obj: Any) -> int:
    return atomic_write(file, json.dumps(obj, indent=2, ensure_ascii=False))


def read_json(file: Union[str, os.PathLike], dflt: Any = None) -> Any:
    try:
        return json.loads(Path(file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dflt


def append_jsonl(file: Union[str, os.PathLike], obj: Any) -> None:
    ensure_dir(os.path.dirname(str(file)))
    fd = os.open(str(file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(file: Union[str, os.PathLike]) -> List[Any]:
    try:
        text = Path(file).read_text(encoding="utf-8")
    except OSError:
        return []
    out: List[Any] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def acquire_lock(dir_: Union[str, os.PathLike], name: str,
                 stale_ms: int = 10 * 60 * 1000) -> Dict[str, Any]:
    """单写者锁：文件存在且持有者 pid 存活且未过期时不抢。返回 dict(ok, file?, token?, holder?, error?, release?)。"""
    import time
    ensure_dir(dir_)
    file = os.path.join(str(dir_), name + ".lock")
    token = secrets.token_hex(8)
    now_ms = lambda: int(time.time() * 1000)
    for _attempt in range(2):
        try:
            fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, json.dumps({"pid": os.getpid(), "token": token, "at": now_ms()}).encode("utf-8"))
            finally:
                os.close(fd)

            def release() -> None:
                try:
                    cur = json.loads(Path(file).read_text(encoding="utf-8"))
                    if cur and cur.get("token") == token:
                        os.unlink(file)
                except (OSError, ValueError):
                    pass

            return {"ok": True, "file": file, "token": token, "release": release}
        except OSError as e:
            if e.errno != errno.EEXIST:
                return {"ok": False, "error": e.__class__.__name__}
            cur = None
            try:
                cur = json.loads(Path(file).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cur = None
            holder_pid = cur.get("pid") if cur else None
            stale = (not cur) or (not holder_pid) or (now_ms() - (cur.get("at") or 0) > stale_ms) or (not pid_alive(holder_pid))
            if not stale:
                return {"ok": False, "error": "locked", "holder": cur}
            try:
                os.unlink(file)
            except OSError:
                pass
    return {"ok": False, "error": "locked"}
