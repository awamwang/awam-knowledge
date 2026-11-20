# SPDX-License-Identifier: MIT
"""存储层自检：fs_util / store / index_root 关键行为。运行：python tests/storage_check.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from awam_knowledge import fs_util, store, index_root  # noqa: E402


def check_fs_util() -> None:
    d = tempfile.mkdtemp()
    f = os.path.join(d, "sub", "a.json")
    n = fs_util.write_json_atomic(f, {"k": "中文"})
    assert fs_util.read_json(f) == {"k": "中文"}, "json roundtrip"
    assert n > 0 and not any(x.endswith(".tmp-") for x in os.listdir(os.path.dirname(f))), "no tmp residue"
    jl = os.path.join(d, "b.jsonl")
    fs_util.append_jsonl(jl, {"i": 1})
    fs_util.append_jsonl(jl, {"i": 2})
    assert fs_util.read_jsonl(jl) == [{"i": 1}, {"i": 2}], "jsonl roundtrip"
    lk = fs_util.acquire_lock(d, "w")
    assert lk["ok"], "first lock"
    lk2 = fs_util.acquire_lock(d, "w")
    assert not lk2["ok"] and lk2["error"] == "locked", "second lock refused while held"
    lk["release"]()
    lk3 = fs_util.acquire_lock(d, "w")
    assert lk3["ok"], "reacquire after release"
    lk3["release"]()
    print("  ok fs_util")


def check_store() -> None:
    d = tempfile.mkdtemp()
    s = store.open_store(d)
    s.record_job({"job_id": "j1", "status": "running", "done": 0, "total": 5})
    s.record_token("t-abc")
    s.record_undo({"proposal_id": "p1"})
    s.audit({"event": "start", "job_id": "j1"})
    # reopen：running → interrupted
    s2 = store.open_store(d)
    assert s2.interrupted == ["j1"], f"interrupted: {s2.interrupted}"
    assert s2.state["jobs"]["j1"]["status"] == "interrupted"
    assert "t-abc" in s2.state["usedTokens"]
    assert len(s2.state["undo"]) == 1
    a = s2.read_audit()
    assert a["total"] == 1 and a["entries"][0]["event"] == "start", a
    print("  ok store")


def check_index_root() -> None:
    d = tempfile.mkdtemp()
    raw = os.path.join(d, "realsource")
    os.makedirs(raw)
    os.environ["KM_RAW_ROOTS"] = raw
    v = index_root.validate_index_root(raw, allow_raw=False)
    assert not v["ok"] and v["reason"] == "raw_source", v
    v2 = index_root.validate_index_root(raw, allow_raw=True)
    assert v2["ok"] and v2["root"] == raw, v2
    v3 = index_root.validate_index_root("")  # 无显式、无 KM_DATA_ROOT 环境（可能 settings 有）
    assert ("ok" in v3)
    # normalize_path 行为
    assert index_root.normalize_path("C:\\a\\b\\") == "c:/a/b"
    assert index_root.normalize_path("/x//y/") == "/x/y"
    del os.environ["KM_RAW_ROOTS"]
    print("  ok index_root")


def check_pid_alive() -> None:
    import subprocess
    import sys
    import time as _t
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        deadline = _t.time() + 10
        while fs_util.pid_alive(child.pid) is not True:
            assert _t.time() < deadline, "子进程启动后 pid_alive 应为 True"
            _t.sleep(0.05)
        child.kill()
        child.wait(timeout=10)
        # 进程已终止但 Popen 句柄仍被本进程持有：Windows 上必须判死（GetExitCodeProcess 口径）
        assert fs_util.pid_alive(child.pid) is False, "已终止（句柄未释放）的进程 pid_alive 应为 False"
    finally:
        child.poll()
    print("  ok pid_alive")


if __name__ == "__main__":
    check_fs_util()
    check_store()
    check_index_root()
    check_pid_alive()
    print("STORAGE CHECK OK")
