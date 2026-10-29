'use strict';
/**
 * 文件与进程工具（零依赖）：原子写、JSONL 日志、目录锁。
 * 写入统一走「临时文件 + rename」，避免半截文件；锁用 pid + 过期时间判定。
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function ensureDir(dir) { fs.mkdirSync(dir, { recursive: true }); }

/** 原子写：同目录临时文件 → fsync → rename 覆盖。返回写入字节数。 */
function atomicWrite(file, data) {
  ensureDir(path.dirname(file));
  const tmp = file + '.tmp-' + process.pid + '-' + crypto.randomBytes(4).toString('hex');
  const buf = Buffer.isBuffer(data) ? data : Buffer.from(String(data), 'utf8');
  const fd = fs.openSync(tmp, 'w');
  try {
    fs.writeFileSync(fd, buf);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  try {
    fs.renameSync(tmp, file);
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch (x) { /* ignore */ }
    throw e;
  }
  return buf.length;
}

function writeJsonAtomic(file, obj) { return atomicWrite(file, JSON.stringify(obj, null, 2)); }

function readJson(file, dflt) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch (e) { return dflt; }
}

function appendJsonl(file, obj) {
  ensureDir(path.dirname(file));
  const fd = fs.openSync(file, 'a');
  try {
    fs.writeSync(fd, JSON.stringify(obj) + '\n');
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

function readJsonl(file) {
  let text;
  try { text = fs.readFileSync(file, 'utf8'); } catch (e) { return []; }
  const out = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try { out.push(JSON.parse(line)); } catch (e) { /* 跳过损坏行 */ }
  }
  return out;
}

function pidAlive(pid) {
  if (!pid || pid === process.pid) return pid === process.pid;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}

/**
 * 单写者锁：文件存在且持有者 pid 仍存活、且未过期时不抢。
 * @returns {{ok:boolean, error?:string, holder?:object, release?:Function}}
 */
function acquireLock(dir, name, opts) {
  opts = opts || {};
  const staleMs = opts.staleMs || 10 * 60 * 1000;
  ensureDir(dir);
  const file = path.join(dir, name + '.lock');
  const token = crypto.randomBytes(8).toString('hex');
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = fs.openSync(file, 'wx');
      try { fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, token, at: Date.now() })); }
      finally { fs.closeSync(fd); }
      return {
        ok: true,
        file,
        release: function () {
          try {
            const cur = JSON.parse(fs.readFileSync(file, 'utf8'));
            if (cur && cur.token === token) fs.unlinkSync(file);
          } catch (e) { /* 已被清理 */ }
        },
      };
    } catch (e) {
      if (e.code !== 'EEXIST') return { ok: false, error: e.code || String(e.message) };
      let cur = null;
      try { cur = JSON.parse(fs.readFileSync(file, 'utf8')); } catch (x) { /* 半截锁文件 */ }
      const stale = !cur || !cur.pid || (Date.now() - (cur.at || 0) > staleMs) || !pidAlive(cur.pid);
      if (!stale) return { ok: false, error: 'locked', holder: cur };
      try { fs.unlinkSync(file); } catch (x) { /* 竞争下可能已被删 */ }
    }
  }
  return { ok: false, error: 'locked' };
}

module.exports = { ensureDir, atomicWrite, writeJsonAtomic, readJson, appendJsonl, readJsonl, pidAlive, acquireLock };
