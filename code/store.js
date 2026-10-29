'use strict';
/**
 * 进程外状态（零依赖）：job、一次性 token、撤销记录。
 *
 * 快照 state.json + 追加日志 state.jsonl：
 * - 每次变更追加一行事件（带 at），写入即落盘；
 * - 启动时先读快照，再回放 at 晚于快照 saved_at 的事件；
 * - 日志超过阈值就重写快照并清空日志（先写快照再清空）。
 *
 * 持久化状态跃迁（start/done/cancelled/failed）与检查点同频的 done/total，不逐文件写进度；
 * 进程崩溃后未完成的 job 在下次启动时标记为 interrupted（不自动续跑）。
 *
 * 另有一份独立的审计日志 audit.jsonl：只追加、永不重写，状态日志压实不影响它。
 */
const fs = require('fs');
const path = require('path');
const { ensureDir, appendJsonl, readJsonl, readJson, writeJsonAtomic } = require('./fs-util');

const SNAPSHOT = 'state.json';
const JOURNAL = 'state.jsonl';
const AUDIT = 'audit.jsonl';
const JOURNAL_MAX_BYTES = 512 * 1024;

function newState() {
  return { jobs: Object.create(null), usedTokens: new Set(), undo: [], undone: new Set() };
}

function applySnapshot(state, snap) {
  if (!snap || typeof snap !== 'object') return;
  for (const j of snap.jobs || []) if (j && j.job_id) state.jobs[j.job_id] = j;
  for (const t of snap.tokens || []) state.usedTokens.add(t);
  if (Array.isArray(snap.undo)) state.undo = snap.undo.slice();
  for (const id of snap.undone || []) state.undone.add(id);
}

function applyEvent(state, ev) {
  if (!ev || typeof ev !== 'object') return;
  if (ev.t === 'job' && ev.job && ev.job.job_id) state.jobs[ev.job.job_id] = ev.job;
  else if (ev.t === 'token' && ev.token) state.usedTokens.add(ev.token);
  else if (ev.t === 'undo' && ev.entry) state.undo.push(ev.entry);
  else if (ev.t === 'undo_used' && ev.proposal_id) state.undone.add(ev.proposal_id);
}

function openStore(dir, opts) {
  opts = opts || {};
  ensureDir(dir);
  const snapshotPath = path.join(dir, SNAPSHOT);
  const journalPath = path.join(dir, JOURNAL);
  const auditPath = path.join(dir, AUDIT);
  const state = newState();

  const snap = readJson(snapshotPath, null);
  applySnapshot(state, snap);
  const since = (snap && snap.saved_at) || 0;
  for (const ev of readJsonl(journalPath)) if ((ev.at || 0) >= since) applyEvent(state, ev);

  const interrupted = [];
  if (opts.recover !== false) {
    for (const id of Object.keys(state.jobs)) {
      const j = state.jobs[id];
      if (j.status === 'running') {
        j.status = 'interrupted';
        j.phase = 'interrupted';
        j.interrupted_at = Date.now();
        interrupted.push(id);
      }
    }
  }

  function append(ev) {
    ev.at = Date.now();
    appendJsonl(journalPath, ev);
    return ev;
  }

  function compact() {
    writeJsonAtomic(snapshotPath, {
      saved_at: Date.now(),
      jobs: Object.values(state.jobs),
      tokens: [...state.usedTokens],
      undo: state.undo,
      undone: [...state.undone],
    });
    try { fs.truncateSync(journalPath, 0); } catch (e) { /* 日志可能不存在 */ }
  }

  function maybeCompact() {
    try { if (fs.statSync(journalPath).size > JOURNAL_MAX_BYTES) compact(); } catch (e) { /* ignore */ }
  }

  return {
    dir,
    state,
    interrupted,
    recordJob(job) { state.jobs[job.job_id] = job; append({ t: 'job', job }); maybeCompact(); },
    recordToken(token) { state.usedTokens.add(token); append({ t: 'token', token }); maybeCompact(); },
    recordUndo(entry) { state.undo.push(entry); append({ t: 'undo', entry }); maybeCompact(); },
    recordUndoUsed(proposalId) { state.undone.add(proposalId); append({ t: 'undo_used', proposal_id: proposalId }); maybeCompact(); },
    compact,
    // 审计与状态日志分开：状态日志会被压实/截断，审计只追加、永不重写
    audit(rec) { appendJsonl(auditPath, Object.assign({ at: Date.now() }, rec)); },
    readAudit(opts) {
      const o = opts || {};
      const since = Number(o.since || 0) || 0;
      const rawLimit = (o.limit === undefined || o.limit === null || o.limit === '') ? 100 : Number(o.limit);
      const limit = Number.isFinite(rawLimit) ? Math.max(1, Math.min(Math.trunc(rawLimit), 2000)) : 100;
      const all = readJsonl(auditPath).filter(e => (e.at || 0) >= since);
      return { total: all.length, entries: all.slice(-limit) };
    },
  };
}

module.exports = { openStore, SNAPSHOT, JOURNAL, AUDIT };
