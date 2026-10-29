'use strict';
/**
 * MCP 自检（零依赖）：临时语料 + 临时状态目录，直连 stdio 断言真实行为。
 *
 *   node code/mcp-selftest.js
 *
 * 覆盖：search（命中/行号/片段/topk/排除目录/未配置根）、写入闸（越界拒绝、坏签名、提交、重放、
 * 撤销）、真实导入 job（进度通知、manifest、索引刷新、取消）、跨重启状态（job/提案/撤销）、单写者锁。
 * 临时语料建在系统 temp 下，跑完删除。
 */
const { spawn } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const SERVER = path.join(__dirname, 'mcp-server.js');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'awam-mcp-selftest-'));
const CORPUS = path.join(TMP, 'corpus');
const STATE = path.join(TMP, 'state');
const WORKSPACE = path.join(TMP, 'workspace');
const SECRET = 'selftest-secret-0123456789abcdef';

function write(p, s) { fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, s); }

function writeCorpus() {
  write(path.join(CORPUS, 'alpha.md'), [
    '# 向量检索',
    '',
    '倒排索引配合向量检索可以做混合召回。',
    '这一行是填充内容，用来拉开行号。',
  ].join('\n'));
  write(path.join(CORPUS, 'beta.md'), '# 搬家清单\n\n用程序员的思路搬家：先列依赖，再打包。\n');
  write(path.join(CORPUS, 'ignored.md'), '# 忽略我\n\n向量检索不应命中这里。\n');
  fs.mkdirSync(path.join(CORPUS, '.git'), { recursive: true });
  fs.writeFileSync(path.join(CORPUS, '.git', 'hidden.md'), '# 隐藏\n\n向量检索不应命中这里。\n');
}

function env() {
  const e = Object.assign({}, process.env, {
    AWAM_STATE_DIR: STATE,
    AWAM_INDEX_ROOT: CORPUS,
    AWAM_WRITE_ROOT: WORKSPACE,
    AWAM_SECRET: SECRET,
    KM_SETTINGS: path.join(TMP, 'absent-settings.yaml'),
  });
  delete e.SPIKE_ROOT;
  delete e.KM_DATA_ROOT;
  delete e.DESENS_DATA_ROOT;
  delete e.AWAM_SECRET_CMD;
  return e;
}

function rpc(child) {
  let buf = '';
  let idc = 0;
  const pending = new Map();
  const notifications = [];
  child.stdout.on('data', (d) => {
    buf += d.toString();
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      if (!line.trim()) continue;
      let m; try { m = JSON.parse(line); } catch (e) { continue; }
      if (m.method && String(m.method).startsWith('notifications/')) notifications.push(m);
      if (m.id !== undefined && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    }
  });
  return {
    notifications,
    call(method, params) {
      const id = ++idc;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('timeout: ' + method)), 10000);
        pending.set(id, (m) => { clearTimeout(timer); resolve(m); });
        child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
      });
    },
  };
}

async function startServer(environment) {
  const child = spawn(process.execPath, [SERVER], { env: environment || env(), stdio: ['pipe', 'pipe', 'pipe'] });
  const api = rpc(child);
  await api.call('initialize', { clientInfo: { name: 'awam-selftest', version: '1.0' } });
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  return {
    child, call: api.call, notifications: api.notifications,
    stop: () => new Promise(resolve => {
      if (child.exitCode !== null) return resolve();
      child.once('exit', resolve);
      child.kill();
    }),
  };
}

function parseTool(m) {
  if (!m.result || !Array.isArray(m.result.content)) throw new Error('bad tools/call reply: ' + JSON.stringify(m));
  return JSON.parse(m.result.content[0].text);
}

async function tool(server, name, args, meta) {
  const params = { name, arguments: args };
  if (meta) params._meta = meta;
  return parseTool(await server.call('tools/call', params));
}

