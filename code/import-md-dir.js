'use strict';
/**
 * 通用能力：带附件的 Markdown 目录导入（零依赖，只读源目录）
 *
 *   node import-md-dir.js --root "<dir>" [--manifest "<out.json>"] [--report "<out.md>"]
 *                         [--skip ".git,node_modules,.recycle,bak"] [--max-list 50]
 *                         [--resume "<prev-manifest.json>"] [--no-scan-secrets]
 *   node import-md-dir.js --selftest --selftest-root "<dir>"
 *
 * 能力:
 *   1) Markdown 摄取（.md/.markdown）——条目、标题、字节、行数、SHA256
 *   2) 附件识别（image/av/doc/other）+ SHA256、零字节标记、同内容去重
 *   3) 链接解析（深度感知扫描器）：`[t](dest)` / `![t](dest)` / `<img src>` / 引用式 / `<dest with space>`
 *      —— 处理目标里的成对圆括号、引号包裹、`[别名]([[页面]])` 内嵌双链
 *   4) 密钥模式扫描（只报文件名 / 规则 / 次数，不记录命中值）
 *   5) 编码异常检测（U+FFFD 占比 > 1%）
 *   6) 断点/幂等：--resume 传入上次 manifest，文件 (size, mtime) 未变则复用条目
 *   7) 平台归一与 Logseq 语义：路径键 NFC + win32 大小写比对；双链目标存在性、块引用 ((uuid)) 按本批 id:: 对账
 *
 * 边界:
 *   - 源目录只读；只写 --manifest / --report 指定的文件
 *   - manifest 不含正文；附件只校验路径存在 + SHA256，不做内容解码
 *   - 双链只统计并判断目标页面是否在本批导入内（NFC + win32 大小写归一）；块引用按 `((uuid))` 与
 *     `id:: uuid` 定义对账，不解析页面属性/别名，也不判断块所在页面
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const os = require('os');

const DFLT_SKIP = ['.git', 'node_modules', '.recycle'];

const IMG_RE = /<img[^>]*src=["']([^"']+)["']/gi;
const REFDEF_RE = /^\s*\[[^\]]+\]:\s*(\S+)/gm;
const WIKILINK_RE = /(!)?\[\[([^\]|]+)(\|[^\]]*)?\]\]/g;

const IMAGE_EXT = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.ico', '.tif', '.tiff', '.avif']);
const AV_EXT = new Set(['.mp4', '.mov', '.webm', '.mp3', '.wav', '.m4a', '.flac', '.ogg']);
const DOC_EXT = new Set(['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.csv', '.txt']);

const SECRET_PATTERNS = [
  { id: 'private_key', re: /-----BEGIN [A-Z ]*PRIVATE KEY-----/g },
  { id: 'openai_key', re: /\bsk-[A-Za-z0-9_-]{20,}\b/g },
  { id: 'aws_access_key', re: /\bAKIA[0-9A-Z]{16}\b/g },
  { id: 'github_token', re: /\bgh[pousr]_[A-Za-z0-9]{20,}\b/g },
  { id: 'slack_token', re: /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/g },
  { id: 'google_api_key', re: /\bAIza[0-9A-Za-z_-]{30,}\b/g },
  { id: 'jwt', re: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g },
];

function parseSkip(value) {
  const raw = value === undefined || value === null ? DFLT_SKIP.join(',') : String(value);
  return new Set(raw.split(',').map(s => s.trim()).filter(Boolean));
}

function attachKind(ext) {
  if (IMAGE_EXT.has(ext)) return 'image';
  if (AV_EXT.has(ext)) return 'av';
  if (DOC_EXT.has(ext)) return 'doc';
  return 'other';
}
function isExternal(t) { return /^(https?:|mailto:|data:|tel:|ftp:)/i.test(t); }
function isAbsolute(t) { return /^[a-zA-Z]:[\\/]/.test(t) || /^\\\\/.test(t) || t.startsWith('/'); }
function norm(p) { return p.split('\\').join('/'); }
function sha256(p) { return crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex'); }
function clean(t) {
  let s = String(t).trim();
  if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) s = s.slice(1, -1).trim();
  if (s.startsWith('<') && s.endsWith('>')) s = s.slice(1, -1).trim();
  return s;
}
function isWiki(t) { return /^\[\[[^\]]+\]\]$/.test(t.trim()); }

// 平台差异：macOS 文件名可能是 NFD，Windows 路径大小写不敏感；比对键统一成 NFC +（win32）小写
const IS_WIN = process.platform === 'win32';
const WIN_LONG_PATH = 240;
const nfc = s => String(s).normalize('NFC');
const matchKey = s => { const v = nfc(s); return IS_WIN ? v.toLowerCase() : v; };

const BLOCK_REF_RE = /\(\(([0-9a-fA-F][0-9a-fA-F-]{7,})\)\)/g;
const ID_DEF_RE = /^\s*id::\s*([0-9a-fA-F][0-9a-fA-F-]{7,})\s*$/gm;

/** 深度感知的 Markdown 行内链接扫描器（成对圆括号、<...> 目标、转义、引号包裹）。 */
function extractLinks(txt) {
  const out = [];
  let i = 0;
  while ((i = txt.indexOf('](', i)) !== -1) {
    let j = i + 2;
    while (j < txt.length && (txt[j] === ' ' || txt[j] === '\t')) j++;
    let target = null, end = -1;
    if (txt[j] === '<') {
      const gt = txt.indexOf('>', j + 1);
      if (gt !== -1) {
        let k = gt + 1;
        while (k < txt.length && (txt[k] === ' ' || txt[k] === '\t')) k++;
        if (txt[k] === ')') { target = txt.slice(j + 1, gt); end = k; }
      }
    } else {
      let depth = 0, k = j;
      for (; k < txt.length && k - j < 600; k++) {
        const ch = txt[k];
        if (ch === '\\') { k++; continue; }
        if (ch === '\n' || ch === '\r') break;
        if (ch === '(') depth++;
        else if (ch === ')') { if (depth === 0) break; depth--; }
      }
      if (k < txt.length && txt[k] === ')' && k > j) { target = txt.slice(j, k); end = k; }
    }
    if (target !== null) {
      out.push({ target: clean(target), form: 'md-link' });
      i = end + 1;
    } else i = i + 2;
  }
  return out;
}

