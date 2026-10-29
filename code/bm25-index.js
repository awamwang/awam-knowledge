'use strict';
/**
 * 共享 BM25 语料模块（零依赖）
 *
 * 被 smoke-index.js / eval-recall.js / mcp-server.js 共用，避免三份分词与打分实现漂移。
 * 口径固定：纯文本切块（默认 20 行/块）+ 中文 bigram + BM25(k1=1.2, b=0.75) + 标题 token 加权。
 * 只读语料；不写任何文件。
 */
const fs = require('fs');
const path = require('path');
const { readJson, writeJsonAtomic } = require('./fs-util');

const DEFAULT_CHUNK_LINES = 20;
const DEFAULT_TITLE_WEIGHT = 4;
const DEFAULT_EXCLUDES = 'logseq\\bak,.recycle,.git,node_modules,.km,vault';

function normPath(p) { return String(p).split(path.sep).join('/').split('\\').join('/'); }

function stemAscii(t) {
  let s = t;
  if (s.length > 4 && s.endsWith('ies')) s = s.slice(0, -3) + 'y';
  else if (s.length > 4 && s.endsWith('es')) s = s.slice(0, -2);
  else if (s.length > 3 && s.endsWith('s') && !s.endsWith('ss')) s = s.slice(0, -1);
  if (s.length > 5 && s.endsWith('ing')) s = s.slice(0, -3);
  else if (s.length > 4 && s.endsWith('ed')) s = s.slice(0, -2);
  return s;
}

function tokenize(text, stem) {
  const t = String(text).toLowerCase();
  const out = [];
  const reAscii = /[a-z0-9_]{2,}/g;
  let m;
  while ((m = reAscii.exec(t)) !== null) out.push(stem ? stemAscii(m[0]) : m[0]);
  const cjk = t.replace(/[^\u4e00-\u9fff]+/g, ' ');
  for (const seg of cjk.split(' ')) {
    if (seg.length === 1) { out.push(seg); continue; }
    for (let i = 0; i + 2 <= seg.length; i++) out.push(seg.slice(i, i + 2));
  }
  return out;
}

function parseExcludes(value) {
  const raw = value === undefined || value === null ? DEFAULT_EXCLUDES : String(value);
  return raw.split(',').map(s => s.trim()).filter(Boolean);
}

function isExcluded(relPath, excludes) {
  const norm = normPath(relPath);
  return excludes.some(e => {
    const n = normPath(e);
    return norm === n || norm.includes('/' + n + '/') || norm.startsWith(n + '/') || norm.split('/').includes(n);
  });
}

/** 脱敏仓 build_index.py 的旁路产物；缺失返回 null（保持旧行为）。 */
function loadSidecarIndex(root) {
  const ip = path.join(root, 'index.json');
  if (!fs.existsSync(ip)) return null;
  try {
    const j = JSON.parse(fs.readFileSync(ip, 'utf8'));
    const byPath = new Map();
    for (const d of (j.docs || [])) if (d && d.path) byPath.set(normPath(d.path), d);
    return {
      byPath,
      count: j.count || byPath.size,
      generated_at: j.generated_at || null,
      generated_from: j.generated_from || null,
    };
  } catch (e) {
    return { byPath: new Map(), error: String(e && e.message || e) };
  }
}

function makeDoc(relPath, title, lines, start, opts) {
  const body = lines.slice(start, start + opts.chunkLines).join('\n');
  if (!body.trim()) return null;
  const tokens = tokenize(body, opts.stem);
  const titleToks = tokenize(title, opts.stem);
  for (let i = 0; i < opts.titleWeight; i++) tokens.push(...titleToks);
  const tf = new Map();
  for (const tk of tokens) tf.set(tk, (tf.get(tk) || 0) + 1);
  return {
    path: normPath(relPath), title,
    lineStart: start + 1, lineEnd: Math.min(start + opts.chunkLines, lines.length),
    len: tokens.length, tf, text: body,
  };
}

