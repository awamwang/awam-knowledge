#!/usr/bin/env node
'use strict';
/**
 * awam-knowledge HTTP 服务（AFM 集成）：内容检索 + 账号引用取用
 * 设计契约：design/afm-http-service-design.md（2026-11-18 定稿）
 *
 * 端点（除 /health 外都要求 Authorization: Bearer <KM_SERVICE_TOKEN>，常量时间比较）：
 *   GET  /health           探活（无敏感字段，不鉴权）
 *   POST /search           BM25 内容检索（复用 bm25-index.js 增量缓存）+ 引用提取
 *   POST /vault/status     代理 agent_secret.py status（窗口元数据，无值）
 *   POST /vault/reveal     代理 agent_secret.py resolve（人机通道，唯一明文响应体）
 *   POST /vault/unlock     代理 unlock --yes（body.confirm !== true → 400）
 *   POST /vault/lock       代理 lock
 *
 * 配置（环境变量）：
 *   KM_SERVICE_HOST / KM_SERVICE_PORT（默认 127.0.0.1:18790）
 *   KM_SERVICE_TOKEN（必填，未设置启动即退出码 2）
 *   索引根沿用 index-root.js（AWAM_INDEX_ROOT → KM_DATA_ROOT/KM_SOURCE_ID → settings.yaml）
 *   KM_SECRET_PY（默认 python）/ KM_SECRET_SCRIPT / KM_SECRET_ID 或 KM_SECRET_DATA_ROOT /
 *   KM_SECRET_AGENT_TOKEN —— vault 四项任一缺失 → /vault/* 返回 503 vault_not_configured
 *
 * 红线：
 *   1. 明文只出现在 /vault/reveal 的响应体与子进程 stdout→内存；不写日志、不进缓存、不进 console.*。
 *   2. 审计由人机通道侧落盘（secret-access.jsonl，无值）；本服务只记「方法+路径+状态码」访问日志。
 *   3. token 常量时间比较；所有响应 Cache-Control: no-store。
 *   4. 索引根拒绝真源（index-root.js），HTTP 面不提供绕过参数。
 *   5. vault 子进程超时（10s）即杀；python 不存在/脚本报错 → 503 vault_backend_error。
 */
const http = require('http');
const crypto = require('crypto');
const path = require('path');
const os = require('os');
const cp = require('child_process');
const { validateIndexRoot } = require('./index-root');
const bm = require('./bm25-index');

const SERVICE_NAME = 'awam-knowledge-http';
const BODY_LIMIT = 1024 * 1024;        // 请求体上限 1MB
const VAULT_TIMEOUT_MS = 10000;        // 人机通道子进程超时
const DEFAULT_TOPK = 12;
const MAX_TOPK = 50;

// ---------- 引用提取 ----------
// 引用形态（脱敏镜像回填后）：`见保险库：<条目名> [vault:<label>:<hash8>]`
const VAULT_REF_RX = /见保险库：([^\[\]\n]{1,120}?)\s*\[vault:([A-Za-z0-9_\-]+):([0-9a-f]{8})\]/g;
// 裸占位形态（backfill 之前 / 非 credentials 区块）：`[REDACTED:<label>:<hash8>]`
const REDACTED_RX = /\[REDACTED:([A-Za-z0-9_\-]+):([0-9a-f]{8})\]/g;
// reveal 入参必须严格匹配条目名主键，杜绝任意字符串进 CLI
const ENTRY_NAME_RX = /^\[REDACTED:[A-Za-z0-9_\-]+:[0-9a-f]{8}\]$/;

/**
 * 从一段脱敏文本中提取账号引用。
 * 两种形态 → 归一化 key（= map.json 条目名 [REDACTED:label:hash8]）。
 * 同 key 去重，display 保留最长者。返回数组（保持首次出现顺序）。
 */
function extractRefs(text) {
  const seen = new Map(); // key -> {kind, key, display}
  const push = (kind, key, display) => {
    const prev = seen.get(key);
    if (!prev || display.length > prev.display.length) seen.set(key, { kind, key, display });
  };
  let m;
  VAULT_REF_RX.lastIndex = 0;
  while ((m = VAULT_REF_RX.exec(text)) !== null) {
    push('vault', '[REDACTED:' + m[2] + ':' + m[3] + ']', m[1].trim());
  }
  REDACTED_RX.lastIndex = 0;
  while ((m = REDACTED_RX.exec(text)) !== null) {
    push('redacted', m[0], m[1]);
  }
  return [...seen.values()];
}

