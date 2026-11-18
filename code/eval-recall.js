'use strict';
/**
 * T20 · 召回基线评测 harness（零依赖，只读语料）
 *
 * 用法:
 *   # M1 · BM25-only
 *   node eval-recall.js --root "<graphRoot>" --queries-json "<queries.json>" --topn 50 --k 1,3,5,10 --md-out "<out.md>"
 *   # M3a · BM25 + 词形规整（规则型，领域无关）
 *   node eval-recall.js --mode m3 --stem ...
 *   # M3b · BM25 + 语料无监督同义扩展
 *   node eval-recall.js --mode m3 --dict "<dict.json>" --expand-weight 0.35 ...
 *   # M3  · 两者组合
 *   node eval-recall.js --mode m3 --stem --dict "<dict.json>" ...
 *   # M2 · hybrid（BM25 + 向量 RRF 融合）—— 需 OpenAI 兼容 embedding 服务（本地/局域网/线上）
 *   node eval-recall.js --mode hybrid --root "<graphRoot>" --queries-json "<q.json>" \
 *        --embed-endpoint "https://api.example.com/v1/embeddings" \
 *        --embed-model "text-embedding-3-large" \
 *        --embed-cache "<cache.json>" --rrf-k 60 \
 *        --topn 50 --k 1,3,5,10 --md-out "<out.md>"
 *   # 线上服务需鉴权时：把 key 放进环境变量（默认变量名 EMBED_API_KEY），
 *   #   脚本只从 env 读取并置于 Authorization: Bearer 头，**绝不写入任何文件/日志/缓存**。
 *   #   EMBED_API_KEY=****** node eval-recall.js --mode hybrid --embed-api-key-env EMBED_API_KEY ...
 *   # 服务不可达时：退出码 2 + status=unavailable（绝不产出任何数字）
 *   # M4 · BM25 召回 + Rerank 精排（非默认；对照基线 = mode=bm25 标题×4）
 *   node eval-recall.js --mode bm25-rerank --root "<graphRoot>" --queries-json "<q.json>" \
 *        --rerank-endpoint "http://192.168.x.x:8001/v1/rerank" \
 *        --rerank-model "jina-reranker-v2-base-multilingual" \
 *        --topn 50 --title-weight 4 --k 1,3,5,10 --md-out "<out.md>"
 *   # 亦可设 RERANK_ENDPOINT / RERANK_MODEL；可选 RERANK_API_KEY（仅内存、不落盘）
 *   # 端点未配置或不可达：退出码 2 + status=unavailable（绝不产出任何数字）
 *   # 自检
 *   node eval-recall.js --selftest
 *
 * 口径（见 09-query-set-draft.md §1）:
 *   - 命中判定 = 预期文件路径是否出现在结果中（不做内容人工判读）
 *   - 文件级（主）: top-N chunk 按文件去重(保留最高分)后再取 top-k
 *   - chunk 级（辅）: 不去重，直接取 top-k chunk —— 量化"长文件多 chunk 占位稀释"
 *   - 指标: recall@k (k∈{1,3,5,10}) + MRR，两套口径各报一次
 *
 * 边界: 只读源数据；不写/不改用户文件。mode=hybrid / bm25-rerank 端点缺失时不产出任何数字。
 *
 * 默认召回（评测结论）: mode=bm25 + 标题×4；hybrid / bm25-rerank 仅显式开启，非默认。
 * 若 ROOT 下存在 index.json（脱敏仓 build_index 旁路产物），用于文档清单与标题加权。
 */
const fs = require('fs');
const path = require('path');
const { resolveIndexRoot } = require('./index-root');
const bm = require('./bm25-index');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] !== undefined ? process.argv[i + 1] : dflt;
}
const SELFTEST = process.argv.includes('--selftest');
const MODE = arg('mode', 'bm25');                 // bm25 | m3 | hybrid | bm25-rerank
const STEM = process.argv.includes('--stem');     // 词形规整（规则型）
const DICT_PATH = arg('dict', '');                // 语料无监督扩展词典
const EXPW = Number(arg('expand-weight', '0.35'));// 扩展词权重
const ROOT = SELFTEST ? '' : resolveIndexRoot(arg('root', '')); // 自检不需要语料根
const QJSON = arg('queries-json', '');
const TOPN = Number(arg('topn', '50'));
const KS = arg('k', '1,3,5,10').split(',').map(Number).filter(n => n > 0);
const CHUNK_LINES = 20;
const EXCLUDES = bm.parseExcludes(arg('exclude', bm.DEFAULT_EXCLUDES));
const WRITE_MD = arg('md-out', '');

