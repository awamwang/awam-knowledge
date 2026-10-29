'use strict';
/**
 * T10 Spike · companion 侧：模拟"用户在受信通道确认后签发 token"
 * 真实方案中此动作发生在本地 companion（确认页/CLI），AI 侧拿不到 secret。
 * 此文件仅供 spike 复现，非生产实现。
 *
 * secret 来源与 mcp-server 一致：AWAM_SECRET → AWAM_SECRET_CMD → <root>/secret.key。
 */
const path = require('path');
const crypto = require('crypto');
const { resolveSecret } = require('./secret-provider');

function readSecret(root) {
  return resolveSecret({ root: root || process.env.AWAM_STATE_DIR || process.env.SPIKE_ROOT || path.join(__dirname, 'data') }).secret;
}

function issueToken(proposal_id, baseline_hash, issued_at, root) {
  const secret = readSecret(root);
  const payload = { proposal_id: proposal_id, issued_at: issued_at || Date.now(), baseline_hash: baseline_hash };
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const sig = crypto.createHmac('sha256', secret).update(body).digest('base64url');
  return body + '.' + sig;
}
module.exports = { issueToken, readSecret };
