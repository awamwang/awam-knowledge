'use strict';
/**
 * T18 · 候选 query 集的 ground-truth 存在性核验（只读）
 * 目的：确保每条候选 query 的"预期命中来源"确实存在于已连通语料中（否则评测无意义）
 *
 *   node query-set-check.js                 # 用 index-root 解析语料根
 *   node query-set-check.js --root <dir>    # 显式根
 *   node query-set-check.js --selftest      # 合成夹具（不读私人语料）
 *
 * 默认读同目录 queries-t18.json（仓库内为脱敏示例）。
 * 真实集请用 --queries <path>，例如 ../awam-knowledge.private/queries-t18.json。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { resolveIndexRoot } = require('./index-root');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] !== undefined ? process.argv[i + 1] : dflt;
}

function loadCases(file) {
  const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (!Array.isArray(raw)) throw new Error('query 集应为数组: ' + file);
  return raw.map(c => {
    if (!c || !c.id || !c.query || !Array.isArray(c.expect)) throw new Error('query 条目缺 id/query/expect: ' + JSON.stringify(c));
    return { id: c.id, type: c.type || '未分类', query: c.query, expect: c.expect.map(s => String(s).split('\\').join('/')) };
  });
}

function check(root, cases) {
  const rows = cases.map(c => {
    const checks = c.expect.map(rel => ({ path: rel, exists: fs.existsSync(path.join(root, rel)) }));
    return { id: c.id, type: c.type, query: c.query, expect: checks, all_present: checks.every(x => x.exists) };
  });
  const byType = {};
  for (const r of rows) byType[r.type] = (byType[r.type] || 0) + 1;
  const missing = rows.filter(r => !r.all_present);
  return {
    corpus_root: root,
    total_queries: rows.length,
    types: byType,
    all_ground_truth_present: missing.length === 0,
    missing_targets: missing.map(m => ({ id: m.id, query: m.query, missing: m.expect.filter(e => !e.exists).map(e => e.path) })),
    rows,
  };
}

function selftest() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'awam-qset-'));
  fs.mkdirSync(path.join(tmp, 'pages'), { recursive: true });
  fs.writeFileSync(path.join(tmp, 'pages', 'a.md'), '# A\n');
  const cases = [
    { id: 'S1', type: '精确术语', query: '甲', expect: ['pages/a.md'] },
    { id: 'S2', type: '同义换述', query: '乙', expect: ['pages/a.md', 'pages/c.md'] },
  ];
  const r = check(tmp, cases);
  const file = path.join(__dirname, 'queries-t18.json');
  const real = loadCases(file);
  const checks = [
    ['存在时 all_present=true', r.rows[0].all_present === true],
    ['缺失时 all_present=false', r.rows[1].all_present === false],
    ['缺失去重列出', r.missing_targets.length === 1 && r.missing_targets[0].missing[0] === 'pages/c.md'],
    ['类型计数', r.types['精确术语'] === 1 && r.types['同义换述'] === 1],
    ['query 集可加载且非空', real.length > 0 && real.every(c => c.id && c.query && c.expect.length > 0)],
    ['query 集路径已归一为 /', real.every(c => c.expect.every(e => e.indexOf('\\') === -1))],
  ];
  const failed = checks.filter(c => !c[1]).map(c => c[0]);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  return { selftest: true, checks: checks.length, pass: failed.length === 0, failed, queries_file: path.basename(file), queries: real.length };
}

if (require.main === module) {
  if (process.argv.includes('--selftest')) {
    const res = selftest();
    console.log(JSON.stringify(res, null, 2));
    process.exit(res.pass ? 0 : 1);
  }
  const casesFile = arg('queries', path.join(__dirname, 'queries-t18.json'));
  const ROOT = resolveIndexRoot(arg('root', ''));
  const out = check(ROOT, loadCases(casesFile));
  console.log(JSON.stringify(out, null, 2));
  process.exit(out.all_ground_truth_present ? 0 : 1);
}

module.exports = { loadCases, check };
