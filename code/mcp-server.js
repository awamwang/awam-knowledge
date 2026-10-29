#!/usr/bin/env node
/**
 * 最小 MCP server（stdio，零依赖，手写 JSON-RPC 2.0）
 * 暴露：search / propose_write / commit_write / undo_write / start_import / get_job_status / cancel_job / resume_job / get_audit
 *
 * - search：本地 BM25（标题×4），语料根取 AWAM_INDEX_ROOT 或 KM_DATA_ROOT/<KM_SOURCE_ID> 或 settings.yaml；
 *   索引带增量缓存（按 size/mtime/title 复用 chunk），缓存放状态目录。
 * - 写入闸：propose 不落盘；commit 需一次性 token（HMAC、绑基线 hash、TTL），token 用后持久化到状态库，
 *   重启后重放仍被拒；提交走原子写；撤销窗口内可 undo_write。
 * - 导入：start_import 真实遍历目录、写 manifest、报告进度、支持取消；运行中限频写检查点，
 *   中断/取消/崩溃后 resume_job 起新 job 按 size+mtime 复用未变文件。
 * - 状态：job / 已用 token / 撤销记录落在 AWAM_STATE_DIR（默认 .spike-runtime），
 *   快照 state.json + 追加 state.jsonl；同目录单写者锁，第二个进程只读不写。
 *
 * 密钥来源（优先级）：AWAM_SECRET > AWAM_SECRET_CMD > 状态目录下 secret.key（首次生成，0600）。
 * 例：AWAM_SECRET_CMD="bw get password <vault-item>"（Bitwarden CLI）。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { validateIndexRoot } = require('./index-root');
const bm = require('./bm25-index');
const imp = require('./import-md-dir');
const { resolveSecret } = require('./secret-provider');
const { ensureDir, atomicWrite, writeJsonAtomic, readJson, acquireLock } = require('./fs-util');
const { openStore } = require('./store');

const STATE_DIR = path.resolve(process.env.AWAM_STATE_DIR || process.env.SPIKE_ROOT || path.join(__dirname, '..', '.spike-runtime'));
const WRITE_ROOT = path.resolve(process.env.AWAM_WRITE_ROOT || STATE_DIR);
const PROPOSAL_FILE = path.join(STATE_DIR, 'proposals.json');
const IMPORTS_DIR = path.join(STATE_DIR, 'imports');

const PROPOSAL_TTL_MS = Number(process.env.PROPOSAL_TTL_MS || 60000);
const TOKEN_TTL_MS = Number(process.env.TOKEN_TTL_MS || 30000);
const UNDO_WINDOW_MS = Number(process.env.UNDO_WINDOW_MS || 60000);

ensureDir(STATE_DIR);
const LOCK = acquireLock(STATE_DIR, 'store', { staleMs: Number(process.env.AWAM_LOCK_STALE_MS || 600000) });
const STORE = openStore(STATE_DIR, { recover: LOCK.ok });

const sha256 = s => crypto.createHash('sha256').update(String(s)).digest('hex');
const now = () => Date.now();
const norm = p => String(p || '').split('\\').join('/').replace(/\/+$/, '');

// ---------- 审计：写入闸的每次提案/提交/撤销都追加一行，独立于状态日志（不随压实丢失） ----------
let CLIENT = { name: 'unknown', version: null };
function auditWrite(event, extra) {
  try { STORE.audit(Object.assign({ event, client: CLIENT.name, client_version: CLIENT.version }, extra)); }
  catch (e) { /* 审计失败不阻断写入，但也绝不静默改判结果 */ }
}

// ---------- 密钥：环境变量 → 外部命令（如 Bitwarden）→ 本地 0600 文件 ----------
let SECRET = null;
function getSecret() {
  if (SECRET) return SECRET;
  SECRET = resolveSecret({ root: STATE_DIR }).secret;
  return SECRET;
}

function readIfExists(p) { try { return fs.readFileSync(p, 'utf8'); } catch (e) { return ''; } }

function guardWritable() {
  if (LOCK.ok) return null;
  return { error: 'state_locked', reason: LOCK.error || 'locked', holder: LOCK.holder || null,
    note: '同一状态目录已有写者（' + STATE_DIR + '），当前进程只读' };
}

