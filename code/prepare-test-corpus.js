'use strict';
/**
 * T21 · 测试夹具准备：从语料根拷出「一个子目录 + 其引用的附件」（只读源）
 *
 *   node prepare-test-corpus.js --src <dir> --select "pages/mubu" ^
 *        --dest-full <dir> --dest-partial <dir> --asset-limit 5
 *   node prepare-test-corpus.js --selftest        # 合成夹具（不读私人语料）
 *
 * 行为：
 *   - 源目录**只读**；只在 --dest-* 指定目录内创建/覆盖文件
 *   - 拷贝被选中的 md 文件，以及它们**相对链接指向且真实存在**的附件
 *     （附件按"链接解析后的相对位置"落位，保证相对路径在新根下仍成立）
 *   - 逐文件比对源/目标 SHA256，作为"逐字拷贝"证据
 *   - dest-partial 只放前 --asset-limit 个附件，用于验证导入器的"缺失检测"路径
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { resolveIndexRoot } = require('./index-root');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] !== undefined ? process.argv[i + 1] : dflt;
}

const LINK_RE = /!?\[[^\]]*\]\(([^)]+)\)/g;
const IMG_RE = /<img[^>]*src=["']([^"']+)["']/gi;

function sha256(p) { return crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex'); }
function isExternal(t) { return /^(https?:|mailto:|data:|tel:)/i.test(t); }
function isAbsolute(t) { return /^[a-zA-Z]:[\\/]/.test(t) || /^\\\\/.test(t) || t.startsWith('/'); }
function norm(p) { return p.split('\\').join('/'); }

function listMd(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...listMd(p));
    else if (/\.(md|markdown)$/i.test(e.name)) out.push(p);
  }
  return out;
}

function collectAssets(src, mdFiles) {
  const assets = new Map();
  for (const f of mdFiles) {
    const dir = path.dirname(f);
    const txt = fs.readFileSync(f, 'utf8');
    const targets = [];
    let m;
    while ((m = LINK_RE.exec(txt)) !== null) targets.push(m[1].trim());
    while ((m = IMG_RE.exec(txt)) !== null) targets.push(m[1].trim());
    for (const t of targets) {
      if (isExternal(t) || isAbsolute(t) || t.startsWith('#')) continue;
      let dec = t.split('#')[0].split('?')[0];
      try { dec = decodeURIComponent(dec); } catch (e) { /* keep */ }
      const abs = path.resolve(dir, dec);
      if (!fs.existsSync(abs) || !fs.statSync(abs).isFile()) continue;
      if (/\.(md|markdown)$/i.test(abs)) continue;
      assets.set(path.relative(src, abs), abs);
    }
  }
  return assets;
}

function copyInto(destRoot, files, src) {
  let copied = 0, bytes = 0, mismatch = 0;
  const put = (abs) => {
    const rel = path.relative(src, abs);
    const out = path.join(destRoot, rel);
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.copyFileSync(abs, out);
    copied++; bytes += fs.statSync(out).size;
    if (sha256(abs) !== sha256(out)) mismatch++;
  };
  for (const f of files) put(f);
  return { dest: norm(destRoot), files_copied: copied, bytes, sha_mismatch: mismatch };
}

/**
 * @param {{src:string, select:string, destFull?:string, destPartial?:string, assetLimit?:number}} opts
 */
function prepare(opts) {
  const src = opts.src;
  const selAbs = path.resolve(src, opts.select);
  const mdFiles = listMd(selAbs);
  const assets = collectAssets(src, mdFiles);
  const allAssets = [...assets.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const assetLimit = opts.assetLimit == null ? 5 : opts.assetLimit;

  const out = { src: norm(src), select: opts.select, md_files: mdFiles.length };
  if (opts.destFull) out.full = copyInto(opts.destFull, [...mdFiles, ...allAssets.map(([, abs]) => abs)], src);
  if (opts.destPartial) out.partial = copyInto(opts.destPartial, [...mdFiles, ...allAssets.slice(0, assetLimit).map(([, abs]) => abs)], src);
  out.assets_referenced_existing = allAssets.length;
  out.assets_total_bytes = allAssets.reduce((s, [, a]) => s + fs.statSync(a).size, 0);
  out.asset_note = 'partial 目录只拷前 ' + assetLimit + ' 个附件，其余参考文献仍留在 md 中 → 供导入器验证缺失检测';
  return out;
}

function selftest() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'awam-prepare-'));
  const w = (rel, data) => { const p = path.join(tmp, rel); fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, data); };
  w('pages/mubu/a.md', '# A\n\n![x](../../assets/img/x.png)\n![y](../../assets/img/y.png)\n[ext](https://example.com)\n');
  w('pages/mubu/deep/b.md', '# B\n\n![x](../../../assets/img/x.png)\n');
  w('assets/img/x.png', Buffer.alloc(10, 1));
  w('assets/img/y.png', Buffer.alloc(20, 2));
  w('pages/other/c.md', '# C\n\n不要拷贝\n');

  const full = path.join(tmp, 'out-full');
  const partial = path.join(tmp, 'out-partial');
  const r = prepare({ src: tmp, select: 'pages/mubu', destFull: full, destPartial: partial, assetLimit: 1 });

  const exists = p => fs.existsSync(path.join(tmp, p));
  const checks = [
    ['只选中子目录下的 md', r.md_files === 2, r.md_files],
    ['识别 2 个被引用附件', r.assets_referenced_existing === 2, r.assets_referenced_existing],
    ['full 拷 4 个文件（2 md + 2 附件）', r.full.files_copied === 4, r.full.files_copied],
    ['逐字拷贝无 SHA 差异', r.full.sha_mismatch === 0],
    ['附件按相对位置落位', exists('out-full/assets/img/x.png') && exists('out-full/assets/img/y.png')],
    ['未选中的 md 不拷贝', !exists('out-full/pages/other/c.md')],
    ['partial 只放 1 个附件', r.partial.files_copied === 3 && exists('out-partial/assets/img/x.png')],
    ['partial 缺少第二个附件', !exists('out-partial/assets/img/y.png')],
    ['partial 仍拷贝全部 md', exists('out-partial/pages/mubu/a.md') && exists('out-partial/pages/mubu/deep/b.md')],
  ];
  const failed = checks.filter(c => !c[1]).map(c => c[0]);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  return { selftest: true, checks: checks.length, pass: failed.length === 0, failed };
}

if (require.main === module) {
  if (process.argv.includes('--selftest')) {
    const res = selftest();
    console.log(JSON.stringify(res, null, 2));
    process.exit(res.pass ? 0 : 1);
  }
  const SRC = resolveIndexRoot(arg('src', ''), { label: 'SRC' });
  const res = prepare({
    src: SRC,
    select: arg('select', 'pages/mubu'),
    destFull: arg('dest-full', ''),
    destPartial: arg('dest-partial', ''),
    assetLimit: Number(arg('asset-limit', '5')),
  });
  console.log(JSON.stringify(res, null, 2));
}

module.exports = { prepare, listMd, collectAssets };
