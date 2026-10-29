'use strict';
/**
 * 密钥来源（写入闸共享密钥）：
 *   AWAM_SECRET（直接给值）→ AWAM_SECRET_CMD（外部命令，如 Bitwarden CLI）→ <root>/secret.key（首次生成；POSIX 下 0600，Windows 上 chmod 不生效，只依赖状态目录 ACL）
 * 签发端（companion）与校验端（mcp-server）必须走同一套解析，否则长度/空白处理不一致会让闸失效。
 * 命令继承当前进程环境，因此 bw 的 BW_SESSION 直接透传，不需要额外参数。
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const MIN_LEN = 16;

function checkSecret(raw, source) {
  const s = String(raw == null ? '' : raw).trim();
  if (!s) throw new Error(source + ' 输出为空');
  if (s.length < MIN_LEN) throw new Error(source + ' 至少 ' + MIN_LEN + ' 字符（当前 ' + s.length + '）');
  return s;
}

const ANSI = /\u001b\[[0-9;?]*[A-Za-z]/g;

// 未解锁的 bw 会先弹交互式密码提示，stdin 已置 ignore，故它会立即退出而不是挂住；
// 但 stderr 里混着 ANSI 控制序列与提示语，需要清掉并翻译成可操作的说明。
const HINTS = [
  [/master password|vault is locked/i, 'vault 未解锁：先 `bw unlock --raw` 解锁，并把 BW_SESSION 放进 server 进程的环境'],
  [/not found/i, 'Bitwarden 里没有该条目：检查 AWAM_SECRET_CMD 中的条目名'],
];

function classify(msg) {
  for (const [re, hint] of HINTS) if (re.test(msg)) return '（' + hint + '）';
  return '';
}

function runCommand(cmd) {
  let out;
  try {
    out = execFileSync(cmd, { shell: true, encoding: 'utf8', timeout: 15000, stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) {
    const stderr = String((e && e.stderr) || '').replace(ANSI, '').trim();
    const first = stderr.split(/\r?\n/).map(s => s.trim()).filter(Boolean)[0] || (e && e.message) || 'unknown error';
    const status = e && e.status != null ? e.status : '?';
    throw new Error('AWAM_SECRET_CMD 执行失败（退出码 ' + status + '）：' + first + classify(first));
  }
  return out;
}

function readFile(abs, create) {
  if (fs.existsSync(abs)) {
    const s = fs.readFileSync(abs, 'utf8');
    try {
      return checkSecret(s, 'secret.key');
    } catch (e) {
      throw new Error(e.message + '，请删除 ' + abs + ' 后重启，或改用 AWAM_SECRET');
    }
  }
  if (!create) throw new Error('secret_missing：未配置 AWAM_SECRET / AWAM_SECRET_CMD，且 ' + abs + ' 不存在');
  const secret = crypto.randomBytes(32).toString('hex');
  const fd = fs.openSync(abs, 'wx', 0o600);
  try { fs.writeFileSync(fd, secret); } finally { fs.closeSync(fd); }
  return secret;
}

function resolveSecret(opts) {
  const o = opts || {};
  const env = o.env || process.env;
  if (env.AWAM_SECRET != null && env.AWAM_SECRET !== '') {
    return { secret: checkSecret(env.AWAM_SECRET, 'AWAM_SECRET'), source: 'env' };
  }
  if (env.AWAM_SECRET_CMD) {
    return { secret: checkSecret(runCommand(env.AWAM_SECRET_CMD), 'AWAM_SECRET_CMD'), source: 'cmd' };
  }
  const root = o.root || env.AWAM_STATE_DIR || env.SPIKE_ROOT || path.join(__dirname, 'data');
  const abs = path.join(root, 'secret.key');
  return { secret: readFile(abs, o.create !== false), source: 'file', path: abs };
}

module.exports = { resolveSecret, MIN_LEN };
