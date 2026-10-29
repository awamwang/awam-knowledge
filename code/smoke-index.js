'use strict';
/**
 * 最小 Markdown 摄取 + 本地 BM25 索引 + 冒烟检索（零依赖，只读源数据）
 *
 * 用法:
 *   node smoke-index.js [--root "<graphRoot>"] [--exclude "logseq\\bak,.recycle"] [--queries "q1|q2|..."]
 * 缺省根：KM_DATA_ROOT/<KM_SOURCE_ID>（默认源 id logseq-main），不指向真源。
 *
 * 口径（与 eval-recall 共用 code/bm25-index.js）:
 * - 分词：ASCII 单词(>=2 字符) + 中文二元组(bigram)；无词干化、无停用词表、无向量
 * - 打分：BM25(k1=1.2, b=0.75) + 标题×4 加权；ROOT 下有 index.json 时优先用其 title
 * - 分块：每 20 行一个 chunk（记录行号范围），结果可回溯到源文件行
 */
const { resolveIndexRoot } = require('./index-root');
const bm = require('./bm25-index');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] !== undefined ? process.argv[i + 1] : dflt;
}

const ROOT = resolveIndexRoot(arg('root', ''));
const EXCLUDES = bm.parseExcludes(arg('exclude', bm.DEFAULT_EXCLUDES));
const QUERIES = arg('queries', '').split('|').filter(Boolean);
const TOPK = 5;
const TITLE_REP = Number(arg('title-weight', String(bm.DEFAULT_TITLE_WEIGHT)));

const corpus = bm.buildCorpus({ root: ROOT, excludes: EXCLUDES, titleWeight: TITLE_REP });
const idx = bm.buildIndex(corpus.docs);

const queryResults = QUERIES.map(q => ({
  query: q,
  top: bm.search(corpus, idx, q, { topk: TOPK, stem: false }),
}));

console.log(JSON.stringify({
  root: ROOT,
  excludes: EXCLUDES,
  ingest: {
    md_files_scanned: corpus.scanned.length,
    md_files_indexed: new Set(corpus.docs.map(d => d.path)).size,
    md_files_failed: corpus.failed.length,
    zero_byte_files: corpus.zeroByte.length,
    encoding_warning_files: corpus.encodingWarn.length,
    skipped_dirs: corpus.skipped.length,
    total_bytes: corpus.totalBytes,
    total_lines: corpus.totalLines,
    chunks: idx.N,
    avg_chunk_tokens: Number(idx.avgdl.toFixed(1)),
    vocab_size: idx.df.size,
  },
  failure_list: corpus.failed,
  zero_byte_list: corpus.zeroByte,
  encoding_warning_list: corpus.encodingWarn.slice(0, 20),
  skipped_dir_sample: corpus.skipped.slice(0, 10),
  chunking: { chunk_lines: bm.DEFAULT_CHUNK_LINES, scoring: 'BM25 k1=1.2 b=0.75 + title x' + TITLE_REP + ' boost + CJK bigram', title_weight: TITLE_REP },
  sidecar_index: corpus.sidecar
    ? { present: true, count: corpus.sidecar.count, generated_at: corpus.sidecar.generated_at || null, error: corpus.sidecar.error || null }
    : { present: false },
  queries: queryResults,
}, null, 2));