// ---- M2 · hybrid 相关参数 ----
const EMBED_ENDPOINT = arg('embed-endpoint', process.env.EMBED_ENDPOINT || '');
const EMBED_MODEL = arg('embed-model', process.env.EMBED_MODEL || 'BAAI/bge-small-zh-v1.5');
const EMBED_CACHE = arg('embed-cache', path.join(__dirname, 'embed-cache.json'));
const EMBED_BATCH = Number(arg('embed-batch', '32'));
const EMBED_CONCURRENCY = Number(arg('embed-concurrency', '6')); // 并发在飞的批数（远端服务逐条较慢时提速）
const EMBED_RETRIES = Number(arg('embed-retries', '3'));         // 单批失败重试次数
const EMBED_TIMEOUT = Number(arg('embed-timeout', '60000'));
const RRF_K = Number(arg('rrf-k', '60'));
// ---- T33 · 融合策略相关参数（预注册对照：加权 RRF / 表面路由 / 召回并集 / 标题权重）----
const LEX_WEIGHT = Number(arg('lex-weight', '1'));   // 加权 RRF 中词法榜权重
const VEC_WEIGHT = Number(arg('vec-weight', '1'));   // 加权 RRF 中向量榜权重
const ROUTE = process.argv.includes('--route');      // B · query 表面路由（纯 ASCII→向量；含汉字→BM25）
const UNION = process.argv.includes('--union');      // C · 召回并集（两榜各取 top-N，按 min(rank) 排序）
const TITLE_REP = Number(arg('title-weight', '4'));  // 标题 token 重复次数（默认 4；评测稳增益=标题×4）
const EMBED_API_KEY_ENV = arg('embed-api-key-env', 'EMBED_API_KEY'); // 只读 env 变量名，key 值绝不落盘
const EMBED_API_KEY = process.env[EMBED_API_KEY_ENV] || '';         // 仅驻留内存，用于 Authorization 头
const HYBRID_STUB = process.argv.includes('--hybrid-stub'); // 进程内桩向量：仅管路自检，非语义基线
const PLUMBING_ONLY = process.argv.includes('--plumbing-only'); // 外部桩服务场景：结果标为非基线、不可引用
// ---- M4 · BM25 + Rerank ----
const RERANK_ENDPOINT = arg('rerank-endpoint', process.env.RERANK_ENDPOINT || '');
const RERANK_MODEL = arg('rerank-model', process.env.RERANK_MODEL || 'jina-reranker-v2-base-multilingual');
const RERANK_CANDIDATES = Number(arg('rerank-candidates', String(TOPN))); // 送入精排的 BM25 top-N（默认=topn）
const RERANK_DOC_CHARS = Number(arg('rerank-doc-chars', '512')); // 每条候选送精排前截断（0=不截；防服务端 OOM/超长）
const RERANK_TIMEOUT = Number(arg('rerank-timeout', '120000'));
const RERANK_RETRIES = Number(arg('rerank-retries', '3'));
const RERANK_CONCURRENCY = Number(arg('rerank-concurrency', '1')); // 默认串行，避免压垮局域网 GPU
const RERANK_API_KEY_ENV = arg('rerank-api-key-env', 'RERANK_API_KEY');
const RERANK_API_KEY = process.env[RERANK_API_KEY_ENV] || '';
const RERANK_STUB = process.argv.includes('--rerank-stub'); // 进程内桩精排：仅管路自检，非语义基线
/** 精排闸门：all=每条都排；xlang=纯 ASCII；lowconf=含汉字且 BM25 top1 < 阈值；可逗号组合 */
const RERANK_GATE = String(arg('rerank-gate', 'all')).split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
const RERANK_LOWCONF_SCORE = Number(arg('rerank-lowconf-score', '15'));
const NON_BASELINE = HYBRID_STUB || PLUMBING_ONLY || RERANK_STUB;
const crypto = require('crypto');

let DICT = null;
let DICT_PATH_ABS = DICT_PATH;
if (DICT_PATH) {
  DICT = JSON.parse(fs.readFileSync(DICT_PATH, 'utf8'));
  DICT_PATH_ABS = path.resolve(DICT_PATH);
}

// ---------------- 分词 / 词形规整（统一由 bm25-index 提供） ----------------
const stemAscii = bm.stemAscii;
const tokenize = bm.tokenize;
const isExcluded = (rel) => bm.isExcluded(rel, EXCLUDES);
const norm = bm.normPath; // 统一为 / 分隔，便于与 query 集比对

// ---------------- 语料读取与索引 ----------------
function makeDoc(rel, title, lines, s, stem) {
  return bm.makeDoc(rel, title, lines, s, { chunkLines: CHUNK_LINES, titleWeight: TITLE_REP, stem: !!stem });
}
/** 读脱敏仓 build_index 旁路产物；缺失则返回 null（保持旧行为）。 */

function sidecarMeta(built) {
  const s = built && built.sidecar;
  if (!s) return { present: false };
  if (s.error) return { present: true, error: s.error };
  return {
    present: true,
    count: s.count,
    generated_at: s.generated_at || null,
    generated_from: s.generated_from || null,
    title_source: 'index.json preferred',
  };
}

function buildFromDisk(root, stem) {
  return bm.buildCorpus({ root, excludes: EXCLUDES, chunkLines: CHUNK_LINES, titleWeight: TITLE_REP, stem: !!stem });
}
const buildIndex = bm.buildIndex;

// ---------------- 查询侧：权重化 token（含 M3 扩展） ----------------
function queryWeights(q, opts) {
  const base = tokenize(q, opts.stem);
  const w = new Map();
  for (const t of base) w.set(t, 1);
  if (opts.dict) {
    for (const t of new Set(base)) {
      const exps = (opts.dict.entries && opts.dict.entries[t]) || [];
      for (const e of exps) {
        if (!e || !e.t) continue;
        if (!w.has(e.t)) w.set(e.t, opts.expw);
      }
    }
  }
  return w;
}
const rankAll = bm.rankAll;

// ---------------- 两套口径下的排名 ----------------
function chunkLevelRank(ranked, topN) { return ranked.slice(0, topN); }
function fileLevelRank(ranked, topN) {
  const seen = new Set(); const out = [];
  for (const r of ranked) { if (seen.has(r.path)) continue; seen.add(r.path); out.push(r); if (out.length >= topN) break; }
  return out;
}
function firstHitRank(list, expectSet) {
  for (let i = 0; i < list.length; i++) if (expectSet.has(list[i].path)) return i + 1;
  return null;
}