/**
 * @param {{root:string, excludes?:string|string[], chunkLines?:number, titleWeight?:number, stem?:boolean}} opts
 * @returns {{docs:Array, failed:Array, scanned:Array, skipped:string[], zeroByte:Array, encodingWarn:Array, totalBytes:number, totalLines:number, sidecar:object|null}}
 */
function buildCorpus(opts) {
  const root = opts.root;
  const excludes = Array.isArray(opts.excludes) ? opts.excludes : parseExcludes(opts.excludes);
  const settings = {
    chunkLines: opts.chunkLines || DEFAULT_CHUNK_LINES,
    titleWeight: opts.titleWeight == null ? DEFAULT_TITLE_WEIGHT : Number(opts.titleWeight),
    stem: !!opts.stem,
  };
  const docs = [], failed = [], scanned = [], skipped = [], zeroByte = [], encodingWarn = [];
  let totalBytes = 0, totalLines = 0;
  const sidecar = loadSidecarIndex(root);

  (function walk(dir) {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
    catch (e) { failed.push({ path: normPath(path.relative(root, dir)), stage: 'readdir', error: e.code || String(e.message) }); return; }
    for (const e of entries) {
      const p = path.join(dir, e.name);
      const rel = path.relative(root, p);
      if (e.isDirectory()) { if (!isExcluded(rel, excludes)) walk(p); else skipped.push(normPath(rel)); continue; }
      if (!e.isFile() || !/\.(md|markdown)$/i.test(e.name)) continue;
      scanned.push(rel);
      let buf, raw;
      try { buf = fs.readFileSync(p); raw = buf.toString('utf8'); }
      catch (err) { failed.push({ path: normPath(rel), stage: 'read', error: err.code || String(err.message) }); continue; }
      if (buf.length === 0) zeroByte.push(normPath(rel));
      const repl = (raw.match(/\uFFFD/g) || []).length;
      if (raw.length && repl / raw.length > 0.01) encodingWarn.push({ path: normPath(rel), replacement_chars: repl, bytes: buf.length });
      totalBytes += buf.length;
      const lines = raw.split(/\r?\n/);
      totalLines += lines.length;
      const fileTitle = e.name.replace(/\.(md|markdown)$/i, '');
      const meta = sidecar && sidecar.byPath ? sidecar.byPath.get(normPath(rel)) : null;
      const title = (meta && meta.title) ? String(meta.title) : fileTitle;
      for (let s = 0; s < lines.length; s += settings.chunkLines) {
        const d = makeDoc(rel, title, lines, s, settings);
        if (!d) continue;
        if (meta && meta.type) d.docType = meta.type;
        docs.push(d);
      }
    }
  })(root);

  return { docs, failed, scanned, skipped, zeroByte, encodingWarn, totalBytes, totalLines, sidecar };
}

const CACHE_VERSION = 2;

function encodeDocs(docs) {
  return docs.map(d => ({
    lineStart: d.lineStart, lineEnd: d.lineEnd, len: d.len,
    tf: [...d.tf], text: d.text,
    ...(d.docType ? { docType: d.docType } : {}),
  }));
}

function decodeDocs(rel, title, arr) {
  return arr.map(d => ({
    path: normPath(rel), title,
    lineStart: d.lineStart, lineEnd: d.lineEnd, len: d.len,
    tf: new Map(d.tf), text: d.text,
    ...(d.docType ? { docType: d.docType } : {}),
  }));
}

function sameSettings(a, b) {
  if (!a || !b) return false;
  return a.chunkLines === b.chunkLines && a.titleWeight === b.titleWeight &&
    !!a.stem === !!b.stem && String(a.excludes) === String(b.excludes);
}

/**
 * 带缓存的语料构建：按 (mtime, size, title) 判定文件是否变化，未变则复用 chunk。
 * 缓存键里含切块与加权参数，参数变了整体重建；删除的文件会从缓存中剔除。
 *
 * @param {{root:string, excludes?:string|string[], chunkLines?:number, titleWeight?:number,
 *          stem?:boolean, cachePath?:string, force?:boolean}} opts
 */