function emptyCounters() {
  return {
    links_total: 0, resolved: 0, missing: 0, external: 0, absolute: 0, anchor: 0, escapes_root: 0,
    to_markdown: 0, is_directory: 0, wiki_in_link: 0, wiki_in_link_page_exists: 0,
    fallback_unique: 0, fallback_ambiguous: 0, fallback_none: 0,
    block_refs_total: 0, block_refs_resolved: 0, block_refs_unresolved: 0,
    wiki_pages_total: 0, wiki_pages_resolved: 0,
  };
}

function mergeCounters(into, from) {
  for (const k of Object.keys(from || {})) into[k] = (into[k] || 0) + Number(from[k] || 0);
}

function scanSecrets(raw) {
  const hits = [];
  for (const p of SECRET_PATTERNS) {
    const m = raw.match(p.re);
    if (m && m.length) hits.push({ rule: p.id, count: m.length });
  }
  return hits;
}

function collect(root, skip) {
  const mdFiles = [], attachAll = [], walkErrors = [];
  let longPaths = 0;
  (function walk(dir) {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
    catch (e) { walkErrors.push({ path: norm(path.relative(root, dir)), error: e.code || String(e.message) }); return; }
    for (const e of entries) {
      const p = path.join(dir, e.name);
      if (IS_WIN && p.length > WIN_LONG_PATH) longPaths++;
      if (e.isDirectory()) { if (!skip.has(e.name)) walk(p); }
      else if (e.isFile()) {
        const rel = nfc(path.relative(root, p));
        if (/\.(md|markdown)$/i.test(e.name)) mdFiles.push({ rel, abs: p, name: e.name });
        else attachAll.push({ rel, abs: p, ext: path.extname(e.name).toLowerCase(), bytes: fs.statSync(p).size });
      }
    }
  })(root);
  return { mdFiles, attachAll, walkErrors, longPaths };
}

/**
 * 逐文件推进的导入流程。
 * yield：{phase, done, total, path}
 * return：完整结果（cancelled 时为 {cancelled:true}）
 */