// ---------- 配置 ----------
function loadConfig(env) {
  env = env || process.env;
  const token = String(env.KM_SERVICE_TOKEN || '').trim();
  const vaultScript = String(env.KM_SECRET_SCRIPT || '').trim();
  const vaultToken = String(env.KM_SECRET_AGENT_TOKEN || '').trim();
  const vaultId = String(env.KM_SECRET_ID || '').trim();
  const vaultDataRoot = String(env.KM_SECRET_DATA_ROOT || '').trim();
  const cfg = {
    host: String(env.KM_SERVICE_HOST || '127.0.0.1'),
    port: Number(env.KM_SERVICE_PORT || 18790),
    token,
    log: String(env.KM_SERVICE_LOG || '1') !== '0',
    indexRoot: String(env.AWAM_INDEX_ROOT || ''),
    excludes: bm.parseExcludes(env.AWAM_EXCLUDES || bm.DEFAULT_EXCLUDES),
    chunkLines: Number(env.AWAM_CHUNK_LINES || bm.DEFAULT_CHUNK_LINES),
    titleWeight: Number(env.AWAM_TITLE_WEIGHT || bm.DEFAULT_TITLE_WEIGHT),
    indexCache: env.AWAM_INDEX_CACHE
      ? path.resolve(env.AWAM_INDEX_CACHE)
      : path.resolve(env.AWAM_STATE_DIR || os.homedir(), '.awam-knowledge-http', 'index-cache.json'),
    vault: {
      py: String(env.KM_SECRET_PY || 'python'),
      script: vaultScript,
      token: vaultToken,
      id: vaultId,
      dataRoot: vaultDataRoot,
      // 四项（脚本、token、id/data-root 二选一）齐备才启用 vault
      configured: !!(vaultScript && vaultToken && (vaultDataRoot || vaultId)),
    },
  };
  return cfg;
}

// ---------- BM25 索引（懒加载 + 增量缓存，同 mcp-server 的 ensureIndex） ----------
function makeIndex(cfg) {
  let corpus = null;
  let idx = null;
  let info = null; // {root, docs, chunks} | {error, reason}

  function ensure(force) {
    if (!force && idx) return;
    corpus = null; idx = null; info = null;
    const v = validateIndexRoot(cfg.indexRoot);
    if (!v.ok) { info = { error: v.error, reason: v.reason }; return; }
    corpus = bm.buildCorpusCached({
      root: v.root, excludes: cfg.excludes,
      chunkLines: cfg.chunkLines, titleWeight: cfg.titleWeight,
      cachePath: cfg.indexCache,
    });
    idx = bm.buildIndex(corpus.docs);
    info = {
      root: v.root,
      docs: new Set(corpus.docs.map(d => d.path)).size,
      chunks: idx.N,
      cache: corpus.cache,
    };
  }

  function rootInfo() {
    const v = validateIndexRoot(cfg.indexRoot);
    return v.ok
      ? { configured: true, root: v.root, docs: info ? info.docs : null, chunks: info ? info.chunks : null }
      : { configured: false, root: null, docs: null, chunks: null, reason: v.reason };
  }

  function search(q, topk, snippetWidth) {
    ensure(false);
    if (!idx) return { error: 'index_root_not_configured', reason: info && info.error };
    const weights = bm.queryWeights(q, { stem: false });
    const terms = [...weights.keys()];
    const ranked = bm.rankAll(corpus.docs, idx, weights).slice(0, topk);
    const hits = ranked.map(r => {
      const d = corpus.docs[r.di];
      return {
        path: d.path, line: d.lineStart, lines: d.lineStart + '-' + d.lineEnd,
        title: d.title, score: Number(r.score.toFixed(4)),
        snippet: bm.snippetOf(d.text, terms, snippetWidth),
        refs: extractRefs(d.text),
      };
    });
    return { hits, index: { root: info.root, docs: info.docs, chunks: info.chunks } };
  }

  return { ensure, search, rootInfo };
}

