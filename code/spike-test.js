'use strict';
/**
 * T10 Spike · 自动化断言脚本（零依赖，直连 stdio 子进程）
 * 断言：无 token 必拒 / 伪造签名必拒 / 有效 token 可提交 / token 重放必拒 / token 过期必拒
 *       / 内容已变(hash 不符)必拒 / 提案过期必拒 / propose 不落盘
 *       / 真实导入 job 的 progress 通知与 manifest / 取消无 manifest
 * 输出：单行 JSON 结果（供报告引用）
 */
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const { issueToken } = require('./companion');

const SERVER = path.join(__dirname, 'mcp-server.js');
const BASE = path.join(os.tmpdir(), 'awam-spike-run');
const ROOT = path.join(BASE, 'state');
const CORPUS = path.join(BASE, 'corpus');
const BUSY = path.join(BASE, 'busy');
const TOKEN_TTL_MS = 500;

// 干净环境 + 预置共享密钥（模拟 companion 与 server 共享 secret）
fs.rmSync(BASE, { recursive: true, force: true });
fs.mkdirSync(ROOT, { recursive: true });
fs.mkdirSync(CORPUS, { recursive: true });
fs.mkdirSync(BUSY, { recursive: true });
const SECRET = crypto.randomBytes(32).toString('hex');
process.env.AWAM_SECRET = SECRET;
for (let i = 0; i < 8; i++) fs.writeFileSync(path.join(CORPUS, 'f' + i + '.md'), '# 文件 ' + i + '\n\n内容 ' + i + '\n');
for (let i = 0; i < 3000; i++) fs.writeFileSync(path.join(BUSY, 'b' + i + '.md'), '# 批量 ' + i + '\n\n内容 ' + i + '\n');

const child = spawn(process.execPath, [SERVER], {
  env: Object.assign({}, process.env, {
    SPIKE_ROOT: ROOT, AWAM_SECRET: SECRET, TOKEN_TTL_MS: String(TOKEN_TTL_MS), PROPOSAL_TTL_MS: '2000',
  }),
  stdio: ['pipe', 'pipe', 'pipe'],
});

let buf = '', idc = 0;
const pending = new Map();
const notifications = [];
child.stdout.on('data', function (d) {
  buf += d.toString();
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i); buf = buf.slice(i + 1);
    if (!line.trim()) continue;
    let m; try { m = JSON.parse(line); } catch (e) { continue; }
    if (m.id !== undefined && pending.has(m.id)) {
      const r = pending.get(m.id); pending.delete(m.id); r(m);
    } else if (m.method) { notifications.push(m); }
  }
});
child.stderr.on('data', function (d) { process.stderr.write('[server-stderr] ' + d); });

