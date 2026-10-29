#!/usr/bin/env node
'use strict';
/**
 * 原型真渲染与可访问性检查（零依赖：Node ≥22 的全局 WebSocket + 本机 Chrome/Edge）。
 *
 *   node code/ui-render-check.js [--target design/05-prototype.html] [--shots <dir>] [--keep]
 *
 * 做六件事，全部基于真实浏览器：
 *   A. 渲染：1280×900 / 390×844 / 200% 缩放（640×450 布局视口 + 2× 物理像素）三视口下有无横向溢出、关键卡片是否真的可见
 *   B. 对比度：按 WCAG 2.1 AA（4.5:1，大字号 3:1）逐元素算，排除禁用态
 *   C. 键盘焦点：真按 Tab，检查焦点元素是否有可见轮廓
 *   D. 语义：landmark / 可访问名称 / alertdialog / 标题层级
 *   E. 脱敏：页面正文里是否出现形似密钥的明文；状态按钮切换后卡片状态是否正确
 *   F. 纯键盘：只用 Tab 与 Enter 走完「确认写入 → 撤销」，不派发鼠标事件
 *
 * 不进 npm selftest（依赖本机浏览器）；--shots 给出目录时保存各状态截图。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}
const HAS = n => process.argv.includes('--' + n);

const TARGET = path.resolve(arg('target', path.join(__dirname, '..', 'design', '05-prototype.html')));
const SHOTS = arg('shots', '');
const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
].filter(Boolean);

function findChrome() {
  for (const p of CHROME_CANDIDATES) if (fs.existsSync(p)) return p;
  return null;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function httpJson(url) {
  const res = await fetch(url);
  return res.json();
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const waiters = new Map();
    ws.addEventListener('message', ev => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.id && waiters.has(msg.id)) { waiters.get(msg.id)(msg); waiters.delete(msg.id); }
    });
    ws.addEventListener('error', e => reject(new Error('ws error')));
    ws.addEventListener('open', () => resolve({
      send(method, params) {
        return new Promise((res, rej) => {
          const n = ++id;
          waiters.set(n, m => (m.error ? rej(new Error(method + ': ' + JSON.stringify(m.error))) : res(m.result)));
          ws.send(JSON.stringify({ id: n, method, params: params || {} }));
          setTimeout(() => { if (waiters.has(n)) { waiters.delete(n); rej(new Error('timeout ' + method)); } }, 20000);
        });
      },
      close() { try { ws.close(); } catch (e) { /* ignore */ } },
    }));
  });
}