// ---------------- 评测 ----------------
function evaluate(queries, docs, idx, topN, ks, opts, ranker) {
  const perQuery = [];
  for (const q of queries) {
    const expectSet = new Set(q.expect.map(norm));
    const ranked = ranker ? ranker(q) : rankAll(docs, idx, queryWeights(q.query, opts));
    const chunkList = chunkLevelRank(ranked, topN);
    const fileList = fileLevelRank(ranked, topN);
    const rec = { id: q.id, type: q.type, query: q.query, expect: q.expect };
    rec.chunk_rank = firstHitRank(chunkList, expectSet);
    rec.file_rank = firstHitRank(fileList, expectSet);
    rec.chunk_hit = {}; rec.file_hit = {};
    for (const k of ks) {
      rec.chunk_hit['@' + k] = rec.chunk_rank !== null && rec.chunk_rank <= k;
      rec.file_hit['@' + k] = rec.file_rank !== null && rec.file_rank <= k;
    }
    rec.diluted = rec.chunk_rank !== rec.file_rank;
    perQuery.push(rec);
  }
  const mean = a => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : 0);
  const rr = r => (r === null ? 0 : 1 / r);
  const summary = { n_queries: queries.length, ks, top_n: topN };
  for (const view of ['file', 'chunk']) {
    const s = {};
    for (const k of ks) {
      const passed = perQuery.filter(p => p[view + '_hit']['@' + k]).length;
      s['recall@' + k] = { count: passed, total: perQuery.length, value: Number((passed / (perQuery.length || 1)).toFixed(4)) };
    }
    s.MRR = Number(mean(perQuery.map(p => rr(p[view + '_rank']))).toFixed(4));
    s.misses = perQuery.filter(p => p[view + '_rank'] === null).map(p => p.id);
    summary[view + '_level'] = s;
  }
  summary.diluted_queries = perQuery.filter(p => p.diluted).map(p => ({ id: p.id, chunk_rank: p.chunk_rank, file_rank: p.file_rank }));
  summary.diluted_count = summary.diluted_queries.length;
  const byType = {};
  for (const p of perQuery) {
    const t = byType[p.type] = byType[p.type] || { n: 0, hit1: 0, hit5: 0, rr: 0 };
    t.n++;
    if (p.file_hit['@1']) t.hit1++;
    if (p.file_hit['@5']) t.hit5++;
    t.rr += rr(p.file_rank);
  }
  summary.by_type_file_level = {};
  for (const [t, v] of Object.entries(byType)) {
    summary.by_type_file_level[t] = { n: v.n, 'recall@1': Number((v.hit1 / v.n).toFixed(4)), 'recall@5': Number((v.hit5 / v.n).toFixed(4)), MRR: Number((v.rr / v.n).toFixed(4)) };
  }
  return { perQuery, summary };
}

// ---------------- M2 · 向量成分与 RRF 融合 ----------------
function cosine(a, b) {
  let dot = 0, na = 0, nb = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
  if (na === 0 || nb === 0) return 0;
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}
// RRF: score(d) = Σ_lists 1/(k + rank_in_list)。lexList/vecList 元素须含 di。
function rrfFuse(lexList, vecList, k) {
  const m = new Map();
  const bump = (r, i) => {
    const e = m.get(r.di) || { di: r.di, path: r.path, lineStart: r.lineStart, lineEnd: r.lineEnd, rrf: 0 };
    e.rrf += 1 / (k + (i + 1));
    m.set(r.di, e);
  };
  lexList.forEach(bump);
  vecList.forEach(bump);
  const out = [...m.values()];
  out.sort((a, b) => b.rrf - a.rrf);
  return out;
}
// T33-A · 加权 RRF：score(d) = w_lex/(k+rank_lex) + w_vec/(k+rank_vec)。wl=wv=1 时等价旧等权 RRF。
function fuseWeighted(lexList, vecList, k, wl, wv) {
  const m = new Map();
  const bump = (r, i, w) => {
    const e = m.get(r.di) || { di: r.di, path: r.path, lineStart: r.lineStart, lineEnd: r.lineEnd, rrf: 0 };
    e.rrf += w / (k + (i + 1));
    m.set(r.di, e);
  };
  lexList.forEach((r, i) => bump(r, i, wl));
  vecList.forEach((r, i) => bump(r, i, wv));
  const out = [...m.values()];
  out.sort((a, b) => b.rrf - a.rrf); // 稳定排序：同分保持插入序（lex 先于 vec），与旧等权 rrfFuse 完全一致
  return out;
}
// T33-C · 召回并集：两榜各取前 N，按 min(rank_lex, rank_vec) 升序，平手以词法名次优先。
function unionRank(lexList, vecList) {
  const m = new Map();
  const put = (r, i, isLex) => {
    const e = m.get(r.di) || { di: r.di, path: r.path, lineStart: r.lineStart, lineEnd: r.lineEnd, lr: Infinity, vr: Infinity };
    if (isLex) e.lr = i + 1; else e.vr = i + 1;
    m.set(r.di, e);
  };
  lexList.forEach((r, i) => put(r, i, true));
  vecList.forEach((r, i) => put(r, i, false));
  const out = [...m.values()].map(e => ({ ...e, score: Math.min(e.lr, e.vr) }));
  out.sort((a, b) => a.score - b.score || a.lr - b.lr);
  return out;
}
// T33-B · 表面路由判据：query 是否含汉字（只读 query 字面，不参照答案）。
function hasCJK(s) { return /[\u4e00-\u9fff]/.test(String(s)); }
function vectorRank(queryVec, docVecs, docs) {
  const out = [];
  for (let di = 0; di < docVecs.length; di++) {
    if (!docVecs[di]) continue;
    out.push({ di, path: docs[di].path, lineStart: docs[di].lineStart, lineEnd: docs[di].lineEnd, score: cosine(queryVec, docVecs[di]) });
  }
  out.sort((a, b) => b.score - a.score);
  return out;
}
function embedKey(model, text) { return crypto.createHash('sha256').update(model).update('\u0000').update(text).digest('hex'); }
function loadEmbedCache(p) { try { const j = JSON.parse(fs.readFileSync(p, 'utf8')); return { model: j.model || '', vectors: j.vectors || {} }; } catch { return { model: '', vectors: {} }; } }
async function embedBatch(texts, endpoint, model, timeoutMs, apiKey) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (apiKey) headers['Authorization'] = 'Bearer ' + apiKey; // 仅运行时内存使用，不落盘
    const res = await fetch(endpoint, {
      method: 'POST', headers,
      body: JSON.stringify({ model, input: texts }), signal: ctrl.signal,
    });
    if (!res.ok) { const t = await res.text().catch(() => ''); throw new Error('HTTP ' + res.status + ' ' + String(t).slice(0, 200)); }
    const j = await res.json();
    if (!j || !Array.isArray(j.data)) throw new Error('unexpected response shape: ' + JSON.stringify(j).slice(0, 200));
    const vecs = j.data.slice().sort((a, b) => (a.index || 0) - (b.index || 0)).map(d => d.embedding);
    if (vecs.length !== texts.length) throw new Error('vector count mismatch: got ' + vecs.length + ' want ' + texts.length);
    if (!Array.isArray(vecs[0])) throw new Error('embedding not an array');
    return vecs;
  } finally { clearTimeout(timer); }
}
// 桩向量：仅用于验证“客户端/批处理/缓存/RRF”管路是否接通，**不是语义基线**。
function stubVector(text, dim) {
  const h = crypto.createHash('sha256').update(text).digest();
  const v = new Array(dim || 512);
  for (let i = 0; i < v.length; i++) v[i] = (h[i % h.length] / 255) * 2 - 1;
  return v;
}
// 返回 { docVecs, queryVecs, stats }；文本去重后只请求缺失项；缓存按 model+text 哈希。
async function computeEmbeddings(texts, queries, opts) {
  const cache = loadEmbedCache(opts.cachePath);
  const unique = new Map(); // key -> text
  for (const t of texts) unique.set(embedKey(opts.model, t), t);
  for (const q of queries) unique.set(embedKey(opts.model, q.query), q.query);
  let cacheHits = 0, fetched = 0, dim = 0;
  const missing = [];
  for (const [k, t] of unique) {
    const v = cache.vectors[k];
    if (Array.isArray(v) && v.length) { cacheHits++; dim = dim || v.length; }
    else missing.push([k, t]);
  }
  if (opts.stub) {
    for (const [k, t] of missing) { const v = stubVector(t, opts.stubDim); cache.vectors[k] = v; dim = v.length; fetched++; }
  } else {
    const groups = [];
    for (let i = 0; i < missing.length; i += opts.batch) groups.push(missing.slice(i, i + opts.batch));
    let gi = 0;
    const fetchGroup = async (part) => {
      let lastErr;
      const attempts = Math.max(1, opts.retries || 1);
      for (let a = 0; a < attempts; a++) {
        try { return await embedBatch(part.map(x => x[1]), opts.endpoint, opts.model, opts.timeout, opts.apiKey); }
        catch (e) { lastErr = e; }
      }
      throw lastErr;
    };
    const worker = async () => {
      while (gi < groups.length) {
        const part = groups[gi++];
        const vecs = await fetchGroup(part);
        for (let j = 0; j < part.length; j++) { cache.vectors[part[j][0]] = vecs[j]; dim = dim || vecs[j].length; fetched++; }
      }
    };
    const lane = Math.max(1, Math.min(opts.concurrency || 1, groups.length));
    await Promise.all(Array.from({ length: lane }, () => worker()));
  }
  cache.model = opts.model;
  for (const t of texts) if (!cache.vectors[embedKey(opts.model, t)]) throw new Error('missing doc vector after embed');
  for (const q of queries) if (!cache.vectors[embedKey(opts.model, q.query)]) throw new Error('missing query vector after embed');
  try { fs.writeFileSync(opts.cachePath, JSON.stringify(cache)); } catch (e) { /* 缓存写失败不致命 */ }
  return { docVecs: texts.map(t => cache.vectors[embedKey(opts.model, t)]), queryVecs: queries.map(q => cache.vectors[embedKey(opts.model, q.query)]), stats: { cache_hits: cacheHits, fetched, unique_texts: unique.size, dim } };
}

