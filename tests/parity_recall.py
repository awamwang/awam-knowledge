# SPDX-License-Identifier: MIT
"""M2b · 全语料召回回归 oracle：Node↔Python 双引擎对账。

同一语料根 + 同一查询集，分别跑 Node code/eval-recall.js（bm25 模式）与
python -m awam_knowledge.eval_recall，逐 query 对比 file_rank/chunk_rank/diluted 与
file/chunk 两级 recall@k + MRR；不一致即失败（评测口径移植等价性）。

用法:
    python tests/parity_recall.py [--root "<dir>"] [--queries "<queries.json>"]
root/queries 缺省用 index_root 解析的默认根与 code/queries-t18.json。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run_node(root: str, queries: Path) -> dict:
    p = subprocess.run(
        ["node", str(REPO / "code" / "eval-recall.js"), "--root", root,
         "--queries-json", str(queries), "--topn", "50", "--k", "1,3,5,10"],
        capture_output=True, cwd=str(REPO), timeout=300)
    if p.returncode != 0:
        raise RuntimeError("node eval-recall 失败: " + p.stderr.decode("utf-8", "replace")[:500])
    return json.loads(p.stdout.decode("utf-8"))


def run_python(root: str, queries: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, "-m", "awam_knowledge.eval_recall", "--root", root,
         "--queries-json", str(queries), "--topn", "50", "--k", "1,3,5,10"],
        capture_output=True, env=env, cwd=str(REPO), timeout=300)
    if p.returncode != 0:
        raise RuntimeError("python eval_recall 失败: " + p.stderr.decode("utf-8", "replace")[:500])
    return json.loads(p.stdout.decode("utf-8"))


def compare(n: dict, p: dict) -> list:
    diffs: list = []
    if len(n["per_query"]) != len(p["per_query"]):
        diffs.append("per_query 数量不一致: %d vs %d" % (len(n["per_query"]), len(p["per_query"])))
    for a, b in zip(n["per_query"], p["per_query"]):
        for k in ("id", "file_rank", "chunk_rank", "diluted"):
            if a.get(k) != b.get(k):
                diffs.append("%s.%s: node=%r py=%r" % (a.get("id"), k, a.get(k), b.get(k)))
        for k in ("@1", "@3", "@5", "@10"):
            if a["file_hit"][k] != b["file_hit"][k]:
                diffs.append("%s.file_hit%s" % (a["id"], k))
            if a["chunk_hit"][k] != b["chunk_hit"][k]:
                diffs.append("%s.chunk_hit%s" % (a["id"], k))
    for view in ("file_level", "chunk_level"):
        for k in ("recall@1", "recall@3", "recall@5", "recall@10"):
            nv, pv = n[view][k]["value"], p[view][k]["value"]
            if abs(nv - pv) > 1e-9:
                diffs.append("%s.%s: node=%r py=%r" % (view, k, nv, pv))
        if abs(n[view]["MRR"] - p[view]["MRR"]) > 1e-9:
            diffs.append("%s.MRR: node=%r py=%r" % (view, n[view]["MRR"], p[view]["MRR"]))
        if n[view]["misses"] != p[view]["misses"]:
            diffs.append("%s.misses: node=%r py=%r" % (view, n[view]["misses"], p[view]["misses"]))
    if n["corpus"]["md_files"] != p["corpus"]["md_files"]:
        diffs.append("corpus.md_files: node=%r py=%r" % (n["corpus"]["md_files"], p["corpus"]["md_files"]))
    if n["corpus"]["chunks"] != p["corpus"]["chunks"]:
        diffs.append("corpus.chunks: node=%r py=%r" % (n["corpus"]["chunks"], p["corpus"]["chunks"]))
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--queries", default="")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO / "src"))
    from awam_knowledge.index_root import validate_index_root
    v = validate_index_root(args.root)
    if not v.get("ok"):
        print("index root 无效:", json.dumps(v, ensure_ascii=False))
        return 2
    root = v["root"]
    queries = Path(args.queries) if args.queries else REPO / "code" / "queries-t18.json"

    print("root =", root)
    print("queries =", queries.name)
    n = run_node(root, queries)
    p = run_python(root, queries)
    diffs = compare(n, p)
    print("语料 md_files: node=%s py=%s · chunks: node=%s py=%s" % (
        n["corpus"]["md_files"], p["corpus"]["md_files"],
        n["corpus"]["chunks"], p["corpus"]["chunks"]))
    print("file_level: recall@5 node=%s py=%s · MRR node=%s py=%s · diluted %s/%s" % (
        n["file_level"]["recall@5"]["value"], p["file_level"]["recall@5"]["value"],
        n["file_level"]["MRR"], p["file_level"]["MRR"],
        n["diluted_count"], p["diluted_count"]))
    print("DIFFS:", len(diffs))
    for d in diffs[:20]:
        print("  ", d)
    print("VERDICT:", "IDENTICAL" if not diffs else "MISMATCH")
    return 0 if not diffs else 1


if __name__ == "__main__":
    sys.exit(main())