// ---------- 写入闸：提案与一次性 token ----------
const TOKEN_REASONS = {
  MISSING: 'missing_token',
  BAD_SIG: 'bad_signature',
  MISMATCH: 'proposal_mismatch',
  HASH: 'hash_mismatch',
  EXPIRED: 'token_expired',
  REPLAYED: 'token_replayed',
};

function verifyToken(token, proposal) {
  if (!token || typeof token !== 'string' || token.indexOf('.') < 0) {
    return { ok: false, reason: TOKEN_REASONS.MISSING };
  }
  const parts = token.split('.');
  const body = parts[0], sig = parts[1];
  const expect = crypto.createHmac('sha256', getSecret()).update(body).digest('base64url');
  const a = Buffer.from(sig), b = Buffer.from(expect);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return { ok: false, reason: TOKEN_REASONS.BAD_SIG };
  let payload;
  try { payload = JSON.parse(Buffer.from(body, 'base64url').toString('utf8')); }
  catch (e) { return { ok: false, reason: TOKEN_REASONS.BAD_SIG }; }
  if (payload.proposal_id !== proposal.id) return { ok: false, reason: TOKEN_REASONS.MISMATCH };
  if (payload.baseline_hash !== proposal.baseline_hash) return { ok: false, reason: TOKEN_REASONS.HASH };
  if (now() - payload.issued_at > TOKEN_TTL_MS) return { ok: false, reason: TOKEN_REASONS.EXPIRED };
  if (STORE.state.usedTokens.has(token)) return { ok: false, reason: TOKEN_REASONS.REPLAYED };
  return { ok: true, payload };
}

function loadProposals() {
  return readJson(PROPOSAL_FILE, {}) || {};
}
function pruneExpired(proposals) {
  for (const id of Object.keys(proposals)) {
    if (now() > (proposals[id].expires_at || 0)) delete proposals[id];
  }
}
function saveProposals(p) { writeJsonAtomic(PROPOSAL_FILE, p); }

/** 目标路径限定在 WRITE_ROOT 内；越界或绝对路径指向外部一律拒绝。 */
function resolveTarget(target) {
  const p = path.resolve(WRITE_ROOT, String(target || ''));
  const rel = path.relative(WRITE_ROOT, p);
  if (rel.startsWith('..') || path.isAbsolute(rel)) return null;
  return p;
}

function toolProposeWrite(args) {
  const locked = guardWritable();
  if (locked) return locked;
  const target = String((args && args.target_path) || 'notes/new.md');
  const content = String((args && args.content) || '');
  const source = String((args && args.source) || 'session');
  const fp = resolveTarget(target);
  if (!fp) return { error: 'path_outside_write_root', target_path: target, write_root: norm(WRITE_ROOT) };
  const baseline = readIfExists(fp);
  const id = 'prop_' + crypto.randomBytes(6).toString('hex');
  const proposals = loadProposals();
  pruneExpired(proposals);
  const baselineHash = sha256(baseline);
  proposals[id] = {
    id, target_path: target, content, source,
    baseline_hash: baselineHash,
    baseline_exists: fs.existsSync(fp),
    created_at: now(), expires_at: now() + PROPOSAL_TTL_MS,
    status: 'pending',
  };
  saveProposals(proposals);
  auditWrite('propose', { proposal_id: id, target_path: target, baseline_hash: baselineHash, baseline_exists: fs.existsSync(fp), source: source });
  return {
    proposal_id: id,
    target_path: target,
    diff: [
      '--- baseline (' + (baseline ? baseline.split('\n').length + ' lines' : 'new file') + ')',
      '+++ proposal (+' + content.split('\n').length + ' lines)',
    ],
    baseline_hash: baselineHash,
    proposal_ttl_ms: PROPOSAL_TTL_MS,
    persisted: false,
  };
}