// ---------------- M4 · BM25 + Rerank ----------------
/** 解析常见 rerank 响应：{results|data:[...]} 或裸数组；项含 index + relevance_score|score。 */
function parseRerankResponse(body, nDocs) {
  const raw = Array.isArray(body) ? body
    : (body && Array.isArray(body.results) ? body.results
      : (body && Array.isArray(body.data) ? body.data : null));
  if (!raw) throw new Error('unexpected rerank response shape: ' + JSON.stringify(body).slice(0, 200));
  const out = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const index = Number(item.index);
    const score = item.relevance_score !== undefined ? Number(item.relevance_score)
      : (item.score !== undefined ? Number(item.score) : NaN);
    if (!Number.isInteger(index) || index < 0 || index >= nDocs || !Number.isFinite(score)) {
      throw new Error('invalid rerank item: ' + JSON.stringify(item).slice(0, 120));
    }
    out.push({ index, relevance_score: score });
  }
  if (!out.length) throw new Error('rerank returned empty results');
  return out;
}
/** 按精排分数降序重排候选；未出现在 results 中的候选保持原相对序垫后。 */
function applyRerankScores(candidates, results) {
  const scoreByIdx = new Map();
  for (const r of results) scoreByIdx.set(r.index, r.relevance_score);
  const scored = [];
  const rest = [];
  for (let i = 0; i < candidates.length; i++) {
    const c = candidates[i];
    if (scoreByIdx.has(i)) scored.push({ ...c, score: scoreByIdx.get(i), rerank_score: scoreByIdx.get(i) });
    else rest.push({ ...c });
  }
  scored.sort((a, b) => b.score - a.score || a.di - b.di);
  return scored.concat(rest);
}
/** 桩精排：确定性伪分（非语义），只验证客户端/解析/重排管路。 */
function stubRerankResults(query, documents) {
  return documents.map((doc, index) => {
    const h = crypto.createHash('sha256').update(String(query)).update('\u0000').update(String(doc)).digest();
    const score = (h[0] + h[1] * 256) / 65535; // [0,1)
    return { index, relevance_score: score };
  }).sort((a, b) => b.relevance_score - a.relevance_score);
}
async function rerankRequest(query, documents, opts) {
  if (!documents.length) return [];
  if (opts.stub) return stubRerankResults(query, documents);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeout);
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (opts.apiKey) headers['Authorization'] = 'Bearer ' + opts.apiKey;
    const topN = opts.topN != null ? opts.topN : documents.length;
    let lastErr;
    const attempts = Math.max(1, opts.retries || 1);
    for (let a = 0; a < attempts; a++) {
      try {
        const res = await fetch(opts.endpoint, {
          method: 'POST', headers,
          body: JSON.stringify({ model: opts.model, query, documents, top_n: topN }),
          signal: ctrl.signal,
        });
        if (!res.ok) {
          const t = await res.text().catch(() => '');
          throw new Error('HTTP ' + res.status + ' ' + String(t).slice(0, 200));
        }
        const j = await res.json();
        return parseRerankResponse(j, documents.length);
      } catch (e) { lastErr = e; }
    }
    throw lastErr;
  } finally { clearTimeout(timer); }
}
function clipRerankDoc(text, maxChars) {
  const s = String(text || '');
  if (!maxChars || maxChars <= 0 || s.length <= maxChars) return s;
  return s.slice(0, maxChars);
}
function parseRerankGates(gates) {
  const g = (Array.isArray(gates) ? gates : String(gates || 'all').split(','))
    .map(s => String(s).trim().toLowerCase()).filter(Boolean);
  return g.length ? g : ['all'];
}
/**
 * 是否对本 query 启用精排（只读 query 字面 + BM25 榜首分数，不看答案）。
 * - all：全开
 * - xlang：纯 ASCII（跨语言表面启发式）
 * - lowconf：含汉字且存在候选，且 top1 BM25 分 < lowconfScore
 */