function buildCorpusCached(opts) {
  const root = opts.root;
  const excludes = Array.isArray(opts.excludes) ? opts.excludes : parseExcludes(opts.excludes);
  const settings = {
    chunkLines: opts.chunkLines || DEFAULT_CHUNK_LINES,
    titleWeight: opts.titleWeight == null ? DEFAULT_TITLE_WEIGHT : Number(opts.titleWeight),
    stem: !!opts.stem,
    excludes: excludes.join(','),
  };
  const cachePath = opts.cachePath;
  const prev = opts.force || !cachePath ? null : readJson(cachePath, null);
  const compatible = !!(prev && prev.version === CACHE_VERSION && sameSettings(prev.settings, settings));
  const prevFiles = compatible && prev.files ? prev.files : {};

  const sidecar = loadSidecarIndex(root);
  const docs = [], failed = [], scanned = [], skipped = [], zeroByte = [], encodingWarn = [];
  const nextFiles = {};
  let totalBytes = 0, totalLines = 0, hits = 0, misses = 0;

  const files = [];
  (function walk(dir) {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
    catch (e) { failed.push({ path: normPath(path.relative(root, dir)), stage: 'readdir', error: e.code || String(e.message) }); return; }
    for (const e of entries) {
      const p = path.join(dir, e.name);
      const rel = path.relative(root, p);
      if (e.isDirectory()) { if (!isExcluded(rel, excludes)) walk(p); else skipped.push(normPath(rel)); continue; }
      if (e.isFile() && /\.(md|markdown)$/i.test(e.name)) files.push({ abs: p, rel, name: e.name });
    }
  })(root);

  for (const f of files) {
    const relN = normPath(f.rel);
    scanned.push(f.rel);
    let st;
    try { st = fs.statSync(f.abs); }
    catch (e) { failed.push({ path: relN, stage: 'stat', error: e.code || String(e.message) }); continue; }
    const meta = sidecar && sidecar.byPath ? sidecar.byPath.get(relN) : null;
    const title = (meta && meta.title) ? String(meta.title) : f.name.replace(/\.(md|markdown)$/i, '');
    const mtimeMs = Math.floor(st.mtimeMs);
    const cached = prevFiles[relN];
    if (cached && cached.size === st.size && cached.mtime_ms === mtimeMs && cached.title === title) {
      hits++;
      docs.push(...decodeDocs(relN, title, cached.docs || []));
      totalBytes += cached.bytes || 0;
      totalLines += cached.lines || 0;
      if (cached.zero) zeroByte.push(relN);
      if (cached.repl && cached.repl / Math.max(1, cached.chars) > 0.01) {
        encodingWarn.push({ path: relN, replacement_chars: cached.repl, bytes: cached.size });
      }
      nextFiles[relN] = cached;
      continue;
    }

    let buf, raw;
    try { buf = fs.readFileSync(f.abs); raw = buf.toString('utf8'); }
    catch (err) { failed.push({ path: relN, stage: 'read', error: err.code || String(err.message) }); continue; }
    misses++;
    const repl = (raw.match(/\uFFFD/g) || []).length;
    const lines = raw.split(/\r?\n/);
    const fileDocs = [];
    for (let s = 0; s < lines.length; s += settings.chunkLines) {
      const d = makeDoc(f.rel, title, lines, s, settings);
      if (!d) continue;
      if (meta && meta.type) d.docType = meta.type;
      fileDocs.push(d);
    }
    docs.push(...fileDocs);
    totalBytes += buf.length;
    totalLines += lines.length;
    if (buf.length === 0) zeroByte.push(relN);
    if (raw.length && repl / raw.length > 0.01) encodingWarn.push({ path: relN, replacement_chars: repl, bytes: buf.length });
    nextFiles[relN] = {
      mtime_ms: mtimeMs, size: st.size, title, bytes: buf.length, lines: lines.length,
      zero: buf.length === 0, repl, chars: raw.length, docs: encodeDocs(fileDocs),
    };
  }

  let removed = 0;
  for (const rel of Object.keys(prevFiles)) if (!nextFiles[rel]) removed++;

  let saved = false;
  if (cachePath && (misses > 0 || removed > 0 || !compatible)) {
    writeJsonAtomic(cachePath, {
      version: CACHE_VERSION, generated_at: new Date().toISOString(),
      settings, files: nextFiles,
    });
    saved = true;
  }

  return {
    docs, failed, scanned, skipped, zeroByte, encodingWarn, totalBytes, totalLines, sidecar,
    cache: { enabled: !!cachePath, hits, misses, removed, saved, compatible },
  };
}

