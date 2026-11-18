---
topics: [recall, rerank, preregistration]
doc_kind: note
created: 2026-11-07
---

# T35 · BM25 + Rerank 预注册（先定后跑）

> **纪律**：本文件写于看结果之前。跑后不改设置、不做参数择优；多组结果全列。  
> **对照基线**：`mode=bm25` + `--title-weight 4`（D1 词法基准）。  
> **不得**在未跑完前写成「已强于关键字」。

## 假设（待证）

在中文为主的 Logseq 语料上，**BM25（标题×4）召回 top-N 后再用 Jina Reranker v2 精排**，能否在文件级主口径上相对同权重 BM25 提升 recall@1 / MRR（至少不伤 @1）。

## 固定设置

| 项 | 值 |
|---|---|
| 语料 | 与既有基线相同（Logseq-only；真实 query 集仓外 26 条） |
| 切块 | 20 行/块 |
| 标题权重 | **4**（两边相同） |
| 召回 | BM25 → top-N=`50` 送精排（`--rerank-candidates` 默认=topn） |
| 送排截断 | 每条候选正文 **`--rerank-doc-chars 512`**（管路护栏：本机服务在长文×50 时 HTTP 500；非指标择优） |
| 精排模型 | `jina-reranker-v2-base-multilingual` |
| 精排端点 | 运行时 `--rerank-endpoint` / `RERANK_ENDPOINT`（不写入本文件） |
| 指标 | 文件级（主）+ chunk 级（辅）：recall@1/3/5/10 + MRR |
| 次数 | **单次运行**；失败重试仅针对 HTTP 瞬时错误（harness 内 retries） |
| 对照 | 同机同语料先跑 `mode=bm25 --title-weight 4`，再跑 `mode=bm25-rerank` |

## 判定（跑前写死）

- **取胜**：文件级 recall@1 **严格高于** 同次对照的 BM25，且 MRR 不低于 BM25。  
- **非劣**：@1 持平且 @5 或 MRR 严格高于 BM25 → 记「非劣 / 未强于 @1」，**不**称强于关键字。  
- **未胜**：其余情况。  
- 小样本（26 条）差异不宜过度解读；跨语言子集可另表，但不单独改判定。

## 复现命令（占位）

```bash
# 1) 词法基线
node code/eval-recall.js --mode bm25 --title-weight 4 \
  --queries-json "<private-queries>" --topn 50 --md-out out-t35-bm25.md > out-t35-bm25.json

# 2) BM25 + Rerank
node code/eval-recall.js --mode bm25-rerank --title-weight 4 \
  --rerank-endpoint "$RERANK_ENDPOINT" \
  --rerank-model jina-reranker-v2-base-multilingual \
  --queries-json "<private-queries>" --topn 50 --md-out out-t35-rerank.md > out-t35-rerank.json
```

## 跑后填写

- 跑完日期：2026-11-07
- 语料：`G:/Knowledge/data/logseq-main`（434 md / 761 chunk；相对历史 428 md 略增）
- BM25 文件级 @1 / @5 / MRR：**0.6923 / 0.8077 / 0.7355**
- Rerank 文件级 @1 / @5 / MRR：**0.2692 / 0.4231 / 0.3584**
- 判定（取胜 / 非劣 / 未胜）：**未胜**（@1 与 MRR 全面落后；16/26 条文件级名次变差，无一条变好到足以抬 @1）
- 备注：首跑 candidates=50 无截断时 Q04 触发服务端 HTTP 500；改 `--rerank-doc-chars 512` 后整集跑通（约 400s，CPU）。跨语言未命中仍为 Q13/Q15/Q16（精排未救回）。证据：`awam-knowledge.private/research/evidence/out-t35-*.json`