function toolCommitWrite(args) {
  const locked = guardWritable();
  if (locked) return locked;
  const id = String((args && args.proposal_id) || '');
  const token = (args && args.confirmation_token) || null;
  const proposals = loadProposals();
  const p = proposals[id];
  if (!p) { auditWrite('commit_rejected', { proposal_id: id, reason: 'unknown_proposal' }); return { error: 'unknown_proposal', proposal_id: id }; }
  if (p.status === 'committed') { auditWrite('commit_rejected', { proposal_id: id, reason: 'already_committed' }); return { error: 'already_committed', proposal_id: id }; }
  if (now() > p.expires_at) { delete proposals[id]; saveProposals(proposals); auditWrite('commit_rejected', { proposal_id: id, reason: 'proposal_expired' }); return { error: 'proposal_expired', proposal_id: id }; }

  let v;
  try { v = verifyToken(token, p); }
  catch (e) { auditWrite('commit_rejected', { proposal_id: id, reason: 'secret_unavailable' }); return { error: 'secret_unavailable', reason: String(e && e.message) }; }
  if (!v.ok) { auditWrite('commit_rejected', { proposal_id: id, reason: v.reason, target_path: p.target_path }); return { error: 'rejected', reason: v.reason, proposal_id: id }; }

  const fp = resolveTarget(p.target_path);
  if (!fp) { auditWrite('commit_rejected', { proposal_id: id, reason: 'path_outside_write_root', target_path: p.target_path }); return { error: 'path_outside_write_root', proposal_id: id }; }

  const prev = readIfExists(fp);
  const currentHash = sha256(prev);
  if (currentHash !== p.baseline_hash) {
    auditWrite('commit_rejected', { proposal_id: id, reason: 'baseline_changed', target_path: p.target_path });
    return { error: 'conflict', reason: 'baseline_changed', proposal_id: id, current_hash: currentHash, baseline_hash: p.baseline_hash };
  }

  atomicWrite(fp, p.content);
  STORE.recordToken(token);                           // 一次性 token，重启后仍拒重放
  p.status = 'committed'; p.committed_at = now();
  saveProposals(proposals);
  let approvedAt = null;
  try { approvedAt = JSON.parse(Buffer.from(String(token).split('.')[0], 'base64url').toString('utf8')).issued_at || null; } catch (e) { /* 仅用于审计 */ }
  auditWrite('commit', { proposal_id: id, target_path: p.target_path, baseline_hash: p.baseline_hash, committed_hash: sha256(p.content), approved_at: approvedAt });
  STORE.recordUndo({
    proposal_id: id, path: norm(p.target_path), existed: p.baseline_exists,
    prev_content: prev, prev_hash: sha256(prev), committed_hash: sha256(p.content),
    at: now(), expires_at: now() + UNDO_WINDOW_MS,
  });
  return { committed: true, proposal_id: id, target_path: p.target_path, undo_window_ms: UNDO_WINDOW_MS, undo_available: true };
}

function toolUndoWrite(args) {
  const locked = guardWritable();
  if (locked) return locked;
  const id = String((args && args.proposal_id) || '');
  const entry = STORE.state.undo.slice().reverse().find(e => e.proposal_id === id);
  if (!entry) return { error: 'no_undo_entry', proposal_id: id };
  if (STORE.state.undone.has(id)) return { error: 'undo_already_done', proposal_id: id };
  if (now() > (entry.expires_at || 0)) return { error: 'undo_expired', proposal_id: id };
  const fp = resolveTarget(entry.path);
  if (!fp) return { error: 'path_outside_write_root', proposal_id: id };
  const currentHash = sha256(readIfExists(fp));
  if (currentHash !== entry.committed_hash) {
    return { error: 'conflict', reason: 'file_changed_after_commit', proposal_id: id, current_hash: currentHash };
  }
  if (entry.existed) atomicWrite(fp, entry.prev_content || '');
  else { try { fs.unlinkSync(fp); } catch (e) { /* 已不存在 */ } }
  STORE.recordUndoUsed(id);
  auditWrite('undo', { proposal_id: id, target_path: entry.path, restored: entry.existed });
  return { undone: true, proposal_id: id, target_path: entry.path, restored: entry.existed };
}

// ---------- search：本地 BM25 + 增量索引缓存 ----------
let CORPUS = null;
let BM25 = null;
let INDEX_INFO = null;

function ensureIndex(force) {
  if (!force && (BM25 || INDEX_INFO)) return;
  BM25 = null; CORPUS = null; INDEX_INFO = null;
  const v = validateIndexRoot(process.env.AWAM_INDEX_ROOT || '');
  if (!v.ok) { INDEX_INFO = { error: v.error, reason: v.reason }; return; }
  const excludes = bm.parseExcludes(process.env.AWAM_EXCLUDES || bm.DEFAULT_EXCLUDES);
  const chunkLines = Number(process.env.AWAM_CHUNK_LINES || bm.DEFAULT_CHUNK_LINES);
  const titleWeight = Number(process.env.AWAM_TITLE_WEIGHT || bm.DEFAULT_TITLE_WEIGHT);
  const cachePath = path.resolve(process.env.AWAM_INDEX_CACHE || path.join(STATE_DIR, 'index-cache.json'));
  CORPUS = bm.buildCorpusCached({ root: v.root, excludes, chunkLines, titleWeight, cachePath });
  BM25 = bm.buildIndex(CORPUS.docs);
  INDEX_INFO = {
    root: v.root,
    docs: new Set(CORPUS.docs.map(d => d.path)).size,
    chunks: BM25.N,
    title_weight: titleWeight,
    excluded: excludes,
    cache: CORPUS.cache,
  };
}