// ---------- 页面内检查（注入执行） ----------
const PAGE_CHECK = `
(() => {
  const parse = c => { const m = String(c).match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(',').map(Number); return { r:p[0], g:p[1], b:p[2], a: p.length > 3 ? p[3] : 1 }; };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
    return 0.2126*f(c.r) + 0.7152*f(c.g) + 0.0722*f(c.b); };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05); };
  const label = el => el.tagName.toLowerCase() + (el.id ? '#'+el.id : '') + (el.className && typeof el.className === 'string' ? '.'+el.className.trim().split(/\\s+/).join('.') : '');
  // 背景要按 alpha 逐层合成，否则半透明底（如 rgba(210,153,34,.12)）会被当成实色，算出不存在的低对比度
  const bgOf = el => {
    const layers = []; let n = el;
    while (n && n.nodeType === 1) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.001) { layers.push(c); if (c.a >= 0.999) break; }
      n = n.parentElement;
    }
    let out = layers.length && layers[layers.length - 1].a >= 0.999
      ? layers[layers.length - 1] : { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 2; i >= 0; i--) {
      const t = layers[i];
      out = { r: t.r*t.a + out.r*(1-t.a), g: t.g*t.a + out.g*(1-t.a), b: t.b*t.a + out.b*(1-t.a), a: 1 };
    }
    return out;
  };
  const visible = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden' && Number(getComputedStyle(el).opacity) > 0.05;
  const hasOwnText = el => Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim().length > 1);

  const contrast = { checked: 0, violations: [], worst: [] };
  for (const el of document.querySelectorAll('body *')) {
    if (!hasOwnText(el) || !visible(el)) continue;
    if (el.closest('[disabled], :disabled, .hide')) continue;
    const st = getComputedStyle(el);
    const fg = parse(st.color);
    if (!fg || fg.a < 0.05) continue;
    const bg = bgOf(el);
    const size = parseFloat(st.fontSize);
    const weight = st.fontWeight === 'bold' ? 700 : Number(st.fontWeight) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3 : 4.5;
    const r = ratio(fg, bg);
    contrast.checked++;
    const rec = { el: label(el), text: el.textContent.trim().slice(0, 28), size, weight, ratio: Math.round(r*100)/100, need };
    if (r < need) contrast.violations.push(rec);
    contrast.worst.push(rec);
  }
  contrast.worst.sort((a,b) => a.ratio - b.ratio);
  contrast.worst = contrast.worst.slice(0, 6);

  const aria = [];
  const push = (name, pass, detail) => aria.push({ name, pass, detail: detail === undefined ? null : detail });
  push('html lang 已声明', !!document.documentElement.lang, document.documentElement.lang);
  push('存在 main landmark', document.querySelectorAll('main,[role=main]').length === 1);
  const unnamed = Array.from(document.querySelectorAll('button')).filter(b => visible(b) && !(b.getAttribute('aria-label') || b.textContent.trim()));
  push('按钮都有可访问名称', unnamed.length === 0, unnamed.map(label));
  const dlg = document.querySelector('[role=alertdialog]');
  push('写入确认卡为 alertdialog 且带 aria-live', !!dlg && dlg.getAttribute('aria-live') === 'assertive', dlg ? dlg.getAttribute('aria-live') : null);
  const h = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).map(e => Number(e.tagName[1]));
  push('标题层级不跳级', h.every((v, i) => i === 0 || v - h[i-1] <= 1), h);
  push('页面有 h1', h[0] === 1, h[0] || null);
  const imgs = Array.from(document.querySelectorAll('img')).filter(i => !i.hasAttribute('alt'));
  push('图片都有 alt', imgs.length === 0, imgs.length);
  const smallTargets = Array.from(document.querySelectorAll('button')).filter(b => {
    if (!visible(b)) return false; const r = b.getBoundingClientRect(); return r.width < 24 || r.height < 24; });
  push('按钮点按区不小于 24×24', smallTargets.length === 0, smallTargets.map(label));

  const secretRe = [ /sk-[A-Za-z0-9_\\-]{12,}/g, /\\b[A-Za-z0-9_\\-]{32,}\\b/g, /\\b[0-9a-f]{32,}\\b/g, /-----BEGIN [A-Z ]+PRIVATE KEY-----/g ];
  const text = document.body.innerText;
  const secrets = [];
  for (const re of secretRe) { const m = text.match(re); if (m) secrets.push({ re: String(re), hits: m.slice(0, 3) }); }

  const cards = Array.from(document.querySelectorAll('[data-testid]')).map(e => ({ id: e.getAttribute('data-testid'), visible: visible(e) }));

  return { contrast, aria, secrets, cards,
    overflow: { scrollW: document.documentElement.scrollWidth, clientW: document.documentElement.clientWidth } };
})()
`;

