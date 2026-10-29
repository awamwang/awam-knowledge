'use strict';
/**
 * T21 · 全语料链接形态普查（只读，用于选夹具与报告统计）
 *   node scan-links.js --root <dir> [--skip "bak,.recycle"]
 *   node scan-links.js --selftest        # 合成夹具（不读私人语料）
 *
 * 链接目标先去包裹引号/尖括号（真实语料存在 `[x]("https://...")` 形态）；
 * 按文件所在目录统计 missing / escapes / 附件字节，并列出全部目录。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { resolveIndexRoot } = require('./index-root');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] !== undefined ? process.argv[i + 1] : dflt;
}

const LINK_RE = /!?\[[^\]]*\]\(([^)]+)\)/g;
const IMG_RE = /<img[^>]*src=["']([^"']+)["']/gi;
const REFDEF_RE = /^\s*\[[^\]]+\]:\s*(\S+)/gm;
const WIKILINK_RE = /(!)?\[\[([^\]|]+)(\|[^\]]*)?\]\]/g;

function clean(t) {
  let s = String(t).trim();
  if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) s = s.slice(1, -1).trim();
  if (s.startsWith('<') && s.endsWith('>')) s = s.slice(1, -1).trim();
  return s;
}
function isExternal(t) { return /^(https?:|mailto:|data:|tel:|ftp:)/i.test(t); }
function isAbsolute(t) { return /^[a-zA-Z]:[\\/]/.test(t) || /^\\\\/.test(t) || t.startsWith('/'); }
function norm(p) { return p.split('\\').join('/'); }
function dirOf(rel) { return norm(path.dirname(norm(rel))); }

function parseSkip(value) {
  return new Set(String(value || '').split(',').map(s => s.trim()).filter(Boolean));
}

function scan(root, skip) {
  const counters = {
    links_total: 0, md_link: 0, html_img: 0, refdef: 0, external: 0, absolute: 0, anchor: 0,
    resolved_md: 0, resolved_attach: 0, resolved_dir: 0, missing: 0, escapes_root: 0,
  };
  let wikilinks = 0, mdCount = 0, mdBytes = 0;
  const perDir = new Map(), missingList = [], absList = [];
  const refAsset = new Map();

  (function walk(dir) {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) { if (!skip.has(e.name)) walk(p); continue; }
      if (!e.isFile() || !/\.(md|markdown)$/i.test(e.name)) continue;
      const rel = norm(path.relative(root, p)); const key = dirOf(rel);
      if (!perDir.has(key)) perDir.set(key, { dir: key, md: 0, links: 0, ref_assets: 0, ref_bytes: 0, missing: 0, external: 0, escapes: 0, bytes: 0 });
      const d = perDir.get(key); d.md++;
      mdCount++;
      const txt = fs.readFileSync(p, 'utf8'); const b = Buffer.byteLength(txt); mdBytes += b; d.bytes += b;
      const dirAbs = path.dirname(p);
      const targets = [];
      let m;
      while ((m = LINK_RE.exec(txt)) !== null) { targets.push(clean(m[1])); counters.md_link++; }
      while ((m = IMG_RE.exec(txt)) !== null) { targets.push(clean(m[1])); counters.html_img++; }
      while ((m = REFDEF_RE.exec(txt)) !== null) { targets.push(clean(m[1])); counters.refdef++; }
      while ((m = WIKILINK_RE.exec(txt)) !== null) wikilinks++;

      for (const t of targets) {
        counters.links_total++; d.links++;
        if (isExternal(t)) { counters.external++; d.external++; continue; }
        if (t.startsWith('#')) { counters.anchor++; continue; }
        if (isAbsolute(t)) { counters.absolute++; absList.push(rel + ' -> ' + t); continue; }
        let dec = t.split('#')[0].split('?')[0];
        try { dec = decodeURIComponent(dec); } catch (e) { /* keep */ }
        const abs = path.resolve(dirAbs, dec);
        const inside = norm(path.relative(root, abs));
        if (inside.startsWith('..')) {
          counters.escapes_root++; d.escapes++;
          missingList.push({ file: rel, target: t, cause: 'escapes_root' });
          continue;
        }
        if (!fs.existsSync(abs)) {
          counters.missing++; d.missing++;
          missingList.push({ file: rel, target: t, cause: 'missing' });
          continue;
        }
        const st = fs.statSync(abs);
        if (st.isDirectory()) { counters.resolved_dir++; continue; }
        if (/\.(md|markdown)$/i.test(abs)) { counters.resolved_md++; continue; }
        counters.resolved_attach++; refAsset.set(inside, st.size); d.ref_assets++;
        const assetDir = dirOf(inside);
        if (perDir.has(assetDir)) perDir.get(assetDir).ref_bytes += st.size;
      }
    }
  })(root);

  return {
    root: norm(root), skip: [...skip], md_files: mdCount, md_bytes: mdBytes, wikilinks,
    counters,
    ref_assets_distinct: refAsset.size,
    ref_assets_bytes: [...refAsset.values()].reduce((a, b) => a + b, 0),
    dirs: [...perDir.values()].sort((a, b) => b.md - a.md),
    missing_total: missingList.length,
    missing_sample: missingList.slice(0, 12),
    absolute_total: absList.length,
    absolute_sample: absList.slice(0, 10),
  };
}