function toolSearch(args) {
  const q = String((args && args.query) || '').trim();
  if (!q) return { hits: [], note: '未找到（空查询）', mode: 'bm25-title4' };
  ensureIndex(false);
  if (INDEX_INFO.error) {
    return { hits: [], error: 'index_root_not_configured', note: INDEX_INFO.error, mode: 'bm25-title4' };
  }
  const topk = Math.max(1, Math.min(Number((args && args.topk) || 5), 50));
  return {
    hits: bm.search(CORPUS, BM25, q, { topk, stem: false }),
    mode: 'bm25-title4',
    index: INDEX_INFO,
    query: q,
  };
}

// ---------- 长任务：真实导入 job + progress 通知 ----------
function notify(method, params) {
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', method: method, params: params }) + '\n');
}

function sameRoot(a, b) {
  return norm(a).toLowerCase() === norm(b).toLowerCase();
}

function maybeRefreshIndex(jobRoot) {
  try {
    const v = validateIndexRoot(process.env.AWAM_INDEX_ROOT || '');
    if (!v.ok || !sameRoot(v.root, jobRoot)) return { refreshed: false, reason: 'index_root_differs' };
    ensureIndex(true);
    return { refreshed: true, docs: INDEX_INFO.docs, chunks: INDEX_INFO.chunks, cache: INDEX_INFO.cache };
  } catch (e) {
    return { refreshed: false, reason: 'refresh_failed:' + String(e && e.message) };
  }
}

function finishJob(job, status, extra) {
  job.status = status;
  job.phase = status;
  job.finished_at = now();
  delete job.current_path;
  if (extra) Object.assign(job, extra);
  job.resumable = !!(job.checkpoint_path && fs.existsSync(job.checkpoint_path));
  if (LOCK.ok) {
    try { STORE.recordJob(job); } catch (e) { /* 状态写入失败不影响结果 */ }
  }
}

