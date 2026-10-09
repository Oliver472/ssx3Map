// Browser smoke test for the map editor (needs Node with the `playwright` package).
//   python3 -m ssx3map editor some.iso --port 8799 --no-browser &
//   EDITOR_URL=http://127.0.0.1:8799/ OUT_DIR=/tmp/shots [THREE_DIR=/path/to/three@0.160.0] node tests/browser/editor_smoke.mjs
// THREE_DIR serves three.js from a local copy instead of the CDN (offline runs).
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const url = process.env.EDITOR_URL || 'http://127.0.0.1:8799/';
const out = process.env.OUT_DIR || '.';
const threeDir = process.env.THREE_DIR;
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined,
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 850 } });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
if (threeDir) {
  await page.route('https://cdn.jsdelivr.net/npm/three@0.160.0/**', (route) => {
    const rel = new URL(route.request().url()).pathname.replace('/npm/three@0.160.0/', '');
    route.fulfill({ path: path.join(threeDir, rel), contentType: 'text/javascript' });
  });
}
const check = (cond, msg) => { if (!cond) { console.error('FAIL:', msg); process.exitCode = 1; } else console.log('ok:', msg); };
const logText = () => page.$eval('#log', (e) => e.innerText);

await page.goto(url);
await page.waitForFunction(() => /plátov/.test(document.querySelector('#courseInfo').textContent), null, { timeout: 30000 });
await page.waitForTimeout(1500);
await page.screenshot({ path: path.join(out, 'editor-1-loaded.png') });
const meshes = await page.evaluate(() => window.ssxEditor.state.terrain.children.length);
check(meshes > 0, `terrain meshes built (${meshes})`);

// Terrain tool: click the middle of the view.
await page.click('button[data-tool=terrain]');
await page.fill('#height', '3');
const box = await page.$eval('#view', (e) => { const r = e.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
await page.mouse.move(box.x, box.y);
await page.waitForTimeout(300);
await page.mouse.click(box.x, box.y);
await page.waitForFunction(() => /plátov|cannot|terén|terrain/i.test(document.querySelector('#log').innerText), null, { timeout: 30000 });
await page.waitForTimeout(800);
await page.screenshot({ path: path.join(out, 'editor-2-terrain.png') });
let text = await logText();
check(/plátov/.test(text), `terrain edit applied: ${text.split('\n')[0]}`);

// Brush: drag a stroke across the middle of the view (left button paints in this mode).
await page.click('button[data-tool=brush]');
await page.selectOption('#brushMode', 'raise');
await page.fill('#brushHeight', '1.5');
const onLine = (metres) => page.evaluate((d) => {
  const s = window.ssxEditor.state;
  const { p } = s.line.at(d * 100);
  const o = s.origin;
  const v = window.ssxEditor.project((p[0] - o[0]) / 100, (p[2] - o[2]) / 100, -(p[1] - o[1]) / 100);
  const r = document.querySelector('#view').getBoundingClientRect();
  return { x: r.left + (v.x + 1) / 2 * r.width, y: r.top + (1 - v.y) / 2 * r.height };
}, metres);
const a = await onLine(15), b = await onLine(35);
await page.mouse.move(a.x, a.y);
await page.mouse.down();
for (let k = 1; k <= 12; k++) await page.mouse.move(a.x + (b.x - a.x) * k / 12, a.y + (b.y - a.y) * k / 12, { steps: 2 });
await page.mouse.up();
await page.waitForFunction(() => /štetec: zdvihnutie|brush|stroke/.test(document.querySelector('#log').innerText), null, { timeout: 30000 });
text = await logText();
check(/štetec: zdvihnutie, \d+ bodov: \d+ plátov/.test(text), `brush stroke: ${text.split('\n')[0]}`);
await page.click('#undo');
await page.waitForFunction(() => (document.querySelector('#log').innerText.match(/späť/g) || []).length >= 1, null, { timeout: 30000 });

// Undo.
await page.click('#undo');
await page.waitForFunction(() => /späť/.test(document.querySelector('#log').innerText), null, { timeout: 30000 });
check(/späť/.test(await logText()), 'undo');

// Objects: select the first visible object by projecting it to the screen, then raise it.
await page.click('button[data-tool=objects]');
const target = await page.evaluate(() => {
  const s = window.ssxEditor.state;
  const mesh = s.objects; if (!mesh) return null;
  const m = new mesh.matrix.constructor();
  mesh.getMatrixAt(0, m);
  const v = mesh.position.clone().setFromMatrixPosition(m);
  return { x: v.x, y: v.y, z: v.z };
});
check(target !== null, 'objects shown');
if (target) {
  const screen = await page.evaluate(({ x, y, z }) => {
    const r = document.querySelector('#view').getBoundingClientRect();
    const v = window.ssxEditor.project(x, y, z);
    return { x: r.left + (v.x + 1) / 2 * r.width, y: r.top + (1 - v.y) / 2 * r.height };
  }, target);
  await page.mouse.click(screen.x, screen.y);
  await page.waitForTimeout(300);
  const sel = await page.$eval('#selection', (e) => e.innerText);
  check(!/Klikni/.test(sel), `object selected: ${sel.split('\n')[0]}`);
  await page.click('#objUp');
  await page.waitForFunction(() => /posun 1 objektov/.test(document.querySelector('#log').innerText), null, { timeout: 30000 });
  check(true, 'object raised');
  await page.click('#objLeft');
  await page.waitForFunction(() => /otočenie 1 objektov/.test(document.querySelector('#log').innerText), null, { timeout: 30000 });
  check(true, 'object rotated');
  await page.click('#objPlace');
  const spot = await onLine(25);
  await page.mouse.click(spot.x + 25, spot.y);
  await page.waitForFunction(() => /premiestnenie 1 objektov|off the terrain/.test(document.querySelector('#log').innerText), null, { timeout: 30000 });
  check(/premiestnenie 1 objektov/.test(await logText()), 'object placed by click');
}

// Save.
const outIso = path.join(out, 'editor-saved.iso');
await page.fill('#output', outIso);
await page.click('#save');
await page.waitForFunction(() => /uložené|error|nothing/i.test(document.querySelector('#log').innerText), null, { timeout: 60000 });
text = await logText();
check(/uložené/.test(text) && fs.existsSync(outIso), `saved: ${text.split('\n')[0]}`);
await page.screenshot({ path: path.join(out, 'editor-3-final.png') });
check(errors.length === 0, `no page errors ${errors.join(' | ')}`);
await browser.close();