// ---------- vault 代理（agent_secret.py 人机通道） ----------
function makeVault(vcfg) {
  const baseArgs = () => (vcfg.dataRoot
    ? ['--data-root', vcfg.dataRoot]
    : ['--id', vcfg.id]);

  /**
   * 跑一次人机通道子命令。返回：
   *   {ok:true, json}            — opts.json 时：stdout 解析出的 JSON（resolve 成功时含 value，调用方注意红线）
   *   {ok:true, raw:true}        — 非 JSON 模式（unlock/lock 的人类可读输出）：仅当退出码 0
   *   {ok:false, status, code, reason} — 失败（backend/timeout/denied 已映射 HTTP 状态码）
   * 红线：失败时绝不回显 stdout（成功输出可能已含明文片段）。
   */
  function run(args, opts) {
    const wantJson = !opts || opts.json !== false; // 默认 JSON 模式；unlock/lock 显式 {json:false}
    if (!vcfg.configured) {
      return { ok: false, status: 503, code: 'vault_not_configured', reason: '未配置 KM_SECRET_SCRIPT / KM_SECRET_AGENT_TOKEN / KM_SECRET_ID(或 KM_SECRET_DATA_ROOT)' };
    }
    let child;
    try {
      child = cp.spawnSync(vcfg.py, [vcfg.script, ...baseArgs(), ...args], {
        timeout: VAULT_TIMEOUT_MS,
        encoding: 'utf8',
        windowsHide: true,
        env: Object.assign({}, process.env, { AGENT_SECRET_TOKEN: vcfg.token }),
      });
    } catch (e) {
      return { ok: false, status: 503, code: 'vault_backend_error', reason: 'vault 子进程启动失败' };
    }
    if (child.error) {
      if (child.error.code === 'ENOENT') {
        return { ok: false, status: 503, code: 'vault_backend_error', reason: 'python 不可用：' + vcfg.py };
      }
      return { ok: false, status: 503, code: 'vault_backend_error', reason: 'vault 子进程错误' };
    }
    if (child.signal) {
      return { ok: false, status: 503, code: 'vault_timeout', reason: 'vault 子进程超时（>' + VAULT_TIMEOUT_MS + 'ms）' };
    }
    if (!wantJson) {
      // unlock/lock：人类可读输出，只看退出码；stderr 摘要只取 python 报错（无值）
      if (child.status === 0) return { ok: true, raw: true };
      const errTail = String(child.stderr || '').trim().slice(0, 200);
      return { ok: false, status: 503, code: 'vault_backend_error', reason: 'vault 子进程失败' + (errTail ? '：' + errTail : '') };
    }
    // stdout 末行 JSON（agent_secret 的人机通道 JSON 子命令保证一行 JSON）
    const lines = String(child.stdout || '').split(/\r?\n/).filter(s => s.trim());
    const last = lines.length ? lines[lines.length - 1] : '';
    let json = null;
    try { json = JSON.parse(last); } catch (e) { json = null; }
    if (!json || typeof json !== 'object') {
      // 解析失败：不回显 stdout（可能截断了含明文的成功行）；stderr 摘要只取 python 报错（无值）
      const errTail = String(child.stderr || '').trim().slice(0, 200);
      return { ok: false, status: 503, code: 'vault_backend_error', reason: 'vault 输出无法解析' + (errTail ? '：' + errTail : '') };
    }
    if (json.ok === true) return { ok: true, json };
    // deny 路径：json = {ok:false, code, reason}（无值）
    const code = String(json.code || 'denied');
    const reason = String(json.reason || '');
    if (code === 'unlock_required') return { ok: false, status: 423, code, reason };
    if (code === 'rate_limited') return { ok: false, status: 429, code, reason };
    return { ok: false, status: 403, code, reason }; // human_channel_required / not_found / denied
  }

  return { run };
}

// ---------- HTTP 应用 ----------
function sha256b(s) { return crypto.createHash('sha256').update(String(s), 'utf8').digest(); }

