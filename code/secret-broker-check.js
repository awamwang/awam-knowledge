#!/usr/bin/env node
'use strict';
/**
 * 运维自检：确认当前环境下的密钥来源可用，且签发端（companion）与校验端（secret-provider）取到同一把密钥。
 * 不打印密钥本身，也不打印会话；只输出来源、长度与结论。
 *
 *   BW_SESSION=$(bw unlock --raw) AWAM_SECRET_CMD='bw get password <vault-item>' node code/secret-broker-check.js
 *   AWAM_SECRET=...           node code/secret-broker-check.js
 *   node code/secret-broker-check.js          # 回退到 <AWAM_STATE_DIR>/secret.key
 */
const crypto = require('crypto');
const path = require('path');
const { resolveSecret } = require('./secret-provider');
const companion = require('./companion');

function main() {
  const out = { ok: false, source: null, length: 0, same_secret_as_signer: false, roundtrip: false };
  try {
    const provider = resolveSecret({ create: false });
    const root = provider.path ? path.dirname(provider.path) : undefined;
    out.source = provider.source;
    out.length = provider.secret.length;

    out.same_secret_as_signer = companion.readSecret(root) === provider.secret;

    const token = companion.issueToken('check-proposal', 'check-hash', 1, root);
    const [body, sig] = token.split('.');
    const expect = crypto.createHmac('sha256', provider.secret).update(body).digest('base64url');
    out.roundtrip = sig === expect;

    out.ok = out.same_secret_as_signer && out.roundtrip;
  } catch (e) {
    out.error = e.message;
  }
  console.log(JSON.stringify(out, null, 2));
  process.exit(out.ok ? 0 : 1);
}

main();