function* importSteps(root, skip, opts) {
  opts = opts || {};
  const scanSecretsEnabled = opts.scanSecrets !== false;
  const resume = opts.resume && typeof opts.resume === 'object' ? opts.resume : null;
  const checkpoint = payload => {
    if (typeof opts.checkpoint !== 'function') return;
    try { opts.checkpoint(payload); } catch (e) { /* 检查点写失败不影响导入本身 */ }
  };
  const prevByPath = new Map();
  if (resume && Array.isArray(resume.markdown)) {
    for (const e of resume.markdown) if (e && e.path) prevByPath.set(matchKey(e.path), e);
  }

  const { mdFiles, attachAll, walkErrors, longPaths } = collect(root, skip);
  yield { phase: 'analyzing', done: 0, total: mdFiles.length, path: '' };

  const byBase = new Map();
  for (const f of [...mdFiles, ...attachAll]) {
    const b = matchKey(path.basename(f.rel));
    if (!byBase.has(b)) byBase.set(b, []);
    byBase.get(b).push(norm(f.rel));
  }
  const pagesIdx = new Set(mdFiles.map(f => matchKey(norm(f.rel).replace(/^pages\//, '').replace(/\.(md|markdown)$/i, ''))));

  const mdEntries = [], unresolved = [], attachRefs = new Map();
  const counters = emptyCounters();
  const security = { scan: scanSecretsEnabled, files_with_hits: 0, hits_total: 0, rules: {}, files: [] };
  let wikilinks = 0, refdefs = 0, htmlImg = 0, totalBytes = 0;
  let unchanged = 0;
  const blockRefPending = [];      // 块引用要等所有文件的 id:: 读完才能判是否可解析
  const blockRefUnresolved = [];
  const blockIds = new Set();

  function addAttachRef(ref, fromRel) {
    const k = matchKey(ref);
    if (!attachRefs.has(k)) attachRefs.set(k, { path: ref, by: new Set() });
    attachRefs.get(k).by.add(fromRel);
  }

  for (let i = 0; i < mdFiles.length; i++) {
    if (opts.shouldCancel && opts.shouldCancel()) return { cancelled: true, processed: i, phase: 'analyzing' };
    const f = mdFiles[i];
    const relN = norm(f.rel);

    let st;
    try { st = fs.statSync(f.abs); }
    catch (e) { walkErrors.push({ path: relN, error: 'stat:' + (e.code || String(e.message)) }); continue; }

    const prev = prevByPath.get(matchKey(relN));
    if (prev && prev.size === st.size && prev.mtime_ms === Math.floor(st.mtimeMs) && prev.counters) {
      unchanged++;
      mdEntries.push(Object.assign({}, prev, { unchanged: true }));
      mergeCounters(counters, prev.counters);
      for (const u of prev.unresolved || []) unresolved.push(u);
      for (const r of prev.refs || []) addAttachRef(r, relN);
      for (const id of prev.block_ids || []) blockIds.add(id);   // 未变文件不重读，id:: 定义从 manifest 带回
      if (scanSecretsEnabled) {
        for (const h of prev.security || []) addSecurity(security, relN, h.rule, h.count);
      }
      checkpoint({ phase: 'analyzing', done: i + 1, total: mdFiles.length, markdown: mdEntries });
      yield { phase: 'analyzing', done: i + 1, total: mdFiles.length, path: relN };
      continue;
    }

    let txt;
    try { txt = fs.readFileSync(f.abs, 'utf8'); }
    catch (e) { walkErrors.push({ path: relN, error: 'read:' + (e.code || String(e.message)) }); continue; }

    const bytes = Buffer.byteLength(txt);
    totalBytes += bytes;
    const dirAbs = path.dirname(f.abs);
    const targets = extractLinks(txt);
    const fileCounters = emptyCounters();
    const fileUnresolved = [];
    const fileRefs = [];
    let m;
    while ((m = IMG_RE.exec(txt)) !== null) { targets.push({ target: clean(m[1]), form: 'html-img' }); htmlImg++; }
    while ((m = REFDEF_RE.exec(txt)) !== null) { targets.push({ target: clean(m[1]), form: 'ref-def' }); refdefs++; }
    while ((m = WIKILINK_RE.exec(txt)) !== null) {
      wikilinks++;
      const target = matchKey(String(m[2]).trim());
      fileCounters.wiki_pages_total++;
      if (pagesIdx.has(target) || pagesIdx.has(target.replace(/\//g, '.'))) fileCounters.wiki_pages_resolved++;
    }
    const fileBlockRefs = [];
    BLOCK_REF_RE.lastIndex = 0;
    while ((m = BLOCK_REF_RE.exec(txt)) !== null) fileBlockRefs.push(m[1].toLowerCase());
    ID_DEF_RE.lastIndex = 0;
    const fileIds = [];
    while ((m = ID_DEF_RE.exec(txt)) !== null) {
      const id = m[1].toLowerCase();
      if (fileIds.indexOf(id) === -1) fileIds.push(id);
      blockIds.add(id);
    }

    fileCounters.block_refs_total = fileBlockRefs.length;
    for (const t of targets) {
      fileCounters.links_total++;
      const raw = t.target;
      if (isWiki(raw)) {
        fileCounters.wiki_in_link++;
        const name = raw.slice(2, -2).split('|')[0].trim();
        if (pagesIdx.has(matchKey(name)) || pagesIdx.has(matchKey(name.replace(/\//g, '.')))) fileCounters.wiki_in_link_page_exists++;
        continue;
      }
      if (isExternal(raw)) { fileCounters.external++; continue; }
      if (raw.startsWith('#')) { fileCounters.anchor++; continue; }
      if (isAbsolute(raw)) {
        fileCounters.absolute++;
        fileUnresolved.push({ file: relN, target: raw, cause: 'absolute_path', form: t.form });
        continue;
      }
      let dec = raw.split('#')[0].split('?')[0];
      try { dec = decodeURIComponent(dec); } catch (e) { /* keep */ }
      const abs = path.resolve(dirAbs, dec);
      const inside = nfc(norm(path.relative(root, abs)));
      const escapesRoot = inside.startsWith('..') || path.isAbsolute(inside);
      if (!fs.existsSync(abs)) {
        fileCounters.missing++;
        const cand = (byBase.get(matchKey(path.basename(dec))) || []).filter(x => matchKey(x) !== matchKey(inside));
        if (cand.length === 1) fileCounters.fallback_unique++;
        else if (cand.length > 1) fileCounters.fallback_ambiguous++;
        else fileCounters.fallback_none++;
        fileUnresolved.push({
          file: relN, target: raw, cause: 'missing', form: t.form, would_resolve_to: inside,
          basename_candidates: cand.length, basename_sample: cand.slice(0, 3),
        });
        continue;
      }
      if (escapesRoot) {
        fileCounters.escapes_root++;
        fileUnresolved.push({ file: relN, target: raw, cause: 'escapes_import_root', form: t.form, would_resolve_to: inside });
        continue;
      }
      const st2 = fs.statSync(abs);
      if (st2.isDirectory()) {
        fileCounters.is_directory++;
        fileUnresolved.push({ file: relN, target: raw, cause: 'is_directory', form: t.form });
        continue;
      }
      if (/\.(md|markdown)$/i.test(abs)) { fileCounters.to_markdown++; fileCounters.resolved++; continue; }
      fileCounters.resolved++;
      const ref = nfc(norm(path.relative(root, abs)));
      if (fileRefs.indexOf(ref) === -1) fileRefs.push(ref);
      addAttachRef(ref, relN);
    }

    const fileSecurity = scanSecretsEnabled ? scanSecrets(txt) : [];
    for (const h of fileSecurity) addSecurity(security, relN, h.rule, h.count);

    mergeCounters(counters, fileCounters);
    for (const u of fileUnresolved) unresolved.push(u);

    mdEntries.push({
      path: relN,
      title: path.basename(f.rel).replace(/\.(md|markdown)$/i, ''),
      bytes, lines: txt.split(/\r?\n/).length,
      sha256: crypto.createHash('sha256').update(txt).digest('hex'),
      size: st.size, mtime_ms: Math.floor(st.mtimeMs),
      unchanged: false,
      counters: fileCounters, refs: fileRefs, unresolved: fileUnresolved, security: fileSecurity,
      block_ids: fileIds,
    });
    if (fileBlockRefs.length) blockRefPending.push({ relN, refs: fileBlockRefs, counters: fileCounters, entry: mdEntries[mdEntries.length - 1] });
    checkpoint({ phase: 'analyzing', done: i + 1, total: mdFiles.length, markdown: mdEntries });
    yield { phase: 'analyzing', done: i + 1, total: mdFiles.length, path: relN };
  }

  // 块引用要等全部文件的 id:: 读完才能判定是否可解析
  for (const p of blockRefPending) {
    let ok = 0;
    for (const u of p.refs) {
      if (blockIds.has(u)) ok++;
      else blockRefUnresolved.push({ file: p.relN, target: '((' + u + '))', cause: 'block_ref_missing', form: 'block-ref' });
    }
    p.counters.block_refs_resolved = ok;
    p.counters.block_refs_unresolved = p.refs.length - ok;
    // 断点复用时未变文件的计数已在 mergeCounters 里带进来，这里只做累加
    counters.block_refs_resolved += ok;
    counters.block_refs_unresolved += p.refs.length - ok;
  }
  for (const u of blockRefUnresolved) unresolved.push(u);

  yield { phase: 'attachments', done: 0, total: attachAll.length, path: '' };
  const attachRegistry = [];
  let zeroByteAttachments = 0;
  for (let i = 0; i < attachAll.length; i++) {
    if (opts.shouldCancel && opts.shouldCancel()) return { cancelled: true, processed: i, phase: 'attachments' };
    const a = attachAll[i];
    const relN = norm(a.rel);
    let hash = null;
    try { hash = sha256(a.abs); }
    catch (e) { walkErrors.push({ path: relN, error: 'read:' + (e.code || String(e.message)) }); }
    if (a.bytes === 0) zeroByteAttachments++;
    attachRegistry.push({
      path: relN, kind: attachKind(a.ext), ext: a.ext, bytes: a.bytes, sha256: hash,
      zero_byte: a.bytes === 0,
      referenced_by: [...((attachRefs.get(matchKey(relN)) || { by: [] }).by)],
      referenced: attachRefs.has(matchKey(relN)),
    });
    if ((i + 1) % 25 === 0) yield { phase: 'attachments', done: i + 1, total: attachAll.length, path: relN };
  }
  attachRegistry.sort((a, b) => a.path.localeCompare(b.path));

  const byHash = new Map();
  for (const a of attachRegistry) {
    if (!a.sha256) continue;
    if (!byHash.has(a.sha256)) byHash.set(a.sha256, []);
    byHash.get(a.sha256).push(a.path);
  }
  const duplicateGroups = [...byHash.values()].filter(g => g.length > 1)
    .map(g => ({ sha256_group: g.length, paths: g }));

  const orphan = attachRegistry.filter(a => !a.referenced).map(a => a.path);
  const missingInTree = [...attachRefs.values()].map(e => e.path)
    .filter(r => !attachAll.some(a => matchKey(a.rel) === matchKey(r)));
  const byKind = {};
  for (const a of attachRegistry) byKind[a.kind] = (byKind[a.kind] || 0) + 1;
  const denom = counters.resolved + counters.missing;

  security.hits_total = security.files.reduce((s, f) => s + f.count, 0);
  security.files_with_hits = security.files.length;

  const summary = {
    root: norm(root), skip: [...skip],
    markdown: { files: mdEntries.length, bytes: totalBytes, unchanged },
    attachments: {
      files: attachRegistry.length,
      bytes: attachRegistry.reduce((s, a) => s + a.bytes, 0),
      by_kind: byKind,
      referenced: attachRegistry.length - orphan.length,
      never_referenced: orphan.length,
      zero_byte: zeroByteAttachments,
      duplicate_groups: duplicateGroups.length,
    },
    links: {
      ...counters, html_img_links: htmlImg, wikilinks_info: wikilinks, refdefs_info: refdefs,
      block_refs: {
        total: counters.block_refs_total,
        resolved: counters.block_refs_resolved,
        unresolved: counters.block_refs_unresolved,
        id_defs: blockIds.size,
      },
      resolve_rate_on_relative: denom ? Number(((counters.resolved / denom) * 100).toFixed(2)) : null,
      resolve_rate_denominator: 'resolved + missing（相对路径文件引用）',
    },
    security: {
      scan: scanSecretsEnabled, files_with_hits: security.files_with_hits,
      hits_total: security.hits_total, rules: security.rules,
    },
    integrity: {
      walk_errors: walkErrors.length,
      long_paths: IS_WIN ? longPaths : null,
      orphan_attachments: orphan.length,
      referenced_but_absent: missingInTree.length,
      zero_byte_attachments: zeroByteAttachments,
      duplicate_attachment_groups: duplicateGroups.length,
    },
    resume: { used: !!resume, unchanged_files: unchanged },
    unresolved_total: unresolved.length,
  };

  return {
    summary, markdown: mdEntries, attachments: attachRegistry, unresolved, walkErrors, orphan,
    duplicateGroups, security,
  };
}

function addSecurity(bucket, file, rule, count) {
  bucket.rules[rule] = (bucket.rules[rule] || 0) + count;
  const found = bucket.files.find(f => f.file === file);
  if (found) {
    found.count += count;
    if (!found.rules.includes(rule)) found.rules.push(rule);
  } else {
    bucket.files.push({ file, count, rules: [rule] });
  }
}

function importDir(root, skip, opts) {
  const it = importSteps(root, skip, opts);
  let step = it.next();
  while (!step.done) step = it.next();
  return step.value;
}

// ---------------- selftest ----------------
function selftest(root) {
  fs.mkdirSync(path.join(root, 'img'), { recursive: true });
  fs.mkdirSync(path.join(root, 'sub'), { recursive: true });
  fs.writeFileSync(path.join(root, 'img', 'p.png'), Buffer.alloc(100, 1));
  fs.writeFileSync(path.join(root, 'img', 'p(1).png'), Buffer.alloc(60, 3));
  fs.writeFileSync(path.join(root, 'img', 'orphan.png'), Buffer.alloc(50, 2));
  fs.writeFileSync(path.join(root, 'a.md'),
    '# A\n\n![p](img/p.png)\n\n![paren](img/p(1).png)\n\n[ext]("https://example.com/x")\n\n' +
    '[abs](C:/x/y.png)\n\n[anc](#head)\n\n[bad](img/nope.png)\n\n[[wiki-page]]\n\n[alias]([[激活函数]])\n');
  fs.writeFileSync(path.join(root, 'sub', 'b.md'), '# B\n\n![p](../img/p.png)\n');
  const r = importDir(root, new Set(DFLT_SKIP));
  const c = r.summary.links, a = r.summary.attachments;
  const checks = [
    ['md_files==2', r.summary.markdown.files === 2],
    ['attachments==3', a.files === 3],
    ['by_kind image==3', a.by_kind.image === 3],
    ['referenced==2', a.referenced === 2],
    ['never_referenced==1', a.never_referenced === 1],
    ['links_total==8', c.links_total === 8],
    ['resolved==3 (含路径带括号 + sub 相对)', c.resolved === 3],
    ['缺失==1', c.missing === 1],
    ['external==1（去引号后识别）', c.external === 1],
    ['absolute==1', c.absolute === 1],
    ['anchor==1', c.anchor === 1],
    ['wiki_in_link==1（[别名]([[页面]]) 不计缺失）', c.wiki_in_link === 1],
    ['resolve_rate==75', c.resolve_rate_on_relative === 75],
    ['wikilinks_info==2', c.wikilinks_info === 2],
    ['unresolved_total==2 (missing+absolute)', r.unresolved.length === 2],
    ['孤儿附件识别', r.orphan.length === 1 && r.orphan[0] === 'img/orphan.png'],
  ];

  // 第二批：密钥扫描 / 零字节 / 去重 / 断点复用
  const root2 = path.join(root, 'extra');
  fs.mkdirSync(path.join(root2, 'assets'), { recursive: true });
  fs.writeFileSync(path.join(root2, 'assets', 'a.bin'), Buffer.alloc(10, 7));
  fs.writeFileSync(path.join(root2, 'assets', 'b.bin'), Buffer.alloc(10, 7));
  fs.writeFileSync(path.join(root2, 'assets', 'empty.bin'), Buffer.alloc(0));
  fs.writeFileSync(path.join(root2, 'leak.md'),
    '# 泄露样例\n\n' + 'key: ' + 'sk-' + 'ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n' +
    '![a](assets/a.bin)\n![b](assets/b.bin)\n![e](assets/empty.bin)\n');
  const r2 = importDir(root2, new Set(DFLT_SKIP));
  checks.push(['密钥扫描命中 1 个文件', r2.summary.security.files_with_hits === 1]);
  checks.push(['密钥规则为 openai_key', !!r2.summary.security.rules.openai_key]);
  checks.push(['不记录密钥值', !JSON.stringify(r2.security).includes('ABCDEFGHIJKLMNOPQRSTUVWXYZ012345')]);
  checks.push(['零字节附件计数', r2.summary.attachments.zero_byte === 1]);
  checks.push(['同内容附件去重成 1 组', r2.summary.attachments.duplicate_groups === 1]);
  const r2b = importDir(root2, new Set(DFLT_SKIP), { resume: r2 });
  checks.push(['断点复用：文件标记 unchanged', r2b.summary.markdown.unchanged === 1]);
  checks.push(['断点复用：统计口径不变', r2b.summary.links.links_total === r2.summary.links.links_total]);

  // 第三批：平台归一（NFC / win32 大小写）与 Logseq 块引用
  const root3 = path.join(root, 'platform');
  fs.mkdirSync(path.join(root3, 'pages'), { recursive: true });
  fs.mkdirSync(path.join(root3, 'img'), { recursive: true });
  fs.writeFileSync(path.join(root3, 'pages', 'Sample.md'),
    '# Sample\n\nid:: 65F1A2B3-0000-4000-8000-00000000AAAA\n');
  fs.writeFileSync(path.join(root3, 'img', 'photo.png'), Buffer.alloc(20, 5));
  fs.writeFileSync(path.join(root3, 'cafe\u0301.md'), '# NFD 文件名\n\n![图](IMG/PHOTO.PNG)\n');
  fs.writeFileSync(path.join(root3, 'links.md'),
    '# 链接\n\n[[sample]]\n\n[[Sample]]\n\n[[不存在]]\n\n' +
    '((65f1a2b3-0000-4000-8000-00000000aaaa))\n\n((65f1a2b3-0000-4000-8000-00000000bbbb))\n');
  const r3 = importDir(root3, new Set(DFLT_SKIP));
  const c3 = r3.summary.links, a3 = r3.summary.attachments;
  checks.push(['NFC 文件名归一到 manifest', r3.markdown.some(e => e.path === 'café.md')]);
  checks.push(['双链统计 3 条', c3.wiki_pages_total === 3]);
  checks.push(['双链目标存在性（win32 大小写不敏感）', c3.wiki_pages_resolved === (IS_WIN ? 2 : 1)]);
  checks.push(['块引用对账：命中 id:: 定义', c3.block_refs.total === 2 && c3.block_refs.resolved === 1 && c3.block_refs.id_defs === 1]);
  checks.push(['未命中的块引用进 unresolved 明细', r3.unresolved.some(u => u.cause === 'block_ref_missing')]);
  if (IS_WIN) checks.push(['win32 附件引用大小写不敏感', a3.referenced === 1 && a3.never_referenced === 0]);
  else checks.push(['非 win32 保留大小写差异（IMG/PHOTO.PNG 视为缺失）', a3.referenced === 0]);
  checks.push(['长路径计数只在 win32 上报', IS_WIN ? r3.summary.integrity.long_paths === 0 : r3.summary.integrity.long_paths === null]);
  const r3b = importDir(root3, new Set(DFLT_SKIP), { resume: r3 });
  checks.push(['断点复用时块引用对账不退化', r3b.summary.links.block_refs.resolved === 1 && r3b.summary.markdown.unchanged === 3]);

  const failed = checks.filter(x => !x[1]).map(x => x[0]);
  return {
    pass: failed.length === 0, checks: checks.length, failed,
    fixture_root: norm(root), summary: r.summary,
    extra: { summary: r2.summary, resumed_unchanged: r2b.summary.markdown.unchanged },
  };
}

// ---------------- main ----------------
function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] !== undefined ? process.argv[i + 1] : dflt;
}

if (require.main === module) {
  if (process.argv.includes('--selftest')) {
    const explicit = arg('selftest-root', '');
    const root = explicit || fs.mkdtempSync(path.join(os.tmpdir(), 'awam-import-selftest-'));
    const res = selftest(root);
    console.log(JSON.stringify(res, null, 2));
    if (!explicit) { try { fs.rmSync(root, { recursive: true, force: true }); } catch (e) { /* ignore */ } }
    process.exit(res.pass ? 0 : 1);
  }
  const ROOT = arg('root', '');
  if (!ROOT) { console.error('需要 --root <dir>'); process.exit(1); }
  const SKIP = parseSkip(arg('skip', ''));
  const MANIFEST = arg('manifest', '');
  const REPORT = arg('report', '');
  const MAX_LIST = Number(arg('max-list', '50'));
  const RESUME = arg('resume', '');
  const scanSecrets = !process.argv.includes('--no-scan-secrets');

  let resume = null;
  if (RESUME) {
    try { resume = JSON.parse(fs.readFileSync(RESUME, 'utf8')); }
    catch (e) { console.error('无法读取 --resume manifest: ' + (e.message || e)); process.exit(1); }
  }

  const r = importDir(ROOT, SKIP, { resume, scanSecrets });
  if (MANIFEST) {
    fs.mkdirSync(path.dirname(MANIFEST), { recursive: true });
    const tmp = MANIFEST + '.tmp-' + process.pid;
    fs.writeFileSync(tmp, JSON.stringify({
      version: 2, generated_at: new Date().toISOString(),
      summary: r.summary, markdown: r.markdown, attachments: r.attachments,
      unresolved: r.unresolved, security: r.security, duplicate_groups: r.duplicateGroups,
    }, null, 1));
    fs.renameSync(tmp, MANIFEST);
  }
  if (REPORT) {
    const s = r.summary, L = [];
    L.push('# 导入结果 · ' + s.root, '');
    L.push('- skip=' + JSON.stringify(s.skip) + ' · resume=' + (r.summary.resume.used ? 'yes' : 'no'));
    L.push('- Markdown 条目：**' + s.markdown.files + '**（' + s.markdown.bytes + ' B）· 未变化（断点复用）' + s.markdown.unchanged);
    L.push('- 附件：**' + s.attachments.files + '**（' + s.attachments.bytes + ' B）· 被引用 ' + s.attachments.referenced
      + ' · 从未被引用 ' + s.attachments.never_referenced + ' · 零字节 ' + s.attachments.zero_byte
      + ' · 同内容组 ' + s.attachments.duplicate_groups);
    L.push('- 链接：总 ' + s.links.links_total + ' · 解析成功 ' + s.links.resolved + ' · 缺失 ' + s.links.missing
      + ' · 外部 ' + s.links.external + ' · 绝对 ' + s.links.absolute + ' · 锚点 ' + s.links.anchor
      + ' · 越出根 ' + s.links.escapes_root + ' · 内嵌双链 ' + s.links.wiki_in_link);
    L.push('- **相对路径可解析率：' + s.links.resolve_rate_on_relative + '%**（分母 = 解析成功+缺失）');
    L.push('- 缺失链接的文件名回找：唯一命中 ' + s.links.fallback_unique + ' · 多义 ' + s.links.fallback_ambiguous + ' · 无候选 ' + s.links.fallback_none);
    L.push('- 密钥扫描：' + (s.security.scan ? ('命中 ' + s.security.files_with_hits + ' 个文件 / ' + s.security.hits_total + ' 处') : '已关闭'));
    L.push('', '## 失败/丢失清单（前 ' + MAX_LIST + ' 条 / 共 ' + r.unresolved.length + '）', '',
      '| Markdown | 目标 | 原因 | 形式 | 文件名回找候选 |', '|---|---|---|---|---|');
    for (const u of r.unresolved.slice(0, MAX_LIST)) {
      L.push('| ' + u.file + ' | ' + u.target + ' | ' + u.cause + ' | ' + u.form + ' | '
        + (u.basename_candidates === undefined ? '-' : u.basename_candidates) + ' |');
    }
    if (r.security.files.length) {
      L.push('', '## 密钥模式命中（只列文件与规则）', '', '| 文件 | 规则 | 次数 |', '|---|---|---|');
      for (const f of r.security.files.slice(0, MAX_LIST)) L.push('| ' + f.file + ' | ' + f.rules.join(', ') + ' | ' + f.count + ' |');
    }
    fs.writeFileSync(REPORT, L.join('\n'));
  }
  console.log(JSON.stringify(r.summary, null, 2));
}

module.exports = {
  DFLT_SKIP, parseSkip, collect, importSteps, importDir, extractLinks, scanSecrets, SECRET_PATTERNS,
};