function createApp(cfg) {
  const index = makeIndex(cfg);
  const vault = makeVault(cfg.vault);
  const tokenHash = cfg.token ? sha256b(cfg.token) : null;

  function authed(req) {
    const h = String(req.headers['authorization'] || '');
    const m = /^Bearer\s+(.+)$/.exec(h);
    if (!m || !tokenHash) return false;
    const a = sha256b(m[1].trim());
    return a.length === tokenHash.length && crypto.timingSafeEqual(a, tokenHash);
  }

  function send(res, status, obj) {
    const body = JSON.stringify(obj);
    res.writeHead(status, {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': Buffer.byteLength(body),
      'Cache-Control': 'no-store',
    });
    res.end(body);
    return status;
  }

  function accessLog(req, status) {
    if (!cfg.log) return;
    console.log(new Date().toISOString() + ' ' + req.method + ' ' + req.url.split('?')[0] + ' ' + status);
  }

  function readBody(req) {
    return new Promise((resolve) => {
      const chunks = [];
      let size = 0;
      let aborted = false;
      req.on('data', (c) => {
        if (aborted) return;
        size += c.length;
        if (size > BODY_LIMIT) { aborted = true; resolve({ error: 'body_too_large' }); req.destroy(); return; }
        chunks.push(c);
      });
      req.on('end', () => {
        if (aborted) return;
        const raw = Buffer.concat(chunks).toString('utf8');
        if (!raw.trim()) return resolve({ json: {} });
        try { resolve({ json: JSON.parse(raw) }); }
        catch (e) { resolve({ error: 'invalid_json' }); }
      });
      req.on('error', () => { if (!aborted) { aborted = true; resolve({ error: 'read_error' }); } });
    });
  }

  const server = http.createServer((req, res) => {
    let u;
    try { u = new URL(req.url, 'http://127.0.0.1'); }
    catch (e) { accessLog(req, send(res, 400, { code: 'bad_request' })); return; }
    const p = u.pathname.replace(/\/+$/, '') || '/';

    // 唯一不鉴权的端点
    if (req.method === 'GET' && p === '/health') {
      const ri = index.rootInfo();
      accessLog(req, send(res, 200, {
        ok: true, service: SERVICE_NAME,
        index: ri,
        vault: { configured: cfg.vault.configured },
      }));
      return;
    }

    if (!authed(req)) { accessLog(req, send(res, 401, { code: 'unauthorized' })); return; }

    Promise.resolve()
      .then(async () => {
        if (req.method === 'POST' && p === '/search') return handleSearch(req, res);
        if (req.method === 'POST' && p === '/vault/status') return handleVaultStatus(req, res);
        if (req.method === 'POST' && p === '/vault/reveal') return handleVaultReveal(req, res);
        if (req.method === 'POST' && p === '/vault/unlock') return handleVaultUnlock(req, res);
        if (req.method === 'POST' && p === '/vault/lock') return handleVaultLock(req, res);
        accessLog(req, send(res, 404, { code: 'not_found' }));
      })
      .catch((e) => {
        // 兜底：处理器可能已写过头（如 reveal 的明文响应体），此时只毁连接，绝不再写
        if (res.headersSent) { res.destroy(); }
        else { accessLog(req, send(res, 500, { code: 'internal_error' })); }
        if (cfg.log) console.error(new Date().toISOString() + ' internal_error ' + p + ' ' + String(e && e.message));
      });

    async function handleSearch(req, res) {
      const b = await readBody(req);
      if (b.error) { accessLog(req, send(res, b.error === 'body_too_large' ? 413 : 400, { code: b.error })); return; }
      const q = String((b.json && b.json.query) || '').trim();
      if (!q) { accessLog(req, send(res, 200, { hits: [], mode: 'bm25-title4', total: 0 })); return; }
      const topk = Math.max(1, Math.min(Number((b.json && b.json.topk) || DEFAULT_TOPK) || DEFAULT_TOPK, MAX_TOPK));
      const snippetWidth = Math.max(40, Math.min(Number((b.json && b.json.snippetWidth) || 160) || 160, 500));
      const r = index.search(q, topk, snippetWidth);
      if (r.error) { accessLog(req, send(res, 503, { code: r.error, reason: r.reason })); return; }
      // 跨块去重：响应级 refs（同 key 保留 display 最长者），供 UI 渲染密钥行
      const seen = new Map();
      for (const h of r.hits) {
        for (const ref of h.refs) {
          const prev = seen.get(ref.key);
          if (!prev || ref.display.length > prev.display.length) seen.set(ref.key, ref);
        }
      }
      accessLog(req, send(res, 200, {
        hits: r.hits,
        refs: [...seen.values()],
        mode: 'bm25-title4',
        total: r.hits.length,
        index: r.index,
      }));
    }

    async function handleVaultStatus(req, res) {
      const r = vault.run(['status']);
      if (!r.ok) { accessLog(req, send(res, r.status, { code: r.code, reason: r.reason })); return; }
      accessLog(req, send(res, 200, {
        ok: true, window: r.json.window || null, map_exists: !!r.json.map_exists,
      }));
    }

    async function handleVaultReveal(req, res) {
      const b = await readBody(req);
      if (b.error) { accessLog(req, send(res, b.error === 'body_too_large' ? 413 : 400, { code: b.error })); return; }
      const entry = String((b.json && b.json.entry) || '');
      if (!ENTRY_NAME_RX.test(entry)) {
        accessLog(req, send(res, 400, { code: 'invalid_entry', reason: 'entry 必须形如 [REDACTED:<label>:<hash8>]' }));
        return;
      }
      const r = vault.run(['resolve', '--name', entry]);
      if (!r.ok) { accessLog(req, send(res, r.status, { code: r.code, reason: r.reason })); return; }
      // 全服务唯一明文响应体：禁止任何日志/缓存落它（本分支不 console、不落盘）
      res.writeHead(200, {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store',
      });
      res.end(JSON.stringify({
        name: r.json.name, label: r.json.label, sensitivity: r.json.sensitivity,
        masked: r.json.masked, value: r.json.value,
      }));
      if (cfg.log) console.log(new Date().toISOString() + ' ' + req.method + ' /vault/reveal 200');
    }

    async function handleVaultUnlock(req, res) {
      const b = await readBody(req);
      if (b.error) { accessLog(req, send(res, b.error === 'body_too_large' ? 413 : 400, { code: b.error })); return; }
      if (b.json.confirm !== true) {
        accessLog(req, send(res, 400, { code: 'confirm_required', reason: 'AFM 侧必须由用户点击确认后才发 unlock' }));
        return;
      }
      const ttl = Math.max(1, Math.min(Number(b.json.ttl || 600) || 600, 3600));
      const r = vault.run(['unlock', '--yes', '--ttl', String(ttl)], { json: false });
      if (!r.ok) { accessLog(req, send(res, r.status, { code: r.code, reason: r.reason })); return; }
      const s = vault.run(['status']);
      accessLog(req, send(res, 200, {
        ok: true, window: s.ok ? (s.json.window || null) : null, map_exists: s.ok ? !!s.json.map_exists : null,
      }));
    }

    async function handleVaultLock(req, res) {
      const r = vault.run(['lock'], { json: false });
      if (!r.ok) { accessLog(req, send(res, r.status, { code: r.code, reason: r.reason })); return; }
      const s = vault.run(['status']);
      accessLog(req, send(res, 200, {
        ok: true, window: s.ok ? (s.json.window || null) : null, map_exists: s.ok ? !!s.json.map_exists : null,
      }));
    }
  });

  server.cfg = cfg;
  return server;
}