function makeToken(proposalId, baselineHash, issuedAt) {
  const body = Buffer.from(JSON.stringify({
    proposal_id: proposalId, baseline_hash: baselineHash, issued_at: issuedAt || Date.now(),
  })).toString('base64url');
  const sig = crypto.createHmac('sha256', SECRET).update(body).digest('base64url');
  return body + '.' + sig;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// 直接读状态日志，模拟"另一个进程"在 job 运行期间能看到什么
function journalJobProgress(jobId) {
  let txt = '';
  try { txt = fs.readFileSync(path.join(STATE, 'state.jsonl'), 'utf8'); } catch (e) { return null; }
  let best = null;
  for (const line of txt.split(/\r?\n/)) {
    if (!line) continue;
    let ev;
    try { ev = JSON.parse(line); } catch (e) { continue; }  // 正在追加的行可能不完整
    if (ev.t !== 'job' || !ev.job || ev.job.job_id !== jobId || ev.job.status !== 'running') continue;
    if (!best || (ev.job.done || 0) >= (best.done || 0)) best = ev.job;
  }
  return best ? { done: best.done, total: best.total, phase: best.phase } : null;
}

async function waitJob(server, jobId, timeoutMs) {
  const t0 = Date.now();
  let last = null;
  while (Date.now() - t0 < (timeoutMs || 15000)) {
    last = await tool(server, 'get_job_status', { job_id: jobId });
    if (last && ['done', 'cancelled', 'failed'].indexOf(last.status) !== -1) return last;
    await sleep(20);
  }
  throw new Error('job timeout: ' + jobId + ' last=' + JSON.stringify(last));
}

(async () => {
  writeCorpus();
  let ok = true;
  const checks = [];
  const add = (name, pass, detail) => { checks.push({ name, pass: !!pass, detail: detail === undefined ? null : detail }); };

  const s1 = await startServer();

  // ---- search ----
  const hits = await tool(s1, 'search', { query: '向量检索' });
  const top = hits.hits && hits.hits[0];
  add('search 命中预期文件', top && top.path === 'alpha.md', top && top.path);
  add('返回带行号', top && top.line >= 1);
  add('返回非空片段', top && typeof top.snippet === 'string' && top.snippet.length > 0);
  add('模式标注为 bm25-title4', hits.mode === 'bm25-title4');
  add('命中不含被排除目录', (hits.hits || []).every(h => !h.path.includes('.git')));
  add('索引首次构建写缓存', hits.index && hits.index.cache && hits.index.cache.enabled === true, hits.index && hits.index.cache);

  const topk1 = await tool(s1, 'search', { query: '向量检索', topk: 1 });
  add('topk 生效', (topk1.hits || []).length === 1);

  const empty = await tool(s1, 'search', { query: '  ' });
  add('空查询不报错', Array.isArray(empty.hits) && empty.hits.length === 0);

  // ---- 写入闸 ----
  const outside = await tool(s1, 'propose_write', { target_path: path.join(TMP, '..', 'escape.md'), content: 'x' });
  add('写入根越界被拒', outside.error === 'path_outside_write_root', outside);

  const p1 = await tool(s1, 'propose_write', { target_path: 'notes/one.md', content: '# 一\n\n提案内容。\n' });
  add('提案返回 id 与基线 hash', !!p1.proposal_id && /^[0-9a-f]{64}$/.test(p1.baseline_hash || ''));
  const bad = await tool(s1, 'commit_write', { proposal_id: p1.proposal_id, confirmation_token: 'aaa.bbb' });
  add('坏签名被拒', bad.error === 'rejected' && bad.reason === 'bad_signature', bad);

  const t1 = makeToken(p1.proposal_id, p1.baseline_hash);
  const commit1 = await tool(s1, 'commit_write', { proposal_id: p1.proposal_id, confirmation_token: t1 });
  add('有效 token 提交成功', commit1.committed === true, commit1);
  add('提交走原子写（内容落盘）', fs.readFileSync(path.join(WORKSPACE, 'notes', 'one.md'), 'utf8').includes('提案内容'));
  add('提交后有撤销窗口', commit1.undo_available === true);

  const replay = await tool(s1, 'commit_write', { proposal_id: p1.proposal_id, confirmation_token: t1 });
  add('同进程重放被拒', replay.error === 'already_committed', replay);

  const und1 = await tool(s1, 'undo_write', { proposal_id: p1.proposal_id });
  add('撤销新建文件', und1.undone === true && und1.restored === false, und1);
  add('撤销后文件不存在', !fs.existsSync(path.join(WORKSPACE, 'notes', 'one.md')));
  const und2 = await tool(s1, 'undo_write', { proposal_id: p1.proposal_id });
  add('重复撤销被拒', und2.error === 'undo_already_done', und2);

  // ---- 审计：提案 / 提交 / 拒绝 / 撤销 各一条，且带身份 ----
  const audit1 = await tool(s1, 'get_audit', { limit: 20 });
  const evs = (audit1.entries || []).map(e => e.event);
  add('审计记录了 propose', evs.includes('propose'), evs);
  add('审计记录了 commit', evs.includes('commit'), evs);
  add('审计记录了被拒的提交及原因', (audit1.entries || []).some(e => e.event === 'commit_rejected' && e.reason === 'bad_signature'));
  add('审计记录了 undo', evs.includes('undo'), evs);
  add('审计带客户端身份', (audit1.entries || []).every(e => e.client === 'awam-selftest'), (audit1.entries || [])[0]);
  add('审计文件独立于状态日志', /audit\.jsonl$/.test(audit1.file || ''), audit1.file);
  const committedEntry = (audit1.entries || []).find(e => e.event === 'commit');
  add('审计记下审批时间与提交后 hash',
    !!committedEntry && !!committedEntry.approved_at && /^[0-9a-f]{64}$/.test(committedEntry.committed_hash || ''), committedEntry);

  // P2：提交后不撤销，留给重启后验证
  const p2 = await tool(s1, 'propose_write', { target_path: 'notes/persist.md', content: 'persisted proposal\n' });
  const t2 = makeToken(p2.proposal_id, p2.baseline_hash);
  const commit2 = await tool(s1, 'commit_write', { proposal_id: p2.proposal_id, confirmation_token: t2 });
  add('第二份提案提交成功', commit2.committed === true);

  // ---- 真实导入 + 索引刷新 ----
  write(path.join(CORPUS, 'gamma.md'), '# 伽马射线\n\n伽马射线暴是宇宙中最亮的电磁爆发现象。\n');
  const start = await tool(s1, 'start_import', { root: CORPUS }, { progressToken: 'imp-1' });
  add('start_import 返回 job_id', !!start.job_id && start.status === 'running', start);
  const done = await waitJob(s1, start.job_id);
  add('导入 job 完成', done.status === 'done', done);
  add('导入计数：4 个 Markdown', done.counts && done.counts.markdown_files === 4, done.counts);
  add('manifest 已落盘', done.manifest_written === true);
  const manifest = JSON.parse(fs.readFileSync(done.manifest_path, 'utf8'));
  add('manifest v2 且含摘要', manifest.version === 2 && manifest.summary && manifest.summary.markdown.files === 4);
  add('导入后刷新索引', done.index && done.index.refreshed === true, done.index);
  add('增量索引命中旧文件', done.index && done.index.cache && done.index.cache.hits >= 3, done.index && done.index.cache);
  const gamma = await tool(s1, 'search', { query: '伽马射线暴' });
  add('导入后 search 能命中新文件', (gamma.hits || []).some(h => h.path === 'gamma.md'), (gamma.hits || []).map(h => h.path));
  add('导入发进度通知', s1.notifications.filter(n => n.method === 'notifications/progress' && n.params.progressToken === 'imp-1').length > 0);

  // ---- 取消：大目录 ----
  const busy = path.join(TMP, 'busy');
  fs.mkdirSync(busy, { recursive: true });
  for (let i = 0; i < 1200; i++) write(path.join(busy, 'f' + i + '.md'), '# 文件' + i + '\n\n内容 ' + i + '\n');
  const j2 = await tool(s1, 'start_import', { root: busy });
  const cancel = await tool(s1, 'cancel_job', { job_id: j2.job_id });
  add('取消请求被接受（或 job 已结束）', cancel.cancel_requested === true || !!cancel.note, cancel);
  const cancelled = await waitJob(s1, j2.job_id);
  add('job 状态为 cancelled 或无 manifest', cancelled.status === 'cancelled' ? cancelled.manifest_written === false : cancelled.status === 'done', cancelled);

  // ---- 断点续跑：中断留下检查点，resume_job 起新 job 复用未变文件 ----
  const resumableSrc = path.join(TMP, 'resume-src');
  fs.mkdirSync(resumableSrc, { recursive: true });
  const RESUME_FILES = 5000;
  for (let i = 0; i < RESUME_FILES; i++) write(path.join(resumableSrc, 'r' + i + '.md'), '# 续跑' + i + '\n\n内容 ' + i + '\n');
  const j3 = await tool(s1, 'start_import', { root: resumableSrc });
  // 等到检查点真的落盘再取消，否则这条测试会静默跳过续跑分支
  let j3live = null;
  for (let i = 0; i < 600; i++) {
    j3live = await tool(s1, 'get_job_status', { job_id: j3.job_id });
    if (!j3live || j3live.status !== 'running' || j3live.resumable) break;
    await sleep(5);
  }
  await tool(s1, 'cancel_job', { job_id: j3.job_id });
  const j3done = await waitJob(s1, j3.job_id, 30000);
  add('取消前检查点已落盘', j3done.status === 'cancelled' && j3done.resumable === true,
    { status: j3done.status, resumable: j3done.resumable, checkpoint: j3done.checkpoint_path });
  add('检查点文件真实存在', !!j3done.checkpoint_path && fs.existsSync(j3done.checkpoint_path), j3done.checkpoint_path);

  const resumed = await tool(s1, 'resume_job', { job_id: j3.job_id });
  add('resume_job 返回新 job 且指向原 job', !!resumed.job_id && resumed.job_id !== j3.job_id && resumed.resumed_from === j3.job_id, resumed);
  const resumedDone = await waitJob(s1, resumed.job_id, 60000);
  add('续跑后导入完成并写 manifest', resumedDone.status === 'done' && resumedDone.manifest_written === true, resumedDone);
  const resumedManifest = JSON.parse(fs.readFileSync(resumedDone.manifest_path, 'utf8'));
  add('续跑复用了中断前已处理的文件', resumedManifest.summary.markdown.unchanged >= 1,
    { unchanged: resumedManifest.summary.markdown.unchanged, files: resumedManifest.summary.markdown.files });
  add('续跑结果文件数与源一致', resumedManifest.summary.markdown.files === RESUME_FILES, resumedManifest.summary.markdown.files);
  add('完成后检查点被清理', !fs.existsSync(j3done.checkpoint_path) && resumedDone.resumable === false);
  const again = await tool(s1, 'resume_job', { job_id: j3.job_id });
  add('无检查点时拒绝续跑', again.error === 'no_checkpoint', again);
  const resumeUnknown = await tool(s1, 'resume_job', { job_id: 'job_nope' });
  add('续跑未知 job 报 unknown_job', resumeUnknown.error === 'unknown_job', resumeUnknown);

  // ---- 进度数字：运行期间就与检查点同频落盘，另一个进程不必等结束 ----
  const j5 = await tool(s1, 'start_import', { root: resumableSrc });
  let persisted = null;
  for (let i = 0; i < 400; i++) {
    persisted = journalJobProgress(j5.job_id);
    if (persisted && persisted.done >= 49) break;
    const live = await tool(s1, 'get_job_status', { job_id: j5.job_id });
    if (!live || live.status !== 'running') break;
    await sleep(10);
  }
  add('运行中进度已落盘（≥49 个文件）', !!persisted && persisted.done >= 49 && persisted.total === RESUME_FILES, persisted);
  await tool(s1, 'cancel_job', { job_id: j5.job_id });
  await waitJob(s1, j5.job_id, 30000);

  // ---- 崩溃：进程被直接杀掉，重启后应为 interrupted 且可续跑 ----
  const j4 = await tool(s1, 'start_import', { root: resumableSrc });
  await sleep(150);
  const j4before = await tool(s1, 'get_job_status', { job_id: j4.job_id });
  await s1.stop();

  // ---- 重启：状态持久化 ----
  const s2 = await startServer();
  const jobAfter = await tool(s2, 'get_job_status', { job_id: start.job_id });
  add('重启后 job 仍可查且为 done', jobAfter.status === 'done' && jobAfter.manifest_written === true, jobAfter);
  const cancelAfter = await tool(s2, 'get_job_status', { job_id: j2.job_id });
  add('重启后取消状态保留', ['cancelled', 'done'].indexOf(cancelAfter.status) !== -1, cancelAfter.status);

  const crashed = await tool(s2, 'get_job_status', { job_id: j4.job_id });
  add('崩溃：running job 重启后标记 interrupted', crashed.status === 'interrupted', { before: j4before.status, after: crashed.status });
  add('崩溃后检查点仍在，job 可续跑', crashed.resumable === true, crashed.checkpoint_path);
  add('崩溃前进度数字已落盘（另一进程可见）', crashed.done > 0 && crashed.total === RESUME_FILES,
    { done: crashed.done, total: crashed.total });
  if (crashed.resumable) {
    const resumed2 = await tool(s2, 'resume_job', { job_id: j4.job_id });
    const resumed2Done = await waitJob(s2, resumed2.job_id, 60000);
    const m2 = JSON.parse(fs.readFileSync(resumed2Done.manifest_path, 'utf8'));
    add('崩溃后续跑完成并复用已处理文件', resumed2Done.status === 'done' && m2.summary.markdown.unchanged >= 1 && m2.summary.markdown.files === RESUME_FILES,
      { status: resumed2Done.status, unchanged: m2.summary.markdown.unchanged, files: m2.summary.markdown.files });
  }

  const t2fresh = makeToken(p2.proposal_id, p2.baseline_hash);
  const replayAfter = await tool(s2, 'commit_write', { proposal_id: p2.proposal_id, confirmation_token: t2fresh });
  add('重启后提案仍为已提交', replayAfter.error === 'already_committed', replayAfter);

  const undAfter = await tool(s2, 'undo_write', { proposal_id: p2.proposal_id });
  add('重启后撤销记录可用', undAfter.undone === true, undAfter);
  const auditAfter = await tool(s2, 'get_audit', { limit: 50 });
  add('重启后审计仍在', (auditAfter.entries || []).length >= 5, auditAfter.total);
  add('撤销后持久化文件被删除', !fs.existsSync(path.join(WORKSPACE, 'notes', 'persist.md')));

  // ---- 单写者锁 ----
  const s3 = await startServer();
  const locked = await tool(s3, 'start_import', { root: CORPUS });
  add('第二进程写入被锁拒绝', locked.error === 'state_locked', locked);
  const readOnly = await tool(s3, 'search', { query: '向量检索' });
  add('第二进程仍可只读 search', Array.isArray(readOnly.hits) && readOnly.hits.length > 0);
  await s3.stop();
  await s2.stop();

  // ---- 未配置根 ----
  const noRootEnv = Object.assign({}, env(), { AWAM_INDEX_ROOT: '' });
  const s4 = await startServer(noRootEnv);
  const misconfigured = await tool(s4, 'search', { query: '向量检索' });
  add('未配置根时返回明确错误', misconfigured.error === 'index_root_not_configured', misconfigured);
  await s4.stop();

  const failed = checks.filter(c => !c.pass);
  ok = failed.length === 0;
  console.log(JSON.stringify({ selftest: true, checks: checks.length, pass: ok, failed: failed.map(f => f.name), detail: failed, corpus: TMP }, null, 2));
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  process.exit(ok ? 0 : 1);
})().catch((e) => {
  console.error(e && e.stack || e);
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (x) { /* ignore */ }
  process.exit(1);
});
