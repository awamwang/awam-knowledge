'use strict';
/**
 * 密钥来源自检（零依赖，全在系统 temp 下，跑完删除）
 *
 *   node code/secret-broker-selftest.js
 *
 * 覆盖：优先级、外部命令（成功/带空白/过短/失败）、会话环境透传（模拟 bw 的 BW_SESSION）、
 * 本地 secret.key（复用/过短/缺失），以及签发端 companion 与校验端共用同一解析。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { resolveSecret } = require('./secret-provider');
const companion = require('./companion');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'awam-secret-selftest-'));
const checks = [];
const add = (name, pass, detail) => checks.push({ name, pass: !!pass, detail: detail === undefined ? null : detail });

const ENV_KEYS = ['AWAM_SECRET', 'AWAM_SECRET_CMD', 'AWAM_STATE_DIR', 'SPIKE_ROOT', 'BW_SESSION'];
function withEnv(patch, fn) {
  const saved = {};
  for (const k of ENV_KEYS) { saved[k] = process.env[k]; delete process.env[k]; }
  for (const k of Object.keys(patch)) process.env[k] = patch[k];
  try { return fn(); } finally {
    for (const k of ENV_KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  }
}
function throws(fn) { try { fn(); return null; } catch (e) { return e.message; } }

// 假 bw：真实场景由 BW_SESSION 携带会话，这里只验证"环境透传 + 输出处理 + 失败报错"
const HELPER = path.join(TMP, 'fake-bw.js');
fs.writeFileSync(HELPER, [
  "'use strict';",
  'const mode = process.argv[2];',
  "const s = 'k'.repeat(32);",
  "if (mode === 'ok') process.stdout.write(s);",
  "else if (mode === 'pad') process.stdout.write('  ' + s + '\\r\\n');",
  "else if (mode === 'short') process.stdout.write('abc');",
  "else if (mode === 'session') process.stdout.write(process.env.BW_SESSION || '');",
  "else if (mode === 'locked') { process.stderr.write('Vault is locked.\\n'); process.exit(1); }",
  "else if (mode === 'mark') { require('fs').writeFileSync(process.argv[3], 'x'); process.stdout.write(s); }",
  "else if (mode === 'unlocked-prompt') { process.stderr.write('? Master password: [input is hidden] \\u001b[37D\\u001b[37C\\n'); process.exit(1); }",
  "else if (mode === 'notfound') { process.stderr.write('Not found.\\n'); process.exit(1); }",
].join('\n'));
const cmd = mode => 'node ' + HELPER + ' ' + mode;

(function run() {
  // ---------- 优先级 ----------
  const marker = path.join(TMP, 'cmd-was-called');
  const envFirst = withEnv({ AWAM_SECRET: 'a'.repeat(32), AWAM_SECRET_CMD: cmd('mark ' + marker) },
    () => resolveSecret({ root: TMP }));
  add('A1 AWAM_SECRET 优先且不执行命令', envFirst.source === 'env' && !fs.existsSync(marker), envFirst.source);

  add('A2 AWAM_SECRET 过短报错', /至少 16 字符/.test(throws(() => withEnv({ AWAM_SECRET: 'short' }, () => resolveSecret({ root: TMP }))) || ''));
  add('A3 AWAM_SECRET 全空白报错', /为空/.test(throws(() => withEnv({ AWAM_SECRET: '   ' }, () => resolveSecret({ root: TMP }))) || ''));

  // ---------- 外部命令 ----------
  const ok = withEnv({ AWAM_SECRET_CMD: cmd('ok') }, () => resolveSecret({ root: TMP }));
  add('B1 命令输出可用', ok.source === 'cmd' && ok.secret === 'k'.repeat(32), ok.source);

  const padded = withEnv({ AWAM_SECRET_CMD: cmd('pad') }, () => resolveSecret({ root: TMP }));
  add('B2 命令输出去首尾空白', padded.secret === 'k'.repeat(32), padded.secret.length);

  const shortMsg = throws(() => withEnv({ AWAM_SECRET_CMD: cmd('short') }, () => resolveSecret({ root: TMP }))) || '';
  add('B3 命令输出过短报错', /AWAM_SECRET_CMD 至少 16 字符/.test(shortMsg), shortMsg);

  const lockedMsg = throws(() => withEnv({ AWAM_SECRET_CMD: cmd('locked') }, () => resolveSecret({ root: TMP }))) || '';
  add('B4 命令失败带退出码与 stderr 首行', /退出码 1/.test(lockedMsg) && /Vault is locked\./.test(lockedMsg), lockedMsg);

  const promptMsg = throws(() => withEnv({ AWAM_SECRET_CMD: cmd('unlocked-prompt') }, () => resolveSecret({ root: TMP }))) || '';
  add('B7 未解锁提示去掉 ANSI 并给出解锁指引', !/\u001b/.test(promptMsg) && /vault 未解锁/.test(promptMsg), promptMsg);

  const notFoundMsg = throws(() => withEnv({ AWAM_SECRET_CMD: cmd('notfound') }, () => resolveSecret({ root: TMP }))) || '';
  add('B8 条目不存在给出条目名指引', /Bitwarden 里没有该条目/.test(notFoundMsg), notFoundMsg);

  const sess = withEnv({ AWAM_SECRET_CMD: cmd('session'), BW_SESSION: 'session-from-env-0123456789' }, () => resolveSecret({ root: TMP }));
  add('B5 会话经环境透传给子进程（模拟 bw unlock 后的 BW_SESSION）', sess.secret === 'session-from-env-0123456789', sess.secret);

  const noSession = throws(() => withEnv({ AWAM_SECRET_CMD: cmd('session') }, () => resolveSecret({ root: TMP }))) || '';
  add('B6 未解锁（无 BW_SESSION）时明确报空', /AWAM_SECRET_CMD 输出为空/.test(noSession), noSession);

  // ---------- 本地文件 ----------
  const dir = path.join(TMP, 'state');
  fs.mkdirSync(dir, { recursive: true });
  const generated = withEnv({}, () => resolveSecret({ root: dir }));
  const shown = fs.readFileSync(path.join(dir, 'secret.key'), 'utf8');
  add('C1 无来源时生成 secret.key', generated.source === 'file' && /^[0-9a-f]{64}$/.test(shown), generated.source);
  add('C2 生成的密钥可复用', withEnv({}, () => resolveSecret({ root: dir })).secret === generated.secret);
  if (process.platform !== 'win32') {
    add('C3 secret.key 权限 0600', (fs.statSync(path.join(dir, 'secret.key')).mode & 0o777) === 0o600);
  }

  const dir2 = path.join(TMP, 'short-key');
  fs.mkdirSync(dir2, { recursive: true });
  fs.writeFileSync(path.join(dir2, 'secret.key'), 'tiny');
  const shortFile = throws(() => withEnv({}, () => resolveSecret({ root: dir2 }))) || '';
  add('C4 过短 secret.key 报错并提示处理', /至少 16 字符/.test(shortFile) && /请删除/.test(shortFile), shortFile);

  const dir3 = path.join(TMP, 'missing');
  fs.mkdirSync(dir3, { recursive: true });
  const missing = throws(() => withEnv({}, () => resolveSecret({ root: dir3, create: false }))) || '';
  add('C5 禁止生成时报 secret_missing', /secret_missing/.test(missing), missing);
  add('C6 禁止生成时不落文件', !fs.existsSync(path.join(dir3, 'secret.key')));

  // ---------- 签发端与校验端同源 ----------
  const viaCmd = withEnv({ AWAM_SECRET_CMD: cmd('ok') }, () => ({
    provider: resolveSecret({ root: TMP }).secret,
    companion: companion.readSecret(TMP),
  }));
  add('D1 companion 与 secret-provider 取到同一密钥', viaCmd.provider === viaCmd.companion && viaCmd.companion === 'k'.repeat(32));

  const dir4 = path.join(TMP, 'shared-state');
  fs.mkdirSync(dir4, { recursive: true });
  const shared = withEnv({ AWAM_STATE_DIR: dir4 }, () => ({
    provider: resolveSecret({}).secret,
    companion: companion.readSecret(),
  }));
  add('D2 未显式传根时两侧都落到 AWAM_STATE_DIR', shared.provider === shared.companion && shared.provider.length === 64);
})();

const failed = checks.filter(c => !c.pass);
console.log(JSON.stringify({ selftest: true, checks: checks.length, pass: failed.length === 0, failed: failed.map(f => f.name), detail: failed, tmp: TMP }, null, 2));
try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* ignore */ }
process.exit(failed.length === 0 ? 0 : 1);