// ---------- selftest ----------
function req(port, method, path, body, token) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : JSON.stringify(body);
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    if (payload) headers['Content-Length'] = Buffer.byteLength(payload);
    const r = http.request({ host: '127.0.0.1', port, method, path, headers }, (res) => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => {
        let json = null;
        try { json = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch (e) { /* ignore */ }
        resolve({ status: res.statusCode, json, headers: res.headers });
      });
    });
    r.on('error', reject);
    if (payload) r.write(payload);
    r.end();
  });
}

async function selftest() {
  const fs = require('fs');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'km-http-selftest-'));
  const idxRoot = path.join(tmp, 'idx');
  fs.mkdirSync(idxRoot, { recursive: true });
  fs.writeFileSync(path.join(idxRoot, 'accounts.md'), [
    '# 账号笔记',
    'API key 存放在 见保险库：OpenAI 主账号 [vault:llm-key:1a2b3c4d]。',
    '另外还有一个裸占位 [REDACTED:pw:99887766] 供测试。',
  ].join('\n'), 'utf8');
  fs.writeFileSync(path.join(idxRoot, 'daily.md'), [
    '# 日常',
    '今天研究了 BM25 检索与标题加权。',
  ].join('\n'), 'utf8');

  const cfg = loadConfig({
    KM_SERVICE_TOKEN: 'selftest-token-123',
    KM_SERVICE_PORT: '0',
    KM_SERVICE_LOG: '0',
    AWAM_INDEX_ROOT: idxRoot,
    AWAM_INDEX_CACHE: path.join(tmp, 'cache.json'),
  });
  let pass = 0;
  const ok = (cond, label) => {
    if (!cond) throw new Error('selftest 失败：' + label);
    pass++;
    console.log('  ok - ' + label);
  };

  const server = createApp(cfg);
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  try {
    let r = await req(port, 'GET', '/health');
    ok(r.status === 200 && r.json.ok === true && r.json.service === SERVICE_NAME, '/health 200 无鉴权');
    ok(r.json.index.configured === true, '/health 报告索引根已配置');
    ok(r.json.vault.configured === false, '/health 报告 vault 未配置');

    r = await req(port, 'POST', '/search', { query: 'BM25' });
    ok(r.status === 401, '/search 无 token → 401');
    r = await req(port, 'POST', '/search', { query: 'BM25' }, 'wrong-token');
    ok(r.status === 401, '/search 错 token → 401');

    r = await req(port, 'POST', '/search', { query: 'BM25' }, cfg.token);
    ok(r.status === 200 && r.json.hits.length === 1 && r.json.hits[0].path === 'daily.md', '/search 命中 daily.md');
    ok(r.headers['cache-control'] === 'no-store', '响应 Cache-Control: no-store');

    r = await req(port, 'POST', '/search', { query: '账号 API key' }, cfg.token);
    ok(r.status === 200 && r.json.hits.length >= 1, '/search 命中账号笔记');
    const refs = r.json.refs || [];
    const vaultRef = refs.find(x => x.kind === 'vault');
    const redRef = refs.find(x => x.kind === 'redacted');
    ok(vaultRef && vaultRef.key === '[REDACTED:llm-key:1a2b3c4d]' && vaultRef.display === 'OpenAI 主账号',
      '引用提取：见保险库 → vault ref（display=条目名）');
    ok(redRef && redRef.key === '[REDACTED:pw:99887766]' && redRef.display === 'pw',
      '引用提取：裸占位 → redacted ref（display=label）');

    r = await req(port, 'POST', '/search', { query: '  ' }, cfg.token);
    ok(r.status === 200 && r.json.hits.length === 0 && r.json.total === 0, '/search 空查询 → hits []');

    r = await req(port, 'POST', '/search', { query: 'BM25', topk: 999 }, cfg.token);
    ok(r.status === 200, '/search topk 夹取不报错');

    r = await req(port, 'POST', '/vault/status', {}, cfg.token);
    ok(r.status === 503 && r.json.code === 'vault_not_configured', '/vault/status 未配置 → 503');
    r = await req(port, 'POST', '/vault/reveal', { entry: '[REDACTED:llm-key:1a2b3c4d]' }, cfg.token);
    ok(r.status === 503 && r.json.code === 'vault_not_configured', '/vault/reveal 未配置 → 503');
    r = await req(port, 'POST', '/vault/unlock', { confirm: false }, cfg.token);
    ok(r.status === 400 && r.json.code === 'confirm_required', '/vault/unlock 缺 confirm → 400');
    r = await req(port, 'POST', '/vault/reveal', { entry: 'not a valid entry' }, cfg.token);
    ok(r.status === 400 && r.json.code === 'invalid_entry', '/vault/reveal 非法 entry → 400');

    r = await req(port, 'GET', '/nope', undefined, cfg.token);
    ok(r.status === 404, '未知路径 → 404');
  } finally {
    await new Promise(r => server.close(r));
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  console.log('http-server selftest: ' + pass + ' 项全部通过');
}