async function runImport(job, args, progressToken) {
  const skip = imp.parseSkip(args.skip || imp.DFLT_SKIP.join(','));
  const scanSecrets = args.scan_secrets !== false;
  let resume = null;
  if (args.resume_from) {
    resume = readJson(path.resolve(String(args.resume_from)), null);
    if (!resume) { finishJob(job, 'failed', { errors: ['resume_manifest_unreadable'] }); return; }
  }

  // 检查点：只存已完成 Markdown 的条目（resume 只用得到这些字段），限频落盘
  const ckptPath = job.checkpoint_path || null;
  let lastCkptAt = 0, lastCkptCount = 0;
  const checkpoint = c => {
    if (!ckptPath || !c.markdown.length) return;
    const t = now();
    if (c.markdown.length === lastCkptCount) return;
    if (t - lastCkptAt < 1000 && c.markdown.length - lastCkptCount < 50) return;
    lastCkptAt = t; lastCkptCount = c.markdown.length;
    writeJsonAtomic(ckptPath, {
      version: 2, partial: true, job_id: job.job_id, root: job.root,
      generated_at: new Date().toISOString(), phase: c.phase, markdown: c.markdown,
    });
    job.checkpoint_entries = c.markdown.length;
    // 进度数字与检查点同频落盘：只靠 phase 跃迁写状态，另一个进程读到的是整段 analyzing 的旧值
    job.done = c.done; job.total = c.total;
    if (LOCK.ok) {
      try { STORE.recordJob(job); } catch (e) { /* 状态写入失败不影响导入 */ }
    }
  };

  const it = imp.importSteps(job.root, skip, {
    scanSecrets, resume, checkpoint,
    shouldCancel: () => !!job.cancel_requested,
  });
  let lastNotify = 0, steps = 0;
  for (;;) {
    if (job.cancel_requested) {
      finishJob(job, 'cancelled', { done: job.done });
      return;
    }
    const s = it.next();
    if (s.done) {
      const r = s.value || {};
      if (r.cancelled) { finishJob(job, 'cancelled', { done: r.processed || 0 }); return; }
      const manifest = {
        version: 2, generated_at: new Date().toISOString(), job_id: job.job_id,
        summary: r.summary, markdown: r.markdown, attachments: r.attachments,
        unresolved: r.unresolved, security: r.security, duplicate_groups: r.duplicateGroups,
      };
      writeJsonAtomic(job.manifest_path, manifest);
      const counts = {
        markdown_files: r.summary.markdown.files,
        attachments: r.summary.attachments.files,
        unresolved: r.unresolved.length,
        security_files: r.summary.security.files_with_hits,
        walk_errors: r.walkErrors.length,
      };
      const index = maybeRefreshIndex(job.root);
      const errors = r.walkErrors.slice(0, 20).map(e => e.path + ': ' + e.error);
      if (r.unresolved.length) errors.push('unresolved_links: ' + r.unresolved.length + '（明细见 manifest）');
      if (r.summary.security.files_with_hits) errors.push('secret_pattern_hits: ' + r.summary.security.files_with_hits + ' 个文件');
      const units = r.summary.markdown.files + r.summary.attachments.files;
      if (ckptPath) {
        try { fs.unlinkSync(ckptPath); } catch (e) { /* 已不存在 */ }
        if (job.resume_from_path) { try { fs.unlinkSync(job.resume_from_path); } catch (e) { /* 已被删或不可写 */ } }
      }
      finishJob(job, 'done', { done: units, total: units, counts, errors, index });
      if (progressToken) notify('notifications/progress', { progressToken, progress: units, total: units, message: '导入完成' });
      return;
    }
    const v = s.value;
    const phaseChanged = v.phase !== job.phase;
    job.phase = v.phase; job.done = v.done; job.total = v.total;
    if (v.path) job.current_path = v.path;
    if (phaseChanged && LOCK.ok) {
      try { STORE.recordJob(job); } catch (e) { /* 状态写入失败不影响导入 */ }
    }
    if (progressToken && v.total > 0 && (v.done === 0 || v.done === v.total || Date.now() - lastNotify > 250)) {
      lastNotify = Date.now();
      notify('notifications/progress', {
        progressToken, progress: v.done, total: v.total,
        message: (v.phase === 'attachments' ? '附件' : '导入') + ' ' + v.done + '/' + v.total,
      });
    }
    if (++steps % 8 === 0) await new Promise(r => setImmediate(r));
  }
}

function toolGetAudit(args) {
  const r = STORE.readAudit({ limit: args && args.limit, since: args && args.since });
  return { total: r.total, entries: r.entries, file: path.join(STATE_DIR, 'audit.jsonl') };
}

function toolStartImport(args, progressToken) {
  const locked = guardWritable();
  if (locked) return locked;
  const rawRoot = String((args && args.root) || '').trim();
  if (!rawRoot) return { error: 'root_required', note: '需要 root：要导入的 Markdown 目录（只读源）' };
  const root = path.resolve(rawRoot);
  let st;
  try { st = fs.statSync(root); } catch (e) { return { error: 'root_not_found', root: norm(root) }; }
  if (!st.isDirectory()) return { error: 'root_not_directory', root: norm(root) };

  const jobId = 'job_' + crypto.randomBytes(5).toString('hex');
  const job = {
    job_id: jobId, kind: 'import', root: norm(root), status: 'running', phase: 'starting',
    done: 0, total: 0, errors: [], cancel_requested: false, started_at: now(),
    args: { skip: args.skip || null, scan_secrets: args.scan_secrets !== false },
    manifest_path: norm(path.join(IMPORTS_DIR, jobId + '.json')),
    checkpoint_path: norm(path.join(IMPORTS_DIR, jobId + '.partial.json')),
  };
  STORE.recordJob(job);
  runImport(job, args || {}, progressToken).catch(e => {
    finishJob(job, 'failed', { errors: [String((e && e.message) || e)] });
  });
  return { job_id: jobId, status: 'running', root: job.root, progress_notifications: !!progressToken };
}