function buildIndex(docs) {
  const df = new Map();
  for (const d of docs) for (const tk of d.tf.keys()) df.set(tk, (df.get(tk) || 0) + 1);
  const N = docs.length;
  const avgdl = docs.reduce((s, d) => s + d.len, 0) / (N || 1);
  return { df, N, avgdl };
}

function queryWeights(query, opts) {
  const w = new Map();
  for (const t of tokenize(query, opts && opts.stem)) w.set(t, 1);
  return w;
}

/** 返回全部得分 > 0 的块，按分降序。 */
function rankAll(docs, idx, weightMap) {
  const k1 = 1.2, b = 0.75;
  const { df, N, avgdl } = idx;
  const out = [];
  for (let di = 0; di < docs.length; di++) {
    const d = docs[di];
    let score = 0;
    for (const [tk, wq] of weightMap) {
      const f = d.tf.get(tk);
      if (!f) continue;
      const n = df.get(tk) || 1;
      const idf = Math.log(1 + (N - n + 0.5) / (n + 0.5));
      score += wq * idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * d.len / avgdl));
    }
    if (score > 0) out.push({ di, path: d.path, title: d.title, lineStart: d.lineStart, lineEnd: d.lineEnd, score });
  }
  out.sort((a, b2) => b2.score - a.score);
  return out;
}

function snippetOf(text, terms, width) {
  const limit = width || 160;
  const flat = String(text).replace(/\s+/g, ' ').trim();
  let at = -1;
  for (const t of terms) {
    const i = flat.toLowerCase().indexOf(t);
    if (i !== -1 && (at === -1 || i < at)) at = i;
  }
  if (at === -1) return flat.slice(0, limit);
  const from = Math.max(0, at - Math.floor(limit / 3));
  return (from > 0 ? '…' : '') + flat.slice(from, from + limit) + (from + limit < flat.length ? '…' : '');
}

/** 简化检索入口：返回带行号与片段的命中。 */
function search(corpus, idx, query, opts) {
  opts = opts || {};
  const topk = Number(opts.topk || 5);
  const weights = opts.weights || queryWeights(query, { stem: opts.stem });
  const terms = [...weights.keys()];
  return rankAll(corpus.docs, idx, weights).slice(0, topk).map(r => {
    const d = corpus.docs[r.di];
    return {
      path: r.path,
      line: r.lineStart,
      lines: r.lineStart + '-' + r.lineEnd,
      title: r.title,
      score: Number(r.score.toFixed(4)),
      snippet: snippetOf(d.text, terms, opts.snippetWidth),
    };
  });
}

module.exports = {
  DEFAULT_CHUNK_LINES,
  DEFAULT_TITLE_WEIGHT,
  DEFAULT_EXCLUDES,
  normPath,
  stemAscii,
  tokenize,
  parseExcludes,
  isExcluded,
  loadSidecarIndex,
  makeDoc,
  buildCorpus,
  buildCorpusCached,
  buildIndex,
  queryWeights,
  rankAll,
  snippetOf,
  search,
};
