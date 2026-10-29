'use strict';
/**
 * 索引根约定（RAG 侧）
 *
 * - 默认可索引输入 = 脱敏工作副本，来源按优先级：
 *     环境变量 KM_DATA_ROOT + KM_SOURCE_ID
 *     或 settings.yaml 的 data_root / source_id（KM_SETTINGS 可指定文件，默认用户目录）
 *   等价于 `%USERPROFILE%\.knowledge-manager-skills\settings.yaml` 的 data_root + 源 id。
 * - 真源（未脱敏）不得作为默认索引根：真源路径由 KM_RAW_ROOTS 声明（分号分隔），
 *   命中且未显式传 --allow-raw-source / allowRaw 时直接拒绝。
 * - 显式 --root 始终优先；未提供且环境变量缺失时报错退出，不做猜测。
 */
const path = require('path');
const fs = require('fs');
const os = require('os');

const DEFAULT_SOURCE_ID = 'logseq-main';

function normalizePath(p) {
  return String(p || '')
    .trim()
    .replace(/\\/g, '/')
    .replace(/\/+/g, '/')
    .replace(/\/$/, '')
    .toLowerCase();
}

/** 已知真源路径列表（来自 KM_RAW_ROOTS，分号分隔；默认空）。 */
function rawRoots() {
  return String(process.env.KM_RAW_ROOTS || '')
    .split(';')
    .map(s => normalizePath(s))
    .filter(Boolean);
}

/** settings.yaml 位置：KM_SETTINGS 优先，否则用户目录下的 knowledge-manager-skills。 */
function settingsPath() {
  if (process.env.KM_SETTINGS) return process.env.KM_SETTINGS;
  return path.join(os.homedir(), '.knowledge-manager-skills', 'settings.yaml');
}

function expandHome(p) {
  let s = String(p || '').trim();
  if (!s) return '';
  s = s.replace(/^~(?=$|[\\/])/, os.homedir());
  s = s.replace(/%USERPROFILE%/gi, os.homedir());
  return s;
}

/**
 * 极简 YAML 读取：只认顶层 `key: value`（去注释、去引号），够读 data_root / source_id。
 * 读不到或格式不认识时返回空对象，由调用方回退。
 */
function readSettings(file) {
  let text;
  try { text = fs.readFileSync(file || settingsPath(), 'utf8'); } catch (e) { return {}; }
  const out = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/^\s*#.*$/, '');
    const m = /^([A-Za-z0-9_.-]+)\s*:\s*(.+?)\s*$/.exec(line);
    if (!m) continue;
    let v = m[2].trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1).trim();
    if (v === '' || v === '~' || v === 'null') continue;
    out[m[1]] = v;
  }
  return out;
}

function isForbiddenRawSource(root) {
  const n = normalizePath(root);
  return rawRoots().some(f => n === f || n.startsWith(f + '/'));
}

/** 未配置时返回 null（调用方决定报错方式）。环境变量优先于 settings.yaml。 */
function resolveDefaultRoot() {
  const settings = readSettings();
  const dataRoot = process.env.KM_DATA_ROOT || process.env.DESENS_DATA_ROOT || expandHome(settings.data_root);
  if (!dataRoot) return null;
  const sid = process.env.KM_SOURCE_ID || settings.source_id || settings.default_source || DEFAULT_SOURCE_ID;
  return path.join(expandHome(dataRoot), String(sid)).split('\\').join('/');
}

/**
 * 纯校验版本：不退出进程，供 MCP server 等长驻进程使用。
 * @returns {{ok:boolean, root?:string, error?:string, reason?:string}}
 */
function validateIndexRoot(explicit, opts) {
  opts = opts || {};
  const allowRaw = opts.allowRaw === true || process.argv.includes('--allow-raw-source');
  const root = (explicit && String(explicit).trim()) ? String(explicit).trim() : resolveDefaultRoot();
  if (!root) {
    return {
      ok: false, reason: 'no_root',
      error: '未指定索引根：请传 --root，或设置 KM_DATA_ROOT（+ 可选 KM_SOURCE_ID）。' +
        '默认应指向脱敏工作副本，不要指向真源。',
    };
  }
  if (isForbiddenRawSource(root) && !allowRaw) {
    return {
      ok: false, reason: 'raw_source', root,
      error: '禁止使用真源作为索引/评测根（' + (opts.label || 'ROOT') + '）：' + root +
        '\n请改用脱敏工作副本（KM_DATA_ROOT/<源 id>）；确需读真源时显式加 --allow-raw-source。',
    };
  }
  return { ok: true, root };
}

/**
 * CLI 版本：失败即打印并 exit 2。
 * @param {string|undefined|null} explicit
 * @param {{ allowRaw?: boolean, label?: string }} [opts]
 * @returns {string}
 */
function resolveIndexRoot(explicit, opts) {
  const v = validateIndexRoot(explicit, opts);
  if (!v.ok) {
    console.error('[index-root] ' + v.error);
    process.exit(2);
  }
  return v.root;
}

module.exports = {
  DEFAULT_SOURCE_ID,
  normalizePath,
  rawRoots,
  settingsPath,
  readSettings,
  isForbiddenRawSource,
  resolveDefaultRoot,
  validateIndexRoot,
  resolveIndexRoot,
};
