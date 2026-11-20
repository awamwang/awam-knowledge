# SPDX-License-Identifier: MIT
"""T20 · 召回基线评测 harness（零依赖，只读语料）——bm25 模式核心移植。

逐字移植自 Node code/eval-recall.js 的 mode=bm25 主路径与 evaluate() 核心（口径勿改）：
- 命中判定 = 预期文件路径是否出现在结果中（不做内容人工判读）
- 文件级（主）: top-N chunk 按文件去重(保留最高分)后再取 top-k
- chunk 级（辅）: 不去重，直接取 top-k chunk
- 指标: recall@k + MRR，两套口径各报一次

M2（hybrid）/ M4（bm25-rerank）等联网模式不在本移植范围（保持只读离线）；
跨引擎等价性由 tests/parity_recall.py 用同一语料跑 Node↔Python 对账。

用法:
    python -m awam_knowledge.eval_recall --root "<graphRoot>" --queries-json "<queries.json>" \
        --topn 50 --k 1,3,5,10 [--title-weight 4] [--md-out "<out.md>"]
    python -m awam_knowledge.eval_recall --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import bm25
from .index_root import validate_index_root
from .bm25 import norm_path

CHUNK_LINES = 20


def evaluate(queries: List[Dict[str, Any]], docs: List[Dict[str, Any]], idx: Dict[str, Any],
             top_n: int, ks: Sequence[int], opts: Dict[str, Any],
             ranker: Optional[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """与 Node evaluate() 逐字对齐：per_query + 两套口径 summary。"""
    per_query: List[Dict[str, Any]] = []
    for q in queries:
        expect_set = {norm_path(p) for p in q["expect"]}
        ranked = ranker(q) if ranker else bm25.rank_all(docs, idx, bm25.query_weights(q["query"], opts))
        chunk_list = ranked[:top_n]
        seen = set()
        file_list = []
        for r in ranked:
            if r["path"] in seen:
                continue
            seen.add(r["path"])
            file_list.append(r)
            if len(file_list) >= top_n:
                break

        def first_hit_rank(lst: List[Dict[str, Any]]) -> Optional[int]:
            for i, r in enumerate(lst):
                if r["path"] in expect_set:
                    return i + 1
            return None

        rec: Dict[str, Any] = {"id": q["id"], "type": q["type"], "query": q["query"],
                               "expect": q["expect"]}
        rec["chunk_rank"] = first_hit_rank(chunk_list)
        rec["file_rank"] = first_hit_rank(file_list)
        rec["chunk_hit"] = {}
        rec["file_hit"] = {}
        for k in ks:
            rec["chunk_hit"]["@" + str(k)] = rec["chunk_rank"] is not None and rec["chunk_rank"] <= k
            rec["file_hit"]["@" + str(k)] = rec["file_rank"] is not None and rec["file_rank"] <= k
        rec["diluted"] = rec["chunk_rank"] != rec["file_rank"]
        per_query.append(rec)

    def mean(a: List[float]) -> float:
        return sum(a) / len(a) if a else 0.0

    def rr(r: Optional[int]) -> float:
        return 0.0 if r is None else 1.0 / r

    summary: Dict[str, Any] = {"n_queries": len(queries), "ks": list(ks), "top_n": top_n}
    for view in ("file", "chunk"):
        s: Dict[str, Any] = {}
        for k in ks:
            passed = sum(1 for p in per_query if p[view + "_hit"]["@" + str(k)])
            s["recall@" + str(k)] = {"count": passed, "total": len(per_query),
                                     "value": round(passed / (len(per_query) or 1), 4)}
        s["MRR"] = round(mean([rr(p[view + "_rank"]) for p in per_query]), 4)
        s["misses"] = [p["id"] for p in per_query if p[view + "_rank"] is None]
        summary[view + "_level"] = s
    summary["diluted_queries"] = [{"id": p["id"], "chunk_rank": p["chunk_rank"],
                                   "file_rank": p["file_rank"]}
                                  for p in per_query if p["diluted"]]
    summary["diluted_count"] = len(summary["diluted_queries"])
    by_type: Dict[str, Dict[str, float]] = {}
    for p in per_query:
        t = by_type.setdefault(p["type"], {"n": 0, "hit1": 0, "hit5": 0, "rr": 0.0})
        t["n"] += 1
        if p["file_hit"]["@1"]:
            t["hit1"] += 1
        if p["file_hit"]["@5"]:
            t["hit5"] += 1
        t["rr"] += rr(p["file_rank"])
    summary["by_type_file_level"] = {}
    for t, v in by_type.items():
        summary["by_type_file_level"][t] = {
            "n": int(v["n"]), "recall@1": round(v["hit1"] / v["n"], 4),
            "recall@5": round(v["hit5"] / v["n"], 4), "MRR": round(v["rr"] / v["n"], 4)}
    return {"perQuery": per_query, "summary": summary}


def to_markdown(meta: Dict[str, Any], res: Dict[str, Any]) -> str:
    """与 Node toMarkdown 相同的表结构（评测报告）。"""
    s = res["summary"]
    L: List[str] = ["# 召回评测 · " + str(meta.get("label", "")), ""]
    L.append("- root: `" + str(meta.get("root", "")) + "` · 语料 "
             + str(meta.get("corpus", {}).get("md_files")) + " md / "
             + str(meta.get("corpus", {}).get("chunks")) + " chunks")
    fl = s["file_level"]
    L.append("- 文件级：" + " · ".join(
        "recall@%s=%s" % (k, fl["recall@" + str(k)]["value"]) for k in s["ks"])
        + " · MRR=" + str(fl["MRR"]))
    cl = s["chunk_level"]
    L.append("- chunk 级：" + " · ".join(
        "recall@%s=%s" % (k, cl["recall@" + str(k)]["value"]) for k in s["ks"])
        + " · MRR=" + str(cl["MRR"]))
    L.append("- 稀释（chunk 与文件级排名不同）：" + str(s["diluted_count"]) + " 条")
    L += ["", "| id | type | query | file_rank | chunk_rank | diluted |",
          "|---|---|---|---|---|---|"]
    for p in res["perQuery"]:
        L.append("| %s | %s | %s | %s | %s | %s |" % (
            p["id"], p["type"], p["query"], p["file_rank"] or "未命中",
            p["chunk_rank"] or "未命中", "是" if p["diluted"] else ""))
    return "\n".join(L)


# ---------------- 自检（Node selftest 的 bm25 核心部分；联网纯函数不在此范围） ----------------

def selftest() -> int:
    lines = lambda n: ["line" + str(i) + " 内容 内容" for i in range(n)]  # noqa: E731
    docs: List[Dict[str, Any]] = []

    def mk(rel: str, title: str, arr: List[str]) -> None:
        for s in range(0, len(arr), CHUNK_LINES):
            d = bm25._make_doc(rel, title, arr, s, CHUNK_LINES, bm25.DEFAULT_TITLE_WEIGHT, False)
            if d:
                docs.append(d)

    mk("pages/alpha.md", "alpha", lines(45))
    mk("pages/beta.md", "beta", lines(10))
    mk("pages/gamma.md", "gamma", lines(5))
    docs[0]["tf"]["uniqueterm"] = docs[0]["tf"].get("uniqueterm", 0) + 5
    docs[0]["len"] += 5
    docs[3]["tf"]["uniqueterm"] = docs[3]["tf"].get("uniqueterm", 0) + 5
    docs[3]["len"] += 5
    docs[4]["tf"]["otherterm"] = docs[4]["tf"].get("otherterm", 0) + 5
    docs[4]["len"] += 5
    idx = bm25.build_index(docs)
    queries = [
        {"id": "S1", "type": "selftest", "query": "uniqueterm",
         "expect": ["pages/alpha.md", "pages/beta.md"]},
        {"id": "S2", "type": "selftest", "query": "otherterm", "expect": ["pages/gamma.md"]},
        {"id": "S3", "type": "selftest", "query": "nonexistentzzz", "expect": ["pages/beta.md"]},
    ]
    res = evaluate(queries, docs, idx, 50, [1, 3, 5, 10], {"stem": False})
    checks: List[tuple] = [
        ("S1 文件级有命中", res["perQuery"][0]["file_rank"] is not None),
        ("S2 文件级命中第1", res["perQuery"][1]["file_rank"] == 1),
        ("S3 未命中", res["perQuery"][2]["file_rank"] is None),
        ("recall@5 有值", isinstance(res["summary"]["file_level"]["recall@5"]["value"], (int, float))),
        ("MRR 在(0,1]", 0 < res["summary"]["file_level"]["MRR"] <= 1),
        ("词形规整 stemAscii(vectors)=vector", bm25.stem_ascii("vectors") == "vector"),
        ("词形规整 stemAscii(running)=runn", bm25.stem_ascii("running") == "runn"),
    ]
    failed = [name for name, ok in checks if not ok]
    print(json.dumps({
        "selftest": True, "docs": len(docs), "checks": len(checks),
        "pass": not failed, "failed": failed,
        "summary": res["summary"],
        "per_query": [{"id": p["id"], "file_rank": p["file_rank"], "chunk_rank": p["chunk_rank"]}
                      for p in res["perQuery"]],
    }, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="召回基线评测（bm25 模式，零依赖只读）")
    ap.add_argument("--root", default="")
    ap.add_argument("--queries-json", dest="queries_json", default="")
    ap.add_argument("--topn", type=int, default=50)
    ap.add_argument("--k", default="1,3,5,10")
    ap.add_argument("--title-weight", dest="title_weight", type=int, default=4)
    ap.add_argument("--exclude", default=bm25.DEFAULT_EXCLUDES)
    ap.add_argument("--stem", action="store_true")
    ap.add_argument("--md-out", dest="md_out", default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.queries_json:
        print("需要 --queries-json <path>（或用 --selftest）", file=sys.stderr)
        return 1
    v = validate_index_root(args.root)
    if not v.get("ok"):
        print("index root 无效: " + json.dumps(v, ensure_ascii=False), file=sys.stderr)
        return 2
    root = v["root"]
    queries = json.loads(Path(args.queries_json).read_text(encoding="utf-8"))
    ks = [int(x) for x in args.k.split(",") if int(x) > 0]
    excludes = bm25.parse_excludes(args.exclude)
    built = bm25.build_corpus({"root": root, "excludes": excludes, "chunkLines": CHUNK_LINES,
                               "titleWeight": args.title_weight, "stem": args.stem})
    idx = bm25.build_index(built["docs"])
    res = evaluate(queries, built["docs"], idx, args.topn, ks, {"stem": args.stem})
    md_files = len({d["path"] for d in built["docs"]})
    meta = {
        "label": "M1 · BM25-only" if not args.stem else "M3a · BM25 + 词形规整",
        "mode": "bm25" if not args.stem else "m3", "stem": args.stem, "dict": "",
        "expand_weight": None, "title_weight": args.title_weight,
        "root": root, "chunk_lines": CHUNK_LINES, "top_n": args.topn, "ks": ks,
        "excludes": excludes,
        "corpus": {"md_files": md_files, "chunks": len(built["docs"]),
                   "vocab": len(idx["df"]), "bytes": built["totalBytes"],
                   "lines": built["totalLines"], "read_failures": len(built["failed"])},
        "recall_default": "bm25-title4",
    }
    md = to_markdown(meta, res)
    if args.md_out:
        Path(args.md_out).write_text(md, encoding="utf-8")
    out = {**meta, **res["summary"], "per_query": res["perQuery"],
           "markdown": ("(written to " + args.md_out + ")") if args.md_out else md}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