function shouldRerankQuery(query, cands, gates, lowconfScore) {
  const g = parseRerankGates(gates);
  if (g.includes('all')) return { apply: true, reasons: ['all'] };
  const reasons = [];
  const ascii = !hasCJK(query);
  if (g.includes('xlang') && ascii) reasons.push('xlang');
  const top1 = cands && cands[0] && typeof cands[0].score === 'number' ? cands[0].score : null;
  if (g.includes('lowconf') && !ascii && top1 !== null && top1 < lowconfScore) reasons.push('lowconf');
  return { apply: reasons.length > 0, reasons, top1 };
}
/** 对每条 query：BM25 top-C →（按闸门）rerank → 返回与 queries 对齐的列表。 */
async function computeBm25Rerank(queries, docs, idx, opts) {
  const candN = Math.max(1, opts.candidates || TOPN);
  const docChars = opts.docChars != null ? opts.docChars : RERANK_DOC_CHARS;
  const gates = parseRerankGates(opts.gates != null ? opts.gates : RERANK_GATE);
  const lowconfScore = opts.lowconfScore != null ? opts.lowconfScore : RERANK_LOWCONF_SCORE;
  const lexLists = queries.map(q => {
    const w = new Map();
    for (const t of tokenize(q.query, opts.stem)) w.set(t, 1);
    return rankAll(docs, idx, w).slice(0, candN);
  });
  const out = new Array(queries.length);
  const decisions = new Array(queries.length);
  let qi = 0;
  const worker = async () => {
    while (qi < queries.length) {
      const i = qi++;
      const cands = lexLists[i];
      const dec = shouldRerankQuery(queries[i].query, cands, gates, lowconfScore);
      decisions[i] = { id: queries[i].id, apply: dec.apply, reasons: dec.reasons || [], top1: dec.top1 == null ? null : Number(dec.top1.toFixed(4)) };
      if (!cands.length) { out[i] = cands; continue; }
      if (!dec.apply) { out[i] = cands; continue; }
      const texts = cands.map(r => clipRerankDoc(docs[r.di].text, docChars));
      const results = await rerankRequest(queries[i].query, texts, opts);
      out[i] = applyRerankScores(cands, results);
    }
  };
  const lane = Math.max(1, Math.min(opts.concurrency || 1, queries.length || 1));
  await Promise.all(Array.from({ length: lane }, () => worker()));
  const applied = decisions.filter(d => d.apply).map(d => d.id);
  return {
    lists: out,
    decisions,
    stats: {
      candidates: candN, doc_chars: docChars, queries: queries.length, stub: !!opts.stub,
      gates, lowconf_score: lowconfScore,
      reranked: applied.length, reranked_ids: applied,
      skipped: decisions.length - applied.length,
    },
  };
}

// ---------------- Markdown ----------------
function toMarkdown(meta, res) {
  const L = [];
  L.push('## 运行：' + meta.label, '');
  L.push('- 语料根：`' + meta.root + '`');
  L.push('- 配置：mode=' + meta.mode + ' · stem=' + meta.stem + ' · dict=' + (meta.dict || '（无）') + ' · expand_weight=' + meta.expand_weight);
  L.push('- chunk：' + CHUNK_LINES + ' 行/块 · top-N = ' + meta.top_n + ' · k = ' + meta.ks.join('/'));
  L.push('- 语料规模：' + meta.corpus.md_files + ' 文件 / ' + meta.corpus.chunks + ' chunk / 词表 ' + meta.corpus.vocab, '');
  L.push('| 指标（文件级·主口径） | 值 |', '|---|---|');
  for (const k of meta.ks) L.push('| recall@' + k + ' | ' + res.summary.file_level['recall@' + k].count + '/' + res.summary.file_level['recall@' + k].total + ' = ' + res.summary.file_level['recall@' + k].value + ' |');
  L.push('| MRR | ' + res.summary.file_level.MRR + ' |', '');
  L.push('| 指标（chunk 级·辅口径） | 值 |', '|---|---|');
  for (const k of meta.ks) L.push('| recall@' + k + ' | ' + res.summary.chunk_level['recall@' + k].count + '/' + res.summary.chunk_level['recall@' + k].total + ' = ' + res.summary.chunk_level['recall@' + k].value + ' |');
  L.push('| MRR | ' + res.summary.chunk_level.MRR + ' |');
  L.push('| 受同文件 chunk 占位影响（两口径排名不一致） | ' + res.summary.diluted_count + ' 条 |', '');
  L.push('### 逐条', '', '| ID | 类型 | query | 文件级排名 | chunk 级排名 | 稀释 |', '|---|---|---|---|---|---|');
  for (const p of res.perQuery) {
    L.push('| ' + p.id + ' | ' + p.type + ' | ' + p.query + ' | ' + (p.file_rank || '未命中') + ' | ' + (p.chunk_rank || '未命中') + ' | ' + (p.diluted ? '是' : '') + ' |');
  }
  return L.join('\n');
}

