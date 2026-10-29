'use strict';
/**
 * 基础设施自检（零依赖，全在系统 temp 下，跑完删除）
 *
 *   node code/infra-selftest.js
 *
 * 四块：
 *   A. index-root：settings.yaml 回退、环境变量优先级、真源拒绝、~ 展开
 *   B. 增量索引：缓存命中/未变文件复用/改动重建/删除剔除/参数变更全量重建
 *   C. 状态库：原子写、单写者锁、快照+日志回放、崩溃恢复（running → interrupted）
 *   D. 审计日志：追加顺序、时间戳、limit/since 边界、压实不丢审计
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { atomicWrite, writeJsonAtomic, readJson, acquireLock } = require('./fs-util');
const { openStore } = require('./store');
const bm = require('./bm25-index');
const ir = require('./index-root');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'awam-infra-selftest-'));
const checks = [];
const add = (name, pass, detail) => checks.push({ name, pass: !!pass, detail: detail === undefined ? null : detail });

function write(p, s) { fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, s); }
const norm = p => String(p).split('\\').join('/');

const ENV_KEYS = ['KM_SETTINGS', 'KM_DATA_ROOT', 'KM_SOURCE_ID', 'DESENS_DATA_ROOT', 'KM_RAW_ROOTS'];
function withEnv(patch, fn) {
  const saved = {};
  for (const k of ENV_KEYS) saved[k] = process.env[k];
  for (const k of ENV_KEYS) delete process.env[k];
  for (const k of Object.keys(patch)) {
    if (patch[k] === undefined) delete process.env[k];
    else process.env[k] = patch[k];
  }
  try { return fn(); } finally {
    for (const k of ENV_KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  }
}

// ---------- A. index-root ----------
(function sectionA() {
  const dir = path.join(TMP, 'cfg');
  const settings = path.join(dir, 'settings.yaml');
  write(settings, [
    '# 个人配置',
    'data_root: "' + norm(path.join(dir, 'data')) + '"',
    "source_id: 'notes'",
    'unrelated: keep',
    '',
  ].join('\n'));

  const root = withEnv({ KM_SETTINGS: settings }, () => ir.resolveDefaultRoot());
  add('A1 settings.yaml 回退（引号+注释）', norm(root) === norm(path.join(dir, 'data', 'notes')), root);

  const valid = withEnv({ KM_SETTINGS: settings }, () => ir.validateIndexRoot(''));
  add('A2 未显式传根时校验通过', valid.ok === true && norm(valid.root) === norm(path.join(dir, 'data', 'notes')), valid);

  const envRoot = withEnv({ KM_SETTINGS: settings, KM_DATA_ROOT: path.join(dir, 'env'), KM_SOURCE_ID: 'env-src' },
    () => ir.resolveDefaultRoot());
  add('A3 环境变量优先于 settings', norm(envRoot) === norm(path.join(dir, 'env', 'env-src')), envRoot);

  const partialEnv = withEnv({ KM_SETTINGS: settings, KM_DATA_ROOT: path.join(dir, 'env2') }, () => ir.resolveDefaultRoot());
  add('A4 只给 KM_DATA_ROOT 时 source_id 取 settings', norm(partialEnv) === norm(path.join(dir, 'env2', 'notes')), partialEnv);

  const noCfg = withEnv({ KM_SETTINGS: path.join(dir, 'missing.yaml') }, () => ir.resolveDefaultRoot());
  add('A5 无任何配置返回 null', noCfg === null);
  const noCfgValid = withEnv({ KM_SETTINGS: path.join(dir, 'missing.yaml') }, () => ir.validateIndexRoot(''));
  add('A6 无配置时报 no_root', noCfgValid.ok === false && noCfgValid.reason === 'no_root', noCfgValid.reason);

  const explicit = withEnv({ KM_SETTINGS: settings }, () => ir.validateIndexRoot(path.join(dir, 'explicit')));
  add('A7 显式根优先于 settings', explicit.ok === true && norm(explicit.root) === norm(path.join(dir, 'explicit')), explicit.root);

  const tildeCfg = path.join(dir, 'tilde.yaml');
  write(tildeCfg, 'data_root: ~/km-selftest-home\n');
  const tilde = withEnv({ KM_SETTINGS: tildeCfg }, () => ir.resolveDefaultRoot());
  add('A8 ~ 展开为用户目录', norm(tilde) === norm(path.join(os.homedir(), 'km-selftest-home', ir.DEFAULT_SOURCE_ID)), tilde);

  const raw = path.join(dir, 'raw');
  const rawV = withEnv({ KM_SETTINGS: settings, KM_RAW_ROOTS: raw }, () => ir.validateIndexRoot(path.join(raw, 'sub')));
  add('A9 真源默认拒绝', rawV.ok === false && rawV.reason === 'raw_source', rawV.reason);
  const rawAllow = withEnv({ KM_SETTINGS: settings, KM_RAW_ROOTS: raw }, () => ir.validateIndexRoot(path.join(raw, 'sub'), { allowRaw: true }));
  add('A10 allowRaw 时放行', rawAllow.ok === true);
})();

// ---------- B. 增量索引 ----------
(function sectionB() {
  const root = path.join(TMP, 'corpus');
  write(path.join(root, 'a.md'), '# 向量检索\n\n倒排索引配合向量检索。\n');
  write(path.join(root, 'b.md'), '# 搬家清单\n\n先列依赖再打包。\n');
  write(path.join(root, 'sub', 'c.md'), '# 伽马射线\n\n伽马射线暴。\n');
  const cachePath = path.join(TMP, 'cache', 'index-cache.json');
  const opts = { root, chunkLines: 20, titleWeight: 4, cachePath };
  const docKey = r => r.docs.map(d => d.path + ':' + d.lineStart + ':' + d.text).sort().join('|');

  const base = bm.buildCorpus({ root, chunkLines: 20, titleWeight: 4 });
  const r1 = bm.buildCorpusCached(opts);
  add('B1 首次构建：全部 miss 并写缓存', r1.cache.misses === 3 && r1.cache.hits === 0 && r1.cache.saved === true, r1.cache);
  add('B2 首次构建结果与无缓存版一致', docKey(r1) === docKey(base), { cached: r1.docs.length, plain: base.docs.length });

  const r2 = bm.buildCorpusCached(opts);
  add('B3 二次构建：全部 hit 且不重写缓存', r2.cache.hits === 3 && r2.cache.misses === 0 && r2.cache.saved === false, r2.cache);
  add('B4 二次构建文档不变', docKey(r2) === docKey(r1));

  write(path.join(root, 'a.md'), '# 向量检索\n\n改过的内容：混合召回。\n');
  const later = new Date(Date.now() + 2000);
  fs.utimesSync(path.join(root, 'a.md'), later, later);
  const r3 = bm.buildCorpusCached(opts);
  add('B5 单文件改动：1 miss + 2 hit', r3.cache.misses === 1 && r3.cache.hits === 2, r3.cache);
  add('B6 改动后的新内容进入索引', r3.docs.some(d => d.path === 'a.md' && d.text.includes('混合召回')));

  fs.unlinkSync(path.join(root, 'b.md'));
  const r4 = bm.buildCorpusCached(opts);
  add('B7 删除文件被剔除', r4.cache.removed === 1 && !r4.docs.some(d => d.path === 'b.md') && r4.scanned.length === 2, r4.cache);

  const r5 = bm.buildCorpusCached({ root, chunkLines: 10, titleWeight: 4, cachePath });
  add('B8 切块参数变化 → 缓存不兼容全量重建', r5.cache.compatible === false && r5.cache.hits === 0 && r5.cache.misses === 2, r5.cache);

  const r6 = bm.buildCorpusCached(Object.assign({}, opts, { force: true }));
  add('B9 force 跳过缓存', r6.cache.hits === 0 && r6.cache.misses === 2, r6.cache);
})();

// ---------- C. 状态库 ----------
(function sectionC() {
  const dir = path.join(TMP, 'state');

  const target = path.join(dir, 'nested', 'x.json');
  writeJsonAtomic(target, { a: 1 });
  const leftovers = fs.readdirSync(path.dirname(target)).filter(f => f.includes('.tmp-'));
  add('C1 原子写落盘且无临时文件残留', readJson(target, null).a === 1 && leftovers.length === 0, leftovers);

  const l1 = acquireLock(dir, 'locktest');
  const l2 = acquireLock(dir, 'locktest');
  add('C2 同目录第二个锁被拒', l1.ok === true && l2.ok === false && l2.error === 'locked', l2);
  l1.release();
  const l3 = acquireLock(dir, 'locktest');
  add('C3 释放后可再次获取', l3.ok === true);
  l3.release();
  const dead = path.join(dir, 'stale.lock');
  fs.writeFileSync(dead, JSON.stringify({ pid: 999999, at: Date.now() }));
  const l4 = acquireLock(dir, 'stale', { staleMs: 60 * 60 * 1000 });
  add('C4 持有者已退出的锁可被接管', l4.ok === true, l4);
  if (l4.ok) l4.release();

  const store = openStore(dir);
  const job = { job_id: 'j1', status: 'running', phase: 'analyzing', done: 0, total: 3 };
  store.recordJob(job);
  store.recordToken('token-1');
  store.recordUndo({ proposal_id: 'p1', at: Date.now(), path: 'a.md' });

  const reopened = openStore(dir);
  add('C5 日志回放：token 与撤销记录跨进程可见',
    reopened.state.usedTokens.has('token-1') && reopened.state.undo.length === 1, {
    tokens: reopened.state.usedTokens.size, undo: reopened.state.undo.length,
  });
  add('C6 崩溃恢复：running job 标记 interrupted',
    reopened.interrupted.length === 1 && reopened.state.jobs.j1.status === 'interrupted', reopened.interrupted);

  reopened.compact();
  reopened.recordToken('token-2');
  const after = openStore(dir);
  add('C7 compact 后新增事件仍可回放',
    after.state.usedTokens.has('token-1') && after.state.usedTokens.has('token-2'), [...after.state.usedTokens]);

  after.recordUndoUsed('p1');
  const after2 = openStore(dir);
  add('C8 撤销已用标记持久化', after2.state.undone.has('p1'));

  fs.appendFileSync(path.join(dir, 'state.jsonl'), '{broken json\n');
  const after3 = openStore(dir);
  add('C9 损坏日志行被跳过', after3.state.usedTokens.has('token-2'));

  const doneJob = { job_id: 'j2', status: 'done', phase: 'done', done: 9, total: 9 };
  after3.recordJob(doneJob);
  const after4 = openStore(dir);
  add('C10 已结束 job 不被误标 interrupted',
    after4.state.jobs.j2.status === 'done' && after4.interrupted.length === 0, after4.interrupted);

  const bytes = atomicWrite(path.join(dir, 'raw.bin'), Buffer.from('hi'));
  add('C11 原子写返回字节数', bytes === 2);
})();

// ---------- D. 审计日志 ----------
(function sectionD() {
  const dir = path.join(TMP, 'audit');
  const store = openStore(dir);

  store.audit({ event: 'propose', proposal_id: 'p1', client: 'selftest' });
  store.audit({ event: 'commit', proposal_id: 'p1', committed_hash: 'abc' });
  store.audit({ event: 'commit_rejected', proposal_id: 'p2', reason: 'proposal_expired' });

  const r1 = store.readAudit({});
  add('D1 审计按写入顺序返回', r1.total === 3 && r1.entries.map(e => e.event).join(',') === 'propose,commit,commit_rejected',
    r1.entries.map(e => e.event));
  add('D2 每条审计自带时间戳与原始字段', r1.entries.every(e => typeof e.at === 'number' && e.at > 0) &&
    r1.entries[1].committed_hash === 'abc' && r1.entries[0].client === 'selftest');

  const last1 = store.readAudit({ limit: 1 });
  add('D3 limit 取最后 N 条', last1.entries.length === 1 && last1.entries[0].event === 'commit_rejected', {
    entries: last1.entries.length, total: last1.total,
  });
  add('D4 limit 越界被夹到 1..2000', store.readAudit({ limit: 0 }).entries.length === 1 &&
    store.readAudit({ limit: -5 }).entries.length === 1 && store.readAudit({ limit: 99999 }).entries.length === 3);

  const future = store.readAudit({ since: Date.now() + 60 * 1000 });
  add('D5 since 过滤掉更早的记录', future.total === 0 && future.entries.length === 0, future.total);

  // 状态日志会被压实/截断，审计不能跟着丢
  store.compact();
  const reopened = openStore(dir);
  store.audit({ event: 'undo', proposal_id: 'p1' });
  const r2 = reopened.readAudit({});
  add('D6 compact 清空状态日志后审计仍完整', r2.total === 4 &&
    r2.entries.map(e => e.event).join(',') === 'propose,commit,commit_rejected,undo', r2.total);
  const journalPath = path.join(dir, 'state.jsonl');
  add('D7 审计文件独立于快照与状态日志', fs.existsSync(path.join(dir, 'audit.jsonl')) &&
    fs.existsSync(path.join(dir, 'state.json')) &&
    (!fs.existsSync(journalPath) || fs.readFileSync(journalPath, 'utf8').trim() === ''));

  fs.appendFileSync(path.join(dir, 'audit.jsonl'), '{broken\n');
  const r3 = reopened.readAudit({});
  add('D8 审计里的损坏行被跳过而不是整体失败', r3.total === 4, r3.total);
})();

const failed = checks.filter(c => !c.pass);
console.log(JSON.stringify({ selftest: true, checks: checks.length, pass: failed.length === 0, failed: failed.map(f => f.name), detail: failed, tmp: TMP }, null, 2));
try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* ignore */ }
process.exit(failed.length === 0 ? 0 : 1);