function toolGetJobStatus(args) {
  const job = STORE.state.jobs[String((args && args.job_id) || '')];
  if (!job) return { error: 'unknown_job' };
  return {
    job_id: job.job_id, kind: job.kind, phase: job.phase, status: job.status,
    done: job.done, total: job.total, errors: job.errors,
    root: job.root, manifest_path: job.manifest_path,
    manifest_written: !!(job.manifest_path && fs.existsSync(job.manifest_path)),
    counts: job.counts || null, index: job.index || null,
    resumed_from: job.resumed_from || null,
    resumable: !!(job.checkpoint_path && fs.existsSync(job.checkpoint_path)),
    checkpoint_path: job.checkpoint_path || null,
    started_at: job.started_at || null, finished_at: job.finished_at || null,
  };
}

function toolCancelJob(args) {
  const locked = guardWritable();
  if (locked) return locked;
  const job = STORE.state.jobs[String((args && args.job_id) || '')];
  if (!job) return { error: 'unknown_job' };
  if (job.status !== 'running') return { job_id: job.job_id, cancel_requested: false, status: job.status, note: 'job 已结束' };
  job.cancel_requested = true;
  return { job_id: job.job_id, cancel_requested: true };
}

// 断点续跑：只认中断/取消/失败后留下的检查点，起一个新 job 指向原 job
function toolResumeJob(args, progressToken) {
  const locked = guardWritable();
  if (locked) return locked;
  const prev = STORE.state.jobs[String((args && args.job_id) || '')];
  if (!prev) return { error: 'unknown_job' };
  if (prev.status === 'running') return { error: 'already_running', job_id: prev.job_id };
  const ckpt = prev.checkpoint_path || path.join(IMPORTS_DIR, prev.job_id + '.partial.json');
  if (!fs.existsSync(ckpt)) {
    return { error: 'no_checkpoint', job_id: prev.job_id, note: '没有可续跑的检查点（任务已完成，或中断前还没处理任何文件）' };
  }
  if (!fs.existsSync(prev.root)) return { error: 'root_not_found', root: prev.root };

  const jobId = 'job_' + crypto.randomBytes(5).toString('hex');
  const job = {
    job_id: jobId, kind: 'import', root: prev.root, status: 'running', phase: 'starting',
    done: 0, total: 0, errors: [], cancel_requested: false, started_at: now(),
    resumed_from: prev.job_id, resume_from_path: norm(ckpt),
    args: Object.assign({}, prev.args || {}),
    manifest_path: norm(path.join(IMPORTS_DIR, jobId + '.json')),
    checkpoint_path: norm(path.join(IMPORTS_DIR, jobId + '.partial.json')),
  };
  STORE.recordJob(job);
  const runArgs = Object.assign({}, job.args, { resume_from: ckpt });
  runImport(job, runArgs, progressToken).catch(e => {
    finishJob(job, 'failed', { errors: [String((e && e.message) || e)] });
  });
  return {
    job_id: jobId, resumed_from: prev.job_id, resume_from: norm(ckpt),
    status: 'running', progress_notifications: !!progressToken,
  };
}