// ---------------- 自检 ----------------
function selftest() {
  const lines = n => Array.from({ length: n }, (_, i) => 'line' + i + ' 内容 内容');
  const docs = [];
  const mk = (rel, title, arr) => { for (let s = 0; s < arr.length; s += CHUNK_LINES) { const d = makeDoc(rel, title, arr, s, false); if (d) docs.push(d); } };
  mk('pages/alpha.md', 'alpha', lines(45));
  mk('pages/beta.md', 'beta', lines(10));
  mk('pages/gamma.md', 'gamma', lines(5));
  docs[0].tf.set('uniqueterm', 5); docs[0].len += 5;
  docs[3].tf.set('uniqueterm', 5); docs[3].len += 5;
  docs[4].tf.set('otherterm', 5); docs[4].len += 5;
  const idx = buildIndex(docs);
  const queries = [
    { id: 'S1', type: 'selftest', query: 'uniqueterm', expect: ['pages/alpha.md', 'pages/beta.md'] },
    { id: 'S2', type: 'selftest', query: 'otherterm', expect: ['pages/gamma.md'] },
    { id: 'S3', type: 'selftest', query: 'nonexistentzzz', expect: ['pages/beta.md'] },
  ];
  const res = evaluate(queries, docs, idx, TOPN, KS, { stem: false, dict: null, expw: 0 });
  // 扩展权重自检：用 dict 把 S1 的 uniqueterm 扩到 gamma 应能命中? 反例：不应误命中
  const dict = { entries: { uniqueterm: [{ t: 'otherterm', co: 9, pmi: 9 }] } };
  const res2 = evaluate(queries, docs, idx, TOPN, KS, { stem: false, dict, expw: 0.35 });
  const checks = [
    ['S1 文件级有命中', res.perQuery[0].file_rank !== null],
    ['S2 文件级命中第1', res.perQuery[1].file_rank === 1],
    ['S3 未命中', res.perQuery[2].file_rank === null],
    ['recall@5 有值', typeof res.summary.file_level['recall@5'].value === 'number'],
    ['MRR 在(0,1]', res.summary.file_level.MRR > 0 && res.summary.file_level.MRR <= 1],
    ['扩展词生效（S1 查询命中集合含 gamma）', res2.perQuery[0].file_rank !== null],
    ['词形规整 stemAscii(vectors)=vector', stemAscii('vectors') === 'vector'],
    ['词形规整 stemAscii(running)=runn', stemAscii('running') === 'runn'],
  ];
  // M2 管路自检（纯函数，不依赖任何 embedding 服务）
  const fus = rrfFuse([{ di: 0, path: 'a' }, { di: 1, path: 'b' }], [{ di: 1, path: 'b' }, { di: 2, path: 'c' }], 60);
  const tmpCache = path.join(require('os').tmpdir(), 'eval-recall-selftest-cache.json');
  fs.writeFileSync(tmpCache, JSON.stringify({ model: 'x', vectors: { k1: [1, 2, 3] } }));
  const rt = loadEmbedCache(tmpCache);
  try { fs.unlinkSync(tmpCache); } catch (e) { /* ignore */ }
  checks.push(
    ['cosine 同向 = 1', Math.abs(cosine([1, 0], [1, 0]) - 1) < 1e-9],
    ['cosine 正交 = 0', Math.abs(cosine([1, 0], [0, 1])) < 1e-9],
    ['RRF：两榜共现项(di=1)排第一', fus[0].di === 1],
    ['RRF：仅单榜命中项仍在结果内', fus.length === 3 && fus.some(x => x.di === 0) && fus.some(x => x.di === 2)],
    ['embed 缓存可读写', rt.vectors.k1 && rt.vectors.k1[2] === 3],
    ['桩向量维度正确', stubVector('abc', 8).length === 8],
  );
  // T33 新增：加权 RRF / 召回并集 / 表面路由（纯函数）
  const wf = fuseWeighted([{ di: 0, path: 'a' }], [{ di: 1, path: 'b' }], 60, 3, 1);
  const wf1 = fuseWeighted([{ di: 0, path: 'a' }], [{ di: 1, path: 'b' }], 60, 1, 1);
  const un = unionRank([{ di: 0, path: 'a' }, { di: 1, path: 'b' }], [{ di: 1, path: 'b' }, { di: 0, path: 'a' }]);
  checks.push(
    ['加权 RRF：3:1 时词法榜首项优先于向量榜首项', wf[0].di === 0],
    ['加权 RRF：1:1 时两单榜项同分（按 di 稳定排序）', wf1.length === 2],
    ['召回并集：按 min(rank) 排序，平手以词法名次优先(di=0)', un[0].di === 0],
    ['表面路由：含汉字 → true', hasCJK('向量检索') === true],
    ['表面路由：纯 ASCII → false', hasCJK('vector retrieval') === false],
  );
  // M4 · rerank 纯函数 / 桩管路（不触网）
  const parsed = parseRerankResponse({ results: [{ index: 2, relevance_score: 0.9 }, { index: 0, relevance_score: 0.1 }] }, 3);
  const cands = [{ di: 10, path: 'a' }, { di: 11, path: 'b' }, { di: 12, path: 'c' }];
  const reordered = applyRerankScores(cands, [{ index: 2, relevance_score: 5 }, { index: 0, relevance_score: 1 }]);
  const stubbed = stubRerankResults('向量检索', ['甲', '乙', '丙']);
  const partial = applyRerankScores(cands, [{ index: 1, relevance_score: 9 }]);
  checks.push(
    ['rerank 解析：保留 index 与分数', parsed.length === 2 && parsed[0].index === 2 && parsed[0].relevance_score === 0.9],
    ['rerank 重排：高分在前 (di=12→10)', reordered[0].di === 12 && reordered[1].di === 10],
    ['rerank 重排：未打分项垫后且不丢候选', partial.length === 3 && partial[0].di === 11 && partial[1].di === 10 && partial[2].di === 12],
    ['rerank 桩：条数=文档数且含 index', stubbed.length === 3 && stubbed.every(x => Number.isInteger(x.index))],
    ['rerank 截断：超长保留前 N 字', clipRerankDoc('abcdefghij', 4) === 'abcd'],
    ['rerank 截断：0 表示不截', clipRerankDoc('abcdefghij', 0) === 'abcdefghij'],
    ['闸门 all → 启用', shouldRerankQuery('向量检索', [{ score: 30 }], 'all', 15).apply === true],
    ['闸门 xlang：ASCII → 启用', shouldRerankQuery('vector retrieval', [{ score: 3 }], 'xlang', 15).apply === true],
    ['闸门 xlang：含汉字 → 跳过', shouldRerankQuery('向量检索', [{ score: 3 }], 'xlang', 15).apply === false],
    ['闸门 lowconf：低分中文 → 启用', shouldRerankQuery('模糊说法', [{ score: 10 }], 'lowconf', 15).apply === true],
    ['闸门 lowconf：高分中文 → 跳过', shouldRerankQuery('精确术语', [{ score: 40 }], 'lowconf', 15).apply === false],
    ['闸门 lowconf：ASCII 不走低置信', shouldRerankQuery('vector', [{ score: 3 }], 'lowconf', 15).apply === false],
  );
  const failed = checks.filter(c => !c[1]).map(c => c[0]);
  console.log(JSON.stringify({ selftest: true, docs: docs.length, checks: checks.length, failed, pass: failed.length === 0,
    summary: res.summary, per_query: res.perQuery.map(p => ({ id: p.id, file_rank: p.file_rank, chunk_rank: p.chunk_rank })) }, null, 2));
  process.exit(failed.length === 0 ? 0 : 1);
}