function selftest() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'awam-scanlinks-'));
  const w = (rel, s) => { const p = path.join(tmp, rel); fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, s); };
  w('img/x.png', Buffer.alloc(10, 1));
  w('img/y.png', Buffer.alloc(20, 2));
  w('doc.md', '# doc\n');
  fs.mkdirSync(path.join(tmp, 'sub'), { recursive: true });
  w('a.md', [
    '# A',
    '',
    '[doc](doc.md)',
    '![x](img/x.png)',
    '<img src="img/y.png">',
    '[r]: ./doc.md',
    '[ext]("https://example.com/x")',
    '[abs](C:/x/y.png)',
    '[anc](#head)',
    '[gone](img/nope.png)',
    '[out](../outside.md)',
    '[dir](sub/)',
    '[[wiki-page]]',
    '',
  ].join('\n'));
  w('skipme/hidden.md', '[gone](./nowhere.md)\n');

  const r = scan(tmp, parseSkip('skipme'));
  const c = r.counters;
  const checks = [
    ['只扫 2 个 md（跳过 skipme）', r.md_files === 2, r.md_files],
    ['链接总数=10', c.links_total === 10, c.links_total],
    ['md 行内链接=8（含内嵌图片）', c.md_link === 8, c.md_link],
    ['html img=1', c.html_img === 1],
    ['refdef=1', c.refdef === 1],
    ['external=1（去引号后识别）', c.external === 1, c.external],
    ['absolute=1', c.absolute === 1],
    ['anchor=1', c.anchor === 1],
    ['resolved_md=2', c.resolved_md === 2, c.resolved_md],
    ['resolved_attach=2', c.resolved_attach === 2],
    ['resolved_dir=1', c.resolved_dir === 1],
    ['missing=1', c.missing === 1],
    ['escapes_root=1', c.escapes_root === 1],
    ['wikilink=1', r.wikilinks === 1],
    ['附件去重后 2 个 / 30 字节', r.ref_assets_distinct === 2 && r.ref_assets_bytes === 30],
    ['缺失与越界分开归类',
      r.missing_sample.some(x => x.cause === 'missing') && r.missing_sample.some(x => x.cause === 'escapes_root')],
  ];
  const failed = checks.filter(x => !x[1]).map(x => x[0]);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  return { selftest: true, checks: checks.length, pass: failed.length === 0, failed };
}

if (require.main === module) {
  if (process.argv.includes('--selftest')) {
    const res = selftest();
    console.log(JSON.stringify(res, null, 2));
    process.exit(res.pass ? 0 : 1);
  }
  const ROOT = resolveIndexRoot(arg('root', ''));
  console.log(JSON.stringify(scan(ROOT, parseSkip(arg('skip', ''))), null, 1));
}

module.exports = { scan, parseSkip };