function send(method, params) {
  const id = ++idc;
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: id, method: method, params: params }) + '\n');
  return new Promise(function (res) { pending.set(id, res); });
}
async function call(name, args, meta) {
  const r = await send('tools/call', { name: name, arguments: args, _meta: meta });
  const c = r.result && r.result.content && r.result.content[0];
  let obj = null; try { obj = JSON.parse(c.text); } catch (e) {}
  return { obj: obj, isError: !!(r.result && r.result.isError), raw: r };
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
function check(name, cond, detail) { results.push({ name: name, pass: !!cond, detail: detail }); }

(async function main() {
  const init = await send('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'spike-test', version: '0' } });
  check('initialize 握手', init.result && init.result.serverInfo && init.result.protocolVersion === '2024-11-05', init.result && init.result.protocolVersion);

  const list = await send('tools/list', {});
  const names = (list.result.tools || []).map(t => t.name);
  check('tools/list 暴露 9 个工具', names.length === 9 && names.indexOf('commit_write') >= 0 && names.indexOf('undo_write') >= 0 && names.indexOf('get_audit') >= 0, names.join(','));

  // ---------- A: propose 不落盘 ----------
  const targetA = 'notes/a.md';
  const pA = (await call('propose_write', { target_path: targetA, content: 'hello A\n' })).obj;
  check('A1 propose 返回 proposal_id/diff/ttl', !!(pA && pA.proposal_id && pA.diff && pA.proposal_ttl_ms), pA && pA.proposal_id);
  check('A2 propose 未落盘（目标文件不存在）', !fs.existsSync(path.join(ROOT, targetA)), 'exists=' + fs.existsSync(path.join(ROOT, targetA)));

  // ---------- B: 无 token 必拒 ----------
  const bNoToken = (await call('commit_write', { proposal_id: pA.proposal_id })).obj;
  check('B1 无 token 必拒', bNoToken && bNoToken.error === 'rejected' && bNoToken.reason === 'missing_token', JSON.stringify(bNoToken));
  const bBadSig = (await call('commit_write', { proposal_id: pA.proposal_id, confirmation_token: 'ZmFrZQ.fake_signature' })).obj;
  check('B2 伪造签名必拒', bBadSig && bBadSig.reason === 'bad_signature', JSON.stringify(bBadSig));
  check('B3 拒绝后仍未落盘', !fs.existsSync(path.join(ROOT, targetA)), 'exists=' + fs.existsSync(path.join(ROOT, targetA)));

  // ---------- C: 有效 token 可提交 ----------
  const tokA = issueToken(pA.proposal_id, pA.baseline_hash, Date.now(), ROOT);
  const cCommit = (await call('commit_write', { proposal_id: pA.proposal_id, confirmation_token: tokA })).obj;
  check('C1 有效 token 提交成功', cCommit && cCommit.committed === true, JSON.stringify(cCommit));
  const aExists = fs.existsSync(path.join(ROOT, targetA));
  check('C2 内容已落盘且一致', aExists && fs.readFileSync(path.join(ROOT, targetA), 'utf8') === 'hello A\n', 'exists=' + aExists);

  // ---------- D: token 重放必拒 ----------
  const dReplay = (await call('commit_write', { proposal_id: pA.proposal_id, confirmation_token: tokA })).obj;
  check('D1 token 重放必拒', dReplay && (dReplay.reason === 'token_replayed' || dReplay.error === 'already_committed'), JSON.stringify(dReplay));

  // ---------- E: token 过期必拒 ----------
  const targetE = 'notes/e.md';
  const pE = (await call('propose_write', { target_path: targetE, content: 'hello E\n' })).obj;
  const tokExpired = issueToken(pE.proposal_id, pE.baseline_hash, Date.now() - 10000, ROOT);
  const eExpired = (await call('commit_write', { proposal_id: pE.proposal_id, confirmation_token: tokExpired })).obj;
  check('E1 token 过期必拒', eExpired && eExpired.reason === 'token_expired', JSON.stringify(eExpired));
  check('E2 过期拒绝后未落盘', !fs.existsSync(path.join(ROOT, targetE)), 'exists=' + fs.existsSync(path.join(ROOT, targetE)));

  // ---------- F: 内容已变（hash 不符）必拒 ----------
  const targetF = 'notes/f.md';
  fs.mkdirSync(path.join(ROOT, 'notes'), { recursive: true });
  fs.writeFileSync(path.join(ROOT, targetF), 'baseline F v1\n');
  const pF = (await call('propose_write', { target_path: targetF, content: 'baseline F v2 (proposed)\n' })).obj;
  const tokF = issueToken(pF.proposal_id, pF.baseline_hash, Date.now(), ROOT);
  fs.writeFileSync(path.join(ROOT, targetF), 'EXTERNAL EDIT\n');
  const fConflict = (await call('commit_write', { proposal_id: pF.proposal_id, confirmation_token: tokF })).obj;
  check('F1 内容已变必拒（conflict）', fConflict && fConflict.error === 'conflict' && fConflict.reason === 'baseline_changed', JSON.stringify(fConflict));
  check('F2 冲突时不覆盖外部改动', fs.readFileSync(path.join(ROOT, targetF), 'utf8') === 'EXTERNAL EDIT\n', 'content=' + JSON.stringify(fs.readFileSync(path.join(ROOT, targetF), 'utf8')));

  // ---------- G: 提案过期必拒（proposal_ttl）----------
  const pG = (await call('propose_write', { target_path: 'notes/g.md', content: 'g\n' })).obj;
  await sleep(2200);
  const tokG = issueToken(pG.proposal_id, pG.baseline_hash, Date.now(), ROOT);
  const gExpired = (await call('commit_write', { proposal_id: pG.proposal_id, confirmation_token: tokG })).obj;
  check('G1 提案过期必拒（不落盘）', gExpired && gExpired.error === 'proposal_expired', JSON.stringify(gExpired));
  check('G2 提案过期后未落盘', !fs.existsSync(path.join(ROOT, 'notes', 'g.md')), 'exists=' + fs.existsSync(path.join(ROOT, 'notes', 'g.md')));

  // ---------- H: 真实导入 job + notifications/progress ----------
  const progressToken = 'pt-' + Date.now();
  const hJob = (await call('start_import', { root: CORPUS }, { progressToken: progressToken })).obj;
  check('H1 start_import 返回 job_id', !!(hJob && hJob.job_id), JSON.stringify(hJob));
  const jobId = hJob.job_id;
  let hStatus = null;
  for (let i = 0; i < 100; i++) {
    await sleep(50);
    hStatus = (await call('get_job_status', { job_id: jobId })).obj;
    if (hStatus && (hStatus.status === 'done' || hStatus.status === 'cancelled' || hStatus.status === 'failed')) break;
  }
  const progNotifs = notifications.filter(m => m.method === 'notifications/progress' && m.params && m.params.progressToken === progressToken);
  const progresses = progNotifs.map(p => p.params.progress);
  const monotonic = progresses.every((n, i) => i === 0 || n >= progresses[i - 1]);
  check('H2 收到 notifications/progress', progNotifs.length > 0, 'count=' + progNotifs.length);
  check('H3 progress 单调且到达 8', monotonic && progresses[progresses.length - 1] === 8, progresses.join(','));
  check('H4 job 终态 done 且写 manifest', hStatus && hStatus.status === 'done' && hStatus.manifest_written === true, JSON.stringify(hStatus));
  const manifestOk = hStatus && hStatus.manifest_path && fs.existsSync(hStatus.manifest_path)
    && JSON.parse(fs.readFileSync(hStatus.manifest_path, 'utf8')).summary.markdown.files === 8;
  check('H5 manifest 可读且计数正确', manifestOk);

  // ---------- I: 取消 job → 无 manifest ----------
  const iJob = (await call('start_import', { root: BUSY })).obj;
  await sleep(10);
  const iCancel = (await call('cancel_job', { job_id: iJob.job_id })).obj;
  let iStatus = null;
  for (let i = 0; i < 300; i++) {
    await sleep(50);
    iStatus = (await call('get_job_status', { job_id: iJob.job_id })).obj;
    if (iStatus && iStatus.status !== 'running') break;
  }
  check('I1 取消后状态为 cancelled', iStatus && iStatus.status === 'cancelled', JSON.stringify({ status: iStatus && iStatus.status, cancel: iCancel }));
  check('I2 取消后无 manifest', iStatus && iStatus.manifest_written === false, JSON.stringify(iStatus));

  // ---------- 汇总 ----------
  const passed = results.filter(r => r.pass).length;
  console.log(JSON.stringify({
    total: results.length, passed: passed, failed: results.length - passed,
    notificationsSeen: [...new Set(notifications.map(m => m.method))],
    results: results,
  }, null, 2));

  child.stdin.end();
  child.kill();
  fs.rmSync(BASE, { recursive: true, force: true });
  process.exit(passed === results.length ? 0 : 1);
})().catch(function (e) {
  console.error('FATAL', e && e.stack);
  child.kill();
  try { fs.rmSync(BASE, { recursive: true, force: true }); } catch (x) { /* ignore */ }
  process.exit(1);
});