// ---------------- main ----------------
if (SELFTEST) { selftest(); }
else if (MODE === 'bm25-rerank') {
  if (!QJSON) { console.error('需要 --queries-json <path>（或用 --selftest）'); process.exit(1); }
  if (!RERANK_ENDPOINT && !RERANK_STUB) {
    console.log(JSON.stringify({
      mode: 'bm25-rerank', status: 'unavailable', reason: 'endpoint_not_configured',
      note: '未提供 rerank 端点：请传 --rerank-endpoint 或设置 RERANK_ENDPOINT。不产出任何数字。',
    }, null, 2));
    process.exit(2);
  }
  (async () => {
    const queries = JSON.parse(fs.readFileSync(QJSON, 'utf8'));
    const opts = { stem: STEM, dict: DICT, expw: EXPW };
    const built = buildFromDisk(ROOT, STEM);
    const idx = buildIndex(built.docs);
    let rr;
    try {
      rr = await computeBm25Rerank(queries, built.docs, idx, {
        endpoint: RERANK_ENDPOINT, model: RERANK_MODEL, candidates: RERANK_CANDIDATES,
        docChars: RERANK_DOC_CHARS, gates: RERANK_GATE, lowconfScore: RERANK_LOWCONF_SCORE,
        timeout: RERANK_TIMEOUT, retries: RERANK_RETRIES, concurrency: RERANK_CONCURRENCY,
        apiKey: RERANK_API_KEY, stub: RERANK_STUB, stem: STEM, topN: RERANK_CANDIDATES,
      });
    } catch (e) {
      console.log(JSON.stringify({
        mode: 'bm25-rerank', status: 'unavailable', reason: 'rerank_unreachable',
        endpoint: RERANK_ENDPOINT, model: RERANK_MODEL, error: String(e && e.message || e),
        note: 'rerank 服务不可达或响应异常。不产出任何对比数字；服务可用后按同一命令重跑即可。',
      }, null, 2));
      process.exit(2);
    }
    const ranker = (q) => rr.lists[queries.indexOf(q)];
    const res = evaluate(queries, built.docs, idx, TOPN, KS, opts, ranker);
    const gateTag = parseRerankGates(RERANK_GATE).join('+');
    const label = RERANK_STUB
      ? 'M4-管路自检（桩精排·非语义基线·数字不可引用）'
      : ('M4 · BM25（标题×' + TITLE_REP + '）+ Rerank[' + gateTag + ']');
    const meta = {
      label, mode: 'bm25-rerank', stem: STEM, dict: '', expand_weight: null, title_weight: TITLE_REP,
      root: ROOT, chunk_lines: CHUNK_LINES, top_n: TOPN, ks: KS, excludes: EXCLUDES,
      rerank: {
        endpoint: RERANK_ENDPOINT || '(stub)', model: RERANK_MODEL,
        candidates: RERANK_CANDIDATES, doc_chars: RERANK_DOC_CHARS, concurrency: RERANK_CONCURRENCY,
        gates: parseRerankGates(RERANK_GATE), lowconf_score: RERANK_LOWCONF_SCORE,
        stub: RERANK_STUB, not_a_baseline: NON_BASELINE, stats: rr.stats, decisions: rr.decisions,
      },
      corpus: {
        md_files: new Set(built.docs.map(d => d.path)).size, chunks: built.docs.length, vocab: idx.df.size,
        bytes: built.totalBytes, lines: built.totalLines, read_failures: built.failed.length,
        sidecar_index: sidecarMeta(built),
      },
      note: 'bm25-rerank 非默认；公平对照请用同 title-weight 的 mode=bm25。端点未配置时不产数字。',
      baseline_contrast: 'mode=bm25 --title-weight ' + TITLE_REP,
    };
    const md = toMarkdown(meta, res);
    if (WRITE_MD) fs.writeFileSync(WRITE_MD, md);
    console.log(JSON.stringify({ ...meta, ...res.summary, per_query: res.perQuery, markdown: WRITE_MD ? '(written to ' + WRITE_MD + ')' : md }, null, 2));
  })().catch(e => { console.error('bm25-rerank 运行失败: ' + (e && e.stack || e)); process.exit(3); });
} else if (MODE === 'hybrid') {
  if (!QJSON) { console.error('需要 --queries-json <path>（或用 --selftest）'); process.exit(1); }
  if (!EMBED_ENDPOINT) {
    console.log(JSON.stringify({
      mode: 'hybrid', status: 'unavailable', reason: 'endpoint_not_configured',
      note: '未提供 embedding 端点：请传 --embed-endpoint 或设置 EMBED_ENDPOINT。不产出任何数字。',
    }, null, 2));
    process.exit(2);
  }
  (async () => {
    const queries = JSON.parse(fs.readFileSync(QJSON, 'utf8'));
    const opts = { stem: STEM, dict: DICT, expw: EXPW };
    const built = buildFromDisk(ROOT, STEM);
    const idx = buildIndex(built.docs);
    const texts = built.docs.map(d => d.text);
    let emb;
    try {
      emb = await computeEmbeddings(texts, queries, {
        endpoint: EMBED_ENDPOINT, model: EMBED_MODEL, cachePath: EMBED_CACHE,
        batch: EMBED_BATCH, timeout: EMBED_TIMEOUT, stub: HYBRID_STUB, stubDim: 512,
        concurrency: EMBED_CONCURRENCY, retries: EMBED_RETRIES,
        apiKey: EMBED_API_KEY,
      });
    } catch (e) {
      // 服务不可达 / 响应异常：如实报 unavailable，绝不产出任何数字
      console.log(JSON.stringify({
        mode: 'hybrid', status: 'unavailable', reason: 'embedding_unreachable',
        endpoint: EMBED_ENDPOINT, model: EMBED_MODEL, error: String(e && e.message || e),
        note: 'embedding 服务不可达或响应异常，向量成分未能真跑。不产出任何对比数字；服务可用后按 §复现命令重跑即可。',
      }, null, 2));
      process.exit(2);
    }
    const docVecs = emb.docVecs;
    function lexWeights(text) { const w = new Map(); for (const t of tokenize(text, STEM)) w.set(t, 1); return w; }
    const ranker = (q) => {
      const qi = queries.indexOf(q);
      const lexList = rankAll(built.docs, idx, lexWeights(q.query));
      const vecList = vectorRank(emb.queryVecs[qi], docVecs, built.docs);
      if (ROUTE) return hasCJK(q.query) ? lexList : vecList;                        // B · 表面路由
      if (UNION) return unionRank(lexList.slice(0, TOPN), vecList.slice(0, TOPN));  // C · 召回并集
      return fuseWeighted(lexList, vecList, RRF_K, LEX_WEIGHT, VEC_WEIGHT);         // A · 加权 RRF（1:1=等权）
    };
    const res = evaluate(queries, built.docs, idx, TOPN, KS, opts, ranker);
    const fusionTag = ROUTE ? '类型路由(表面)' : (UNION ? '召回并集(min-rank)' : ('加权RRF ' + LEX_WEIGHT + ':' + VEC_WEIGHT));
    const label = HYBRID_STUB ? 'M2-管路自检（桩向量·非语义基线）' : ('M2 · hybrid（BM25 + 向量 · ' + fusionTag + '）');
    const label2 = NON_BASELINE ? 'M2-管路自检（桩向量·非语义基线·数字不可引用）' : label;
    const meta = {
      label: label2, mode: 'hybrid', stem: STEM, dict: '', expand_weight: null,
      root: ROOT, chunk_lines: CHUNK_LINES, top_n: TOPN, ks: KS, excludes: EXCLUDES,
      hybrid: { endpoint: EMBED_ENDPOINT, model: EMBED_MODEL, rrf_k: RRF_K, lex_weight: LEX_WEIGHT, vec_weight: VEC_WEIGHT, route: ROUTE, union: UNION, title_weight: TITLE_REP, dim: emb.stats.dim, cache: path.resolve(EMBED_CACHE), embed_stats: emb.stats, stub: HYBRID_STUB, not_a_baseline: NON_BASELINE },
      corpus: {
        md_files: new Set(built.docs.map(d => d.path)).size, chunks: built.docs.length, vocab: idx.df.size,
        bytes: built.totalBytes, lines: built.totalLines, read_failures: built.failed.length,
        sidecar_index: sidecarMeta(built),
      },
      note: 'hybrid 非默认；默认请用 mode=bm25（标题×4）',
    };
    const md = toMarkdown(meta, res);
    if (WRITE_MD) fs.writeFileSync(WRITE_MD, md);
    console.log(JSON.stringify({ ...meta, ...res.summary, per_query: res.perQuery, markdown: WRITE_MD ? '(written to ' + WRITE_MD + ')' : md }, null, 2));
  })().catch(e => { console.error('hybrid 运行失败: ' + (e && e.stack || e)); process.exit(3); });
} else {
  if (!QJSON) { console.error('需要 --queries-json <path>（或用 --selftest）'); process.exit(1); }
  const queries = JSON.parse(fs.readFileSync(QJSON, 'utf8'));
  const opts = { stem: STEM, dict: DICT, expw: EXPW };
  const built = buildFromDisk(ROOT, STEM);
  const idx = buildIndex(built.docs);
  const res = evaluate(queries, built.docs, idx, TOPN, KS, opts);
  const label = MODE === 'bm25' ? 'M1 · BM25-only'
    : (STEM && DICT ? 'M3 · BM25 + 词形规整 + 语料扩展' : (STEM ? 'M3a · BM25 + 词形规整' : 'M3b · BM25 + 语料扩展'));
  const meta = {
    label, mode: MODE, stem: STEM, dict: DICT_PATH_ABS || '', expand_weight: DICT ? EXPW : null, title_weight: TITLE_REP,
    root: ROOT, chunk_lines: CHUNK_LINES, top_n: TOPN, ks: KS, excludes: EXCLUDES,
    corpus: {
      md_files: new Set(built.docs.map(d => d.path)).size, chunks: built.docs.length, vocab: idx.df.size,
      bytes: built.totalBytes, lines: built.totalLines, read_failures: built.failed.length,
      sidecar_index: sidecarMeta(built),
    },
    recall_default: 'bm25-title4',
  };
  const md = toMarkdown(meta, res);
  if (WRITE_MD) fs.writeFileSync(WRITE_MD, md);
  console.log(JSON.stringify({ ...meta, ...res.summary, per_query: res.perQuery, markdown: WRITE_MD ? '(written to ' + WRITE_MD + ')' : md }, null, 2));
}