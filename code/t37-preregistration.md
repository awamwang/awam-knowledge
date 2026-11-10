---
topics: [recall, rerank, embedding, preregistration, gate, qwen]
doc_kind: note
created: 2026-11-10
---

# T37 · Qwen3.7 精排 / 向量对照预注册（先定后跑）

> 写于看结果之前。跑后不改设置、不做参数择优；多组结果全列。  
> 对照基线：同机同语料再跑 `mode=bm25 --title-weight 4`（不得直接复用旧数字当本轮对照，除非语料/query 未变且同次会话已重跑并核验一致）。  
> **不得**在未跑完前写成「已强于关键字」。

## 动机

1. T35：`jina-reranker-v2-base-multilingual` **全量**精排全面落后（@1 0.6923→0.2692）——高置信中文词法命中被打散。  
2. T36：同 Jina 模型下 `xlang` 闸门与基线打平但未救回跨语言；`lowconf` 拉低 @1/MRR。  
3. 公开榜与型号代际上，`qwen3.7-text-rerank` / Qwen3 Embedding 系相对 Jina v2 / 本地 bge·jina-768 **预期更强**；本轮用同一套 26 条 query 验证是否落到本语料。

## 范围与优先级（先定）

| 优先级 | 臂 | 是否本轮必跑 |
|---|---|---|
| **P0** | BM25 基线（标题×4） | 必跑 |
| **P1** | BM25 + `qwen3.7-text-rerank`，闸门 **仅 `xlang`** | 必跑 |
| **P2**（可选） | 同模型，闸门 `xlang,lowconf` | 仅当 P1 取胜或非劣后再跑；**禁止**本轮先做全量 `all` |
| **P3**（可选、低优） | hybrid + `qwen3.7-text-embedding-flash`，表面路由（纯 ASCII→向量） | 与精排独立；不阻塞 P1 判定 |

本轮**不跑**：全量精排（`--rerank-gate all`）；不以 flash 对全中文 query 做无闸门 hybrid。

## 固定设置

### 公共

| 项 | 值 |
|---|---|
| 语料 | 与 T35/T36 相同 Logseq-only（真实 query 集仓外 26 条） |
| 切块 | 20 行/块 |
| 标题权重 | **4**（所有臂相同） |
| 指标 | 文件级（主）+ chunk 级（辅）：recall@1/3/5/10 + MRR |
| 次数 | **每臂单次运行**；失败重试仅针对 HTTP 瞬时错误 |
| 隐私 | 百炼云 API：仅对评测副本/脱敏 query；凭证只经环境变量，不落盘、不写入本文件 |

### P1 / P2 · Rerank

| 项 | 值 |
|---|---|
| 召回 | BM25 → top-N=`50` 送精排 |
| 送排截断 | `--rerank-doc-chars 0`（不截；模型单条上限约 30k tokens）。若端点因过长失败：记录失败原因后**仅**改为 `2048` 重跑该臂一次，并在跑后备注写明；不得为抬指标再调 |
| 精排模型 | `qwen3.7-text-rerank` |
| 精排端点 | 运行时 `--rerank-endpoint` / `RERANK_ENDPOINT`（不写入本文件） |
| 闸门 P1 | `--rerank-gate xlang` |
| 闸门 P2 | `--rerank-gate xlang,lowconf`（阈值仍 **15**，与 T36 相同、不改） |

### P3 · Embedding（可选）

| 项 | 值 |
|---|---|
| 模式 | `--mode hybrid --route surface`（纯 ASCII→向量，含汉字→BM25） |
| 模型 | `qwen3.7-text-embedding-flash`（默认维 1024；不另调 dimensions） |
| 端点 | `--embed-endpoint` / `EMBED_ENDPOINT`（OpenAI 兼容） |
| 融合 | 沿用 harness 默认 RRF（不调 lex/vec 权重） |
| 说明 | flash 为成本向；若 P3 取胜且要追质量，另开轮次换非 flash 的 `qwen3.7-text-embedding`，**本文件不扩** |

## 判定（跑前写死）

相对**本轮同次** BM25（标题×4）：

- **取胜**：文件级 recall@1 **严格高于** 基线，且 MRR 不低于基线。  
- **非劣**：@1 持平，且 @5 或 MRR 严格高于基线 → 记「非劣 / 未强于 @1」，**不**称强于关键字。  
- **未胜**：其余。

总判定以 **P1** 为准；P2/P3 全列，不单独改写「本轮是否值得默认开启」。  
另表：闸门命中子集（预期 Q13–Q16）相对 BM25 的名次升/降/不变；跨语言子集**不**单独改总判定。  
小样本（26 条）差异不宜过度解读。

## 复现命令（占位）

```bash
# P0 · 词法基线
node code/eval-recall.js --mode bm25 --title-weight 4 \
  --queries-json "<private-queries>" --topn 50 \
  --md-out out-t37-bm25.md > out-t37-bm25.json

# P1 · BM25 + Qwen rerank（仅跨语言闸门）
node code/eval-recall.js --mode bm25-rerank --title-weight 4 \
  --rerank-endpoint "$RERANK_ENDPOINT" \
  --rerank-model qwen3.7-text-rerank \
  --rerank-gate xlang --rerank-doc-chars 0 \
  --queries-json "<private-queries>" --topn 50 \
  --md-out out-t37-rerank-xlang.md > out-t37-rerank-xlang.json

# P2 · 可选：xlang + lowconf（仅 P1 取胜/非劣后）
node code/eval-recall.js --mode bm25-rerank --title-weight 4 \
  --rerank-endpoint "$RERANK_ENDPOINT" \
  --rerank-model qwen3.7-text-rerank \
  --rerank-gate xlang,lowconf --rerank-doc-chars 0 \
  --queries-json "<private-queries>" --topn 50 \
  --md-out out-t37-rerank-xlang-lowconf.md > out-t37-rerank-xlang-lowconf.json

# P3 · 可选：hybrid 表面路由 + embedding-flash
node code/eval-recall.js --mode hybrid --title-weight 4 --route surface \
  --embed-endpoint "$EMBED_ENDPOINT" \
  --embed-model qwen3.7-text-embedding-flash \
  --queries-json "<private-queries>" --topn 50 \
  --md-out out-t37-hybrid-flash-route.md > out-t37-hybrid-flash-route.json
```

## 跑后填写

- 跑完日期：
- 语料路径 / md 数 / chunk 数：
- P0 BM25 文件级 @1 / @5 / MRR：
- P1 xlang @1 / @5 / MRR / 精排条数 / 跨语言子集名次变化：
- P2（若跑）@1 / @5 / MRR：
- P3（若跑）@1 / @5 / MRR：
- 总判定（取胜 / 非劣 / 未胜）：
- 备注（含 doc-chars 是否被迫改为 2048、端点错误等）：
- 证据：`awam-knowledge.private/research/evidence/out-t37-*.json`