// ---------- CLI ----------
function main() {
  if (process.argv.includes('--selftest')) {
    selftest().then(() => process.exit(0)).catch((e) => {
      console.error(String(e && e.message || e));
      process.exit(1);
    });
    return;
  }
  const cfg = loadConfig();
  if (!cfg.token) {
    console.error('[http-server] 缺少 KM_SERVICE_TOKEN（AFM → 服务的鉴权 token）。fail-fast：未设置不启动。');
    process.exit(2);
  }
  const server = createApp(cfg);
  server.listen(cfg.port, cfg.host, () => {
    const addr = server.address();
    const isLoopback = addr.address === '127.0.0.1' || addr.address === '::1' || addr.address === '::';
    console.log('[http-server] ' + SERVICE_NAME + ' 监听 http://' + addr.address + ':' + addr.port);
    if (!isLoopback) {
      console.warn('[http-server] 警告：正在监听非回环地址，服务含本机取密代理，请确认网络暴露面。');
    }
    console.log('[http-server] vault 人机通道：' + (cfg.vault.configured ? '已配置' : '未配置（/vault/* 返回 503）'));
  });
  const shutdown = () => server.close(() => process.exit(0));
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

if (require.main === module) main();
module.exports = { SERVICE_NAME, loadConfig, extractRefs, createApp, selftest };
