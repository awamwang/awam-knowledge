# SPDX-License-Identifier: MIT
"""M1 等价校验：同一 fixture / 同一 query，现有 Node bm25-index.js 与 Python 版逐项比对。

证明「Node→Python 移植在分词与 BM25 排序上数值等价」——整个移植工程的地基与风险点。
运行：python tests/parity/check_parity.py
退出码 0=等价，非 0=有差异（并打印首个差异）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"
sys.path.insert(0, str(SRC))

from awam_knowledge import bm25  # noqa: E402

NODE = os.environ.get("NODE", "node")
FIXTURE = HERE / "fixture"
STEM = os.environ.get("PARITY_STEM", "0") == "1"


def run_node(args):
    out = subprocess.run([NODE, str(HERE / "node_runner.js"), *args],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(f"node failed rc={out.returncode}: {out.stderr}")
    return json.loads(out.stdout)


TOKEN_SAMPLES = [
    "知识库索引 BM25 retrieval 召回",
    "running  runs  studies  boxes  classes  cities",
    "logseq 的 journal/page 块引用 [REDACTED:key]",
    "Hello World 123 _priv  中文单字 漢",
    "",
    "全角ＡＢＣ ascii123",
    "A. Node.js v22 与 CPython 3.12",
]


def check_tokens():
    for s in TOKEN_SAMPLES:
        expect = run_node(["tokens", "1" if STEM else "0", s])
        got = bm25.tokenize(s, STEM)
        if expect != got:
            print(f"[TOKEN DIFF] text={s!r}\n  node={expect}\n  py  ={got}")
            return False
    return True


def check_search():
    queries = ["知识库 索引", "logseq 召回", "BM25 ranking retrieval", "单字 漢"]
    for q in queries:
        expect = run_node(["search", str(FIXTURE), "1" if STEM else "0", q])
        corpus = bm25.build_corpus({"root": str(FIXTURE), "stem": STEM})
        idx = bm25.build_index(corpus["docs"])
        w = bm25.query_weights(q, {"stem": STEM})
        ranked = bm25.rank_all(corpus["docs"], idx, w)
        got = [{"path": r["path"], "line": r["lineStart"], "score": r["score"]} for r in ranked]
        if len(expect) != len(got):
            print(f"[LEN DIFF] q={q!r} node={len(expect)} py={len(got)}")
            return False
        for i, (e, g) in enumerate(zip(expect, got)):
            if e["path"] != g["path"] or e["line"] != g["line"]:
                print(f"[ORDER DIFF] q={q!r} #{i} node={e['path']}:{e['line']} py={g['path']}:{g['line']}")
                return False
            if abs(e["score"] - g["score"]) > 1e-9:
                print(f"[SCORE DIFF] q={q!r} #{i} {e['path']} node={e['score']!r} py={g['score']!r} d={abs(e['score']-g['score']):g}")
                return False
        print(f"  ok q={q!r}: {len(got)} 命中，排序与分数一致")
    return True


def main() -> int:
    print(f"STEM={STEM}")
    if not check_tokens():
        return 1
    print("  ok tokenize 全部样本一致")
    if not check_search():
        return 1
    print("PARITY OK：Node ↔ Python bm25 等价")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