// ---------- tools/list ----------
const TOOLS = [
  { name: 'search', description: '本地 BM25 召回（标题×4）；语料根取 AWAM_INDEX_ROOT / KM_DATA_ROOT+KM_SOURCE_ID / settings.yaml，带增量索引缓存',
    inputSchema: { type: 'object', properties: { query: { type: 'string' }, topk: { type: 'number' } }, required: ['query'] },
    annotations: { readOnlyHint: true, destructiveHint: false } },
  { name: 'propose_write', description: '生成写入提案（不落盘）；target_path 限定在写入根内',
    inputSchema: { type: 'object', properties: { target_path: { type: 'string' }, content: { type: 'string' }, source: { type: 'string' } }, required: ['target_path', 'content'] },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: false } },
  { name: 'commit_write', description: '提交写入（需用户签发的一次性 token；原子写；token 用后持久化，重启后重放仍拒）',
    inputSchema: { type: 'object', properties: { proposal_id: { type: 'string' }, confirmation_token: { type: 'string' } }, required: ['proposal_id', 'confirmation_token'] },
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false } },
  { name: 'undo_write', description: '撤销窗口内回滚一次已提交写入（文件被后续改动时拒绝）',
    inputSchema: { type: 'object', properties: { proposal_id: { type: 'string' } }, required: ['proposal_id'] },
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false } },
  { name: 'start_import', description: '启动真实导入长任务：遍历 root 下 Markdown 与附件，产出 manifest（状态目录 imports/<job_id>.json），可取消',
    inputSchema: {
      type: 'object',
      properties: {
        root: { type: 'string' },
        skip: { type: 'string', description: '逗号分隔的目录名；缺省用内置清单' },
        scan_secrets: { type: 'boolean', description: '默认 true' },
        resume_from: { type: 'string', description: '上一份 manifest 路径，按 size+mtime 复用未变文件' },
      },
      required: ['root'],
    },
    annotations: { readOnlyHint: false, destructiveHint: false } },
  { name: 'get_job_status', description: '查询 job 状态（跨进程可见，状态持久化）',
    inputSchema: { type: 'object', properties: { job_id: { type: 'string' } }, required: ['job_id'] },
    annotations: { readOnlyHint: true, destructiveHint: false } },
  { name: 'cancel_job', description: '取消运行中的 job（下一个文件边界生效，不写 manifest）',
    inputSchema: { type: 'object', properties: { job_id: { type: 'string' } }, required: ['job_id'] },
    annotations: { readOnlyHint: false, destructiveHint: false } },
  { name: 'resume_job', description: '用中断/取消留下的检查点续跑导入（新 job，未变文件复用，不改源目录）',
    inputSchema: { type: 'object', properties: { job_id: { type: 'string' } }, required: ['job_id'] },
    annotations: { readOnlyHint: false, destructiveHint: false } },
  { name: 'get_audit', description: '读取写入闸审计（提案/提交/拒绝/撤销，只追加不重写）',
    inputSchema: { type: 'object', properties: {
      limit: { type: 'number', description: '返回最近 N 条，默认 100，上限 2000' },
      since: { type: 'number', description: '只返回该时间戳（毫秒）之后的记录' },
    } },
    annotations: { readOnlyHint: true, destructiveHint: false } },
];

// ---------- 分发 ----------
function dispatch(msg) {
  const id = msg.id;
  const method = msg.method;
  const params = msg.params || {};
  const reply = (result) => { if (id !== undefined) process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: id, result: result }) + '\n'); };
  const fail = (code, message) => { if (id !== undefined) process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: id, error: { code: code, message: message } }) + '\n'); };

  if (method === 'initialize') {
    const ci = params.clientInfo || {};
    CLIENT = { name: String(ci.name || 'unknown'), version: ci.version || null };
    return reply({
      protocolVersion: '2024-11-05',
      capabilities: { tools: { listChanged: false }, logging: {} },
      serverInfo: { name: 'awam-knowledge', version: '0.2.0' },
    });
  }
  if (method === 'notifications/initialized') return;
  if (method === 'ping') return reply({});
  if (method === 'tools/list') return reply({ tools: TOOLS });
  if (method === 'tools/call') {
    const name = params.name;
    const args = params.arguments || {};
    const progressToken = params._meta && params._meta.progressToken;
    let out;
    try {
      if (name === 'search') out = toolSearch(args);
      else if (name === 'propose_write') out = toolProposeWrite(args);
      else if (name === 'commit_write') out = toolCommitWrite(args);
      else if (name === 'undo_write') out = toolUndoWrite(args);
      else if (name === 'start_import') out = toolStartImport(args, progressToken);
      else if (name === 'get_job_status') out = toolGetJobStatus(args);
      else if (name === 'cancel_job') out = toolCancelJob(args);
      else if (name === 'resume_job') out = toolResumeJob(args, progressToken);
      else if (name === 'get_audit') out = toolGetAudit(args);
      else return fail(-32601, 'unknown tool: ' + name);
    } catch (e) { return fail(-32000, String(e && e.message)); }
    const isError = !!(out && out.error);
    return reply({ content: [{ type: 'text', text: JSON.stringify(out) }], isError: isError });
  }
  return fail(-32601, 'method not found: ' + method);
}

const readline = require('readline');
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', function (line) {
  const s = line.trim();
  if (!s) return;
  let msg;
  try { msg = JSON.parse(s); } catch (e) { return; }
  try { dispatch(msg); } catch (e) { /* 保持进程存活 */ }
});

function cleanup() {
  try { if (LOCK.ok) LOCK.release(); } catch (e) { /* ignore */ }
}
process.stdin.on('end', function () { cleanup(); process.exit(0); });
process.on('SIGINT', function () { cleanup(); process.exit(0); });
process.on('SIGTERM', function () { cleanup(); process.exit(0); });