async function main() {
  const chrome = findChrome();
  if (!chrome) throw new Error('未找到 Chrome/Edge，可用 CHROME_PATH 指定');

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'awam-ui-profile-'));
  const port = 9300 + Math.floor(Math.random() * 200);
  const url = 'file:///' + TARGET.split('\\').join('/');
  const child = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--hide-scrollbars', '--window-size=1280,900',
    '--user-data-dir=' + profile, '--remote-debugging-port=' + port, url,
  ], { stdio: 'ignore' });

  const report = { target: TARGET, chrome, viewports: {}, contrast: null, focus: [], aria: [], secrets: [], states: {}, shots: [], errors: [] };
  let cdp;
  try {
    let page = null;
    for (let i = 0; i < 60; i++) {
      try {
        const list = await httpJson('http://127.0.0.1:' + port + '/json/list');
        page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
        if (page) break;
      } catch (e) { /* not ready */ }
      await sleep(250);
    }
    if (!page) throw new Error('CDP 未就绪');

    cdp = await connect(page.webSocketDebuggerUrl);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    for (let i = 0; i < 40; i++) {
      const r = await cdp.send('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true });
      if (r.result.value === 'complete') break;
      await sleep(100);
    }

    // A/B：视口渲染与对比度（zoom200 = 1280×900 窗口 200% 缩放：布局视口减半、物理像素 2×）
    for (const vp of [
      { name: 'desktop', width: 1280, height: 900 },
      { name: 'mobile', width: 390, height: 844 },
      { name: 'zoom200', width: 640, height: 450, dsf: 2 },
    ]) {
      await cdp.send('Emulation.setDeviceMetricsOverride', { width: vp.width, height: vp.height, deviceScaleFactor: vp.dsf || 1, mobile: vp.name === 'mobile' });
      await sleep(150);
      const r = await cdp.send('Runtime.evaluate', { expression: PAGE_CHECK, returnByValue: true, awaitPromise: false });
      const v = r.result.value;
      report.viewports[vp.name] = { width: vp.width, height: vp.height, overflowX: v.overflow.scrollW - v.overflow.clientW, cards: v.cards };
      if (vp.name === 'desktop') {
        report.contrast = v.contrast;
        report.aria = v.aria;
        report.secrets = v.secrets;
      }
      if (SHOTS) {
        const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
        const f = path.join(SHOTS, 'ui-' + vp.name + '-default.png');
        fs.mkdirSync(SHOTS, { recursive: true });
        fs.writeFileSync(f, Buffer.from(shot.data, 'base64'));
        report.shots.push(f);
      }
    }
    await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });

    // C：键盘焦点
    await cdp.send('Runtime.evaluate', { expression: 'document.activeElement && document.activeElement.blur(); window.scrollTo(0,0);', returnByValue: true });
    for (let i = 0; i < 10; i++) {
      await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
      const r = await cdp.send('Runtime.evaluate', { expression: `(() => { const el = document.activeElement; if (!el || el === document.body) return null;
        const st = getComputedStyle(el);
        return { tag: el.tagName.toLowerCase(), id: el.id || null, text: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 20),
          outline: st.outlineStyle !== 'none' && parseFloat(st.outlineWidth) > 0, outlineW: st.outlineWidth }; })()`, returnByValue: true });
      if (r.result.value) report.focus.push(r.result.value);
    }

    // F：纯键盘走完写入（只按 Tab / Enter，不派发鼠标事件）
    const ENTER_TEXT = String.fromCharCode(13);
    const pressKey = async (key, code, vk, text) => {
      const base = { key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk };
      // Enter 必须带 text 才产生完整按键事件；rawKeyDown 不会激活按钮
      await cdp.send('Input.dispatchKeyEvent', Object.assign({ type: text ? 'keyDown' : 'rawKeyDown' }, base,
        text ? { text, unmodifiedText: text } : {}));
      await cdp.send('Input.dispatchKeyEvent', Object.assign({ type: 'keyUp' }, base));
    };
    const focusId = async () => (await cdp.send('Runtime.evaluate', {
      expression: 'document.activeElement ? document.activeElement.id : ""', returnByValue: true })).result.value;
    const tabTo = async id => {
      for (let i = 0; i < 60; i++) {
        if (await focusId() === id) return true;
        await pressKey('Tab', 'Tab', 9);
        await sleep(20);
      }
      return false;
    };
    const badgeText = async () => (await cdp.send('Runtime.evaluate', {
      expression: `(() => { const e = document.getElementById('write-badge'); return e ? e.textContent.trim() : null; })()`,
      returnByValue: true })).result.value;

    await cdp.send('Runtime.evaluate', { expression: 'document.activeElement && document.activeElement.blur(); window.scrollTo(0,0);', returnByValue: true });
    report.keyboard = { reachedConfirm: false, badgeAfterEnter: null, reachedUndo: false, badgeAfterUndo: null };
    report.keyboard.reachedConfirm = await tabTo('btn-confirm');
    await pressKey('Enter', 'Enter', 13, ENTER_TEXT); await sleep(80);
    report.keyboard.badgeAfterEnter = await badgeText();
    report.keyboard.reachedUndo = await tabTo('btn-undo');
    await pressKey('Enter', 'Enter', 13, ENTER_TEXT); await sleep(80);
    report.keyboard.badgeAfterUndo = await badgeText();

    // 回到初始状态，后面的鼠标状态切换从头跑
    await cdp.send('Page.reload');
    for (let i = 0; i < 40; i++) {
      const r = await cdp.send('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true });
      if (r.result.value === 'complete') break;
      await sleep(100);
    }
    await sleep(100);

    // E：状态切换
    const click = id => cdp.send('Runtime.evaluate', { expression: `document.getElementById(${JSON.stringify(id)}).click()`, returnByValue: true });
    const stateOf = (testid) => cdp.send('Runtime.evaluate', { expression: `(() => { const e = document.querySelector('[data-testid="${testid}"]'); if (!e) return null;
      const vis = e.getClientRects().length > 0 && !e.closest('.hide') && getComputedStyle(e).visibility !== 'hidden';
      return { visible: vis, text: vis ? (e.textContent || '').trim().slice(0, 40) : null, disabled: !!e.disabled,
        opacity: vis ? Number(getComputedStyle(e).opacity) : null }; })()`, returnByValue: true });

    await click('btn-noresult'); await sleep(80);
    report.states.noresult = (await stateOf('result-none')).result.value;
    await click('btn-noresult'); await sleep(80);

    await click('btn-blocked'); await sleep(80);
    report.states.blocked = { scanBlock: (await stateOf('scan-block')).result.value, confirm: (await stateOf('btn-confirm')).result.value };
    if (SHOTS) {
      const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
      const f = path.join(SHOTS, 'ui-desktop-blocked.png');
      fs.writeFileSync(f, Buffer.from(shot.data, 'base64'));
      report.shots.push(f);
    }
    await click('btn-blocked'); await sleep(80);

    await click('btn-autofail'); await sleep(80);
    report.states.autofail = (await stateOf('fill-status')).result.value;

    await click('btn-confirm'); await sleep(80);
    report.states.confirmed = { badge: (await stateOf('write-badge')).result.value, undo: (await stateOf('btn-undo')).result.value };
    if (SHOTS) {
      const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
      const f = path.join(SHOTS, 'ui-desktop-confirmed.png');
      fs.writeFileSync(f, Buffer.from(shot.data, 'base64'));
      report.shots.push(f);
    }
  } catch (e) {
    report.errors.push(e.message);
  } finally {
    if (cdp) cdp.close();
    try { child.kill(); } catch (e) { /* ignore */ }
    await sleep(300);
    if (!HAS('keep')) { try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ } }
  }

  const problems = [];
  for (const [name, vp] of Object.entries(report.viewports)) if (vp.overflowX > 1) problems.push('viewport.' + name + ' 横向溢出 ' + vp.overflowX + 'px');
  if (report.contrast) for (const v of report.contrast.violations) problems.push('对比度 ' + v.el + ' ' + v.ratio + ' < ' + v.need);
  for (const f of report.focus) if (!f.outline) problems.push('焦点无可见轮廓: ' + f.tag + (f.id ? '#' + f.id : ''));
  for (const a of report.aria) if (!a.pass) problems.push('语义: ' + a.name + ' ' + JSON.stringify(a.detail));
  for (const s of report.secrets) problems.push('疑似密钥明文: ' + s.re);
  if (!report.states.blocked || !report.states.blocked.confirm || !report.states.blocked.confirm.disabled) problems.push('阻断态未禁用「确认写入」');
  else if (report.states.blocked.confirm.opacity === 1) problems.push('禁用按钮外观与可点态无差别');
  if (!report.keyboard || !report.keyboard.reachedConfirm) problems.push('键盘无法定位「确认写入」');
  else if (!/已确认/.test(report.keyboard.badgeAfterEnter || '')) problems.push('Enter 未激活「确认写入」: ' + report.keyboard.badgeAfterEnter);
  if (!report.keyboard || !report.keyboard.reachedUndo) problems.push('键盘无法定位「撤销」');
  else if (report.keyboard.badgeAfterUndo !== '已撤销') problems.push('Enter 未激活「撤销」: ' + report.keyboard.badgeAfterUndo);
  if (!report.states.noresult || !report.states.noresult.visible) problems.push('无结果态未显示');
  if (!report.states.confirmed || !report.states.confirmed.undo || !report.states.confirmed.undo.visible) problems.push('确认态未出现撤销按钮');

  report.problems = problems;
  report.ok = problems.length === 0 && report.errors.length === 0;
  console.log(JSON.stringify(report, null, 2));
  process.exit(report.ok ? 0 : 1);
}

main().catch(e => { console.log(JSON.stringify({ ok: false, error: e.message }, null, 2)); process.exit(1); });
