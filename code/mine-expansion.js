'use strict';
/**
 * T20 · M3 扩展词典的「独立来源」构建器 —— 无监督，仅用语料
 *
 * 关键纪律（对应 T20 硬约束）:
 *   - 本脚本**只读取语料**，输入只有 --root；**不读取、不引用** query 集与 ground truth（expect 字段）
 *   - 扩展词完全由语料内共现统计自动产生（PMI 式打分），无人工挑选、无按答案反推
 *   - 因此 M3 的词典来源独立于评测答案；但须承认"无法排除语料与答案的间接相关"（报告中声明）
 *
 * 用法:
 *   node mine-expansion.js --root "$KM_DATA_ROOT/logseq-main" --out ".\m3-expansion-dictionary.json" ^
 *        --min-co 2 --pmi-min 1.0 --top-m 3 --df-max-ratio 0.2
 *
 * 方法:
 *   1) 以「行」为共现窗口（比 chunk 更紧，减少噪声）
 *   2) 过滤: 出现在 [2, df-max-ratio*总行数] 之间的 token 才参与（去极端停用词与一次性噪声）
 *   3) 打分: pmi(t,u) = log( (co(t,u) * L) / (df(t) * df(u)) )，要求 co>=min-co 且 pmi>=pmi-min
 *   4) 每个 token 保留 pmi 最高的 top-m 个扩展词
 */
const fs = require('fs');
const path = require('path');
const { resolveIndexRoot } = require('./index-root');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] !== undefined ? process.argv[i + 1] : dflt;
}
const ROOT = resolveIndexRoot(arg('root', ''));
const OUT = arg('out', '');
const MIN_CO = Number(arg('min-co', '2'));
const PMI_MIN = Number(arg('pmi-min', '1.0'));
const TOPM = Number(arg('top-m', '3'));
const DF_MAX_RATIO = Number(arg('df-max-ratio', '0.2'));
const MAX_TOKENS_PER_LINE = Number(arg('max-tokens-per-line', '40'));
const DROP_DIGITS = process.argv.includes('--drop-digits'); // 领域无关规则：丢弃纯数字 token
const CHUNK_EXCLUDES = arg('exclude', 'logseq\\bak,.recycle,.git,node_modules').split(',').map(s => s.trim()).filter(Boolean);

function tokenize(text) {
  const t = String(text).toLowerCase();
  const out = [];
  const reAscii = /[a-z0-9_]{2,}/g;
  let m;
  while ((m = reAscii.exec(t)) !== null) out.push(m[0]);
  const cjk = t.replace(/[^\u4e00-\u9fff]+/g, ' ');
  for (const seg of cjk.split(' ')) {
    if (seg.length === 1) { out.push(seg); continue; }
    for (let i = 0; i + 2 <= seg.length; i++) out.push(seg.slice(i, i + 2));
  }
  return out;
}
function isExcluded(rel) {
  const norm = rel.split(path.sep).join('/');
  return CHUNK_EXCLUDES.some(e => {
    const n = e.split('\\').join('/');
    return norm === n || norm.includes('/' + n + '/') || norm.startsWith(n + '/') || norm.split('/').includes(n);
  });
}

// ---------- 读语料，按行收集 token（只读） ----------
const lines = []; // 每行: 去重后的 token 数组
const failed = [];
let fileCount = 0, totalBytes = 0;
(function walk(dir) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
  catch (e) { failed.push({ path: path.relative(ROOT, dir), error: e.code || String(e.message) }); return; }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    const rel = path.relative(ROOT, p);
    if (e.isDirectory()) { if (!isExcluded(rel)) walk(p); }
    else if (e.isFile() && /\.(md|markdown)$/i.test(e.name)) {
      let buf;
      try { buf = fs.readFileSync(p); } catch (err) { failed.push({ path: rel, error: err.code || String(err.message) }); continue; }
      fileCount++; totalBytes += buf.length;
      for (const raw of buf.toString('utf8').split(/\r?\n/)) {
        if (!raw.trim()) continue;
        const toks = Array.from(new Set(tokenize(raw)));
        if (toks.length) lines.push(toks);
      }
    }
  }
})(ROOT);

const L = lines.length;
const df = new Map();
for (const toks of lines) for (const t of toks) df.set(t, (df.get(t) || 0) + 1);
const DF_MAX = Math.max(2, Math.floor(DF_MAX_RATIO * L));
function eligible(t) { const d = df.get(t) || 0; if (DROP_DIGITS && /^[0-9]+$/.test(t)) return false; return d >= 2 && d <= DF_MAX; }

// ---------- 共现统计（两趟：先 df，再只对合格 token 计对） ----------
const co = new Map(); // key: "a\u0000b" (a<b)
function coKey(a, b) { return a < b ? a + '\u0000' + b : b + '\u0000' + a; }
let pairOps = 0;
for (const toks of lines) {
  const keep = toks.filter(eligible).slice(0, MAX_TOKENS_PER_LINE);
  for (let i = 0; i < keep.length; i++) {
    for (let j = i + 1; j < keep.length; j++) {
      const k = coKey(keep[i], keep[j]);
      co.set(k, (co.get(k) || 0) + 1);
      pairOps++;
    }
  }
}

// ---------- 打分与裁剪 ----------
const best = new Map(); // token -> [{t, co, pmi}] 仅保留 top-m
for (const [k, c] of co) {
  if (c < MIN_CO) continue;
  const [a, b] = k.split('\u0000');
  const da = df.get(a), db = df.get(b);
  const pmi = Math.log((c * L) / (da * db));
  if (pmi < PMI_MIN) continue;
  for (const [x, y] of [[a, b], [b, a]]) {
    let arr = best.get(x);
    if (!arr) { arr = []; best.set(x, arr); }
    arr.push({ t: y, co: c, pmi: Number(pmi.toFixed(3)) });
    arr.sort((p, q) => q.pmi - p.pmi || q.co - p.co);
    if (arr.length > TOPM) best.set(x, arr.slice(0, TOPM));
  }
}

const entries = {};
for (const [t, arr] of best) entries[t] = arr;

const dict = {
  meta: {
    generated_by: 'mine-expansion.js',
    method: 'unsupervised line-level co-occurrence + PMI filtering (corpus only)',
    independent_of_query_set: true,
    note: '构建过程不读取 query 集或 ground truth；扩展词由语料统计自动产生',
    root: ROOT,
    corpus: { md_files: fileCount, lines: L, bytes: totalBytes, vocab: df.size, read_failures: failed.length },
    params: { min_co: MIN_CO, pmi_min: PMI_MIN, top_m: TOPM, df_max_ratio: DF_MAX_RATIO, df_max: DF_MAX, max_tokens_per_line: MAX_TOKENS_PER_LINE },
    stats: { pair_ops: pairOps, raw_pairs: co.size, kept_source_tokens: Object.keys(entries).length, total_expansions: Object.values(entries).reduce((s, a) => s + a.length, 0) },
  },
  entries,
};

if (OUT) fs.writeFileSync(OUT, JSON.stringify(dict));
console.log(JSON.stringify({
  out: OUT || '(not written)',
  corpus: dict.meta.corpus, params: dict.meta.params, stats: dict.meta.stats,
  sample: Object.fromEntries(Object.entries(entries).slice(0, 8)),
}, null, 2));