// SSX 3 map editor (three.js front end). The server does every edit; this page
// only shows the course and turns clicks into edit requests.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const $ = (id) => document.getElementById(id);
const CM = 100;          // game data is in centimetres, the page talks metres
const SEG = 6;           // tessellation of one bicubic patch per side
const SINK_LIMIT = 50000; // objects this far (cm) under the terrain were removed

// ---------------------------------------------------------------- server API
async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function log(msg, cls = '') {
  const div = document.createElement('div');
  div.textContent = msg;
  if (cls) div.className = cls;
  $('log').prepend(div);
}

function busy(on, text = 'working…') {
  $('busy').classList.toggle('hidden', !on);
  $('busyText').textContent = text;
}

// ---------------------------------------------------------------- three.js
const view = $('view');
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
view.appendChild(renderer.domElement);
const labelRenderer = new CSS2DRenderer();
Object.assign(labelRenderer.domElement.style, { position: 'absolute', top: '0', left: '0', pointerEvents: 'none' });
view.appendChild(labelRenderer.domElement);

const scene = new THREE.Scene();
// The sky is its own pass under everything (like the game): its glows and clouds blend.
const skyScene = new THREE.Scene();
renderer.autoClear = false;
renderer.setClearColor(0xa9c2d6);
const camera = new THREE.PerspectiveCamera(55, 1, 0.5, 20000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.495;
scene.add(new THREE.HemisphereLight(0xffffff, 0x50606f, 1.7));
const sun = new THREE.DirectionalLight(0xffffff, 1.5);
sun.position.set(0.5, 1, 0.35);
scene.add(sun);

const transform = new TransformControls(camera, renderer.domElement);
transform.setSize(0.9);
scene.add(transform);
const proxy = new THREE.Object3D();
scene.add(proxy);
transform.addEventListener('dragging-changed', (e) => {
  controls.enabled = !e.value;
  if (!e.value) commitMove();
});

function resize() {
  const w = view.clientWidth, h = view.clientHeight;
  renderer.setSize(w, h);
  labelRenderer.setSize(w, h);
  camera.aspect = w / Math.max(1, h);
  camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize);

// ---------------------------------------------------------------- state
const state = {
  code: null, course: null, origin: [0, 0, 0], zmin: 0, zmax: 1,
  terrain: new THREE.Group(), objects: null, overlay: new THREE.Group(), brush: new THREE.Group(),
  tool: 'view', sel: null, index: null, line: null, textures: new Map(), framed: false,
  models: new THREE.Group(), sky: new THREE.Group(), packs: new Map(), selBox: null,
  stroke: null, placing: false, grab: null,
};
scene.add(state.terrain, state.overlay, state.brush, state.models);
skyScene.add(state.sky);

const toScene = (p) => new THREE.Vector3((p[0] - state.origin[0]) / CM, (p[2] - state.origin[2]) / CM,
  -(p[1] - state.origin[1]) / CM);
const toGame = (v) => [v.x * CM + state.origin[0], -v.z * CM + state.origin[1], v.y * CM + state.origin[2]];

// ---------------------------------------------------------------- patch maths (game space, cm)
function evalPatch(c, u, v) {
  const up = [1, u, u * u, u * u * u], vp = [1, v, v * v, v * v * v];
  let x = 0, y = 0, z = 0;
  for (let j = 0; j < 4; j++) for (let i = 0; i < 4; i++) {
    const w = up[i] * vp[j], k = (j * 4 + i) * 3;
    x += c[k] * w; y += c[k + 1] * w; z += c[k + 2] * w;
  }
  return [x, y, z];
}

function evalDerivs(c, u, v) {
  const up = [1, u, u * u, u * u * u], vp = [1, v, v * v, v * v * v];
  const dup = [0, 1, 2 * u, 3 * u * u], dvp = [0, 1, 2 * v, 3 * v * v];
  const du = [0, 0, 0], dv = [0, 0, 0];
  for (let j = 0; j < 4; j++) for (let i = 0; i < 4; i++) {
    const k = (j * 4 + i) * 3, a = dup[i] * vp[j], b = up[i] * dvp[j];
    for (let n = 0; n < 3; n++) { du[n] += c[k + n] * a; dv[n] += c[k + n] * b; }
  }
  return [du, dv];
}

const CELL = 3000;
function buildIndex(patches) {
  const cells = new Map();
  const info = patches.map((p, n) => {
    let lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
    for (let a = 0; a <= 4; a++) for (let b = 0; b <= 4; b++) {
      const q = evalPatch(p.c, a / 4, b / 4);
      for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], q[k]); hi[k] = Math.max(hi[k], q[k]); }
    }
    const p00 = evalPatch(p.c, 0, 0), p10 = evalPatch(p.c, 1, 0), p01 = evalPatch(p.c, 0, 1);
    const edge = (Math.hypot(p10[0] - p00[0], p10[1] - p00[1], p10[2] - p00[2]) +
      Math.hypot(p01[0] - p00[0], p01[1] - p00[1], p01[2] - p00[2])) / 2;
    for (let ix = Math.floor(lo[0] / CELL); ix <= Math.floor(hi[0] / CELL); ix++)
      for (let iy = Math.floor(lo[1] / CELL); iy <= Math.floor(hi[1] / CELL); iy++) {
        const key = ix + ',' + iy;
        if (!cells.has(key)) cells.set(key, []);
        cells.get(key).push(n);
      }
    return { lo, hi, edge, cx: (lo[0] + hi[0]) / 2, cy: (lo[1] + hi[1]) / 2 };
  });
  return { cells, info, patches };
}

function surfaceZ(x, y) {
  const idx = state.index;
  if (!idx) return null;
  const list = idx.cells.get(Math.floor(x / CELL) + ',' + Math.floor(y / CELL)) || [];
  let best = null;
  for (const n of list) {
    const inf = idx.info[n];
    if (x < inf.lo[0] - 1 || x > inf.hi[0] + 1 || y < inf.lo[1] - 1 || y > inf.hi[1] + 1) continue;
    const c = idx.patches[n].c;
    let u = 0.5, v = 0.5;
    for (let it = 0; it < 20; it++) {
      const p = evalPatch(c, u, v);
      const [du, dv] = evalDerivs(c, u, v);
      const det = du[0] * dv[1] - dv[0] * du[1];
      if (Math.abs(det) < 1e-9) break;
      const rx = x - p[0], ry = y - p[1];
      const su = (dv[1] * rx - dv[0] * ry) / det, sv = (du[0] * ry - du[1] * rx) / det;
      u += su; v += sv;
      if (Math.abs(su) + Math.abs(sv) < 1e-7) break;
    }
    if (u < -1e-3 || u > 1.001 || v < -1e-3 || v > 1.001) continue;
    const p = evalPatch(c, Math.min(1, Math.max(0, u)), Math.min(1, Math.max(0, v)));
    if (Math.abs(p[0] - x) < 2 && Math.abs(p[1] - y) < 2 && (best === null || p[2] > best)) best = p[2];
  }
  return best;
}

function localPatchSize(x, y) {
  const idx = state.index;
  const edges = [];
  for (const inf of idx.info) if (Math.hypot(inf.cx - x, inf.cy - y) < 4000) edges.push(inf.edge);
  if (!edges.length) return null;
  edges.sort((a, b) => a - b);
  return edges[edges.length >> 1] / CM;
}

// Same defaults as mapedit.shape_dims (metres).
function shapeDims(unit) {
  return { radius: Math.max(12, 2 * unit), edge: Math.max(5, 1.5 * unit), length: Math.max(20, 3 * unit),
    width: Math.max(12, 2 * unit), drop: Math.max(8, 1.5 * unit) };
}

// ---------------------------------------------------------------- course line
function makeLine(line) {
  if (!line) return null;
  const pts = line.pts, acc = [0];
  for (let k = 1; k < pts.length; k++)
    acc.push(acc[k - 1] + Math.hypot(pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1], pts[k][2] - pts[k - 1][2]));
  return {
    pts, acc, offset: line.offset, length: line.length,
    at(d) {                                  // d: cm after the start
      const t = this.offset + Math.max(0, d);
      for (let k = 0; k < pts.length - 1; k++) {
        if (t <= acc[k + 1] || k === pts.length - 2) {
          const s = acc[k + 1] - acc[k], f = s ? Math.min(1, Math.max(0, (t - acc[k]) / s)) : 0;
          const a = pts[k], b = pts[k + 1];
          const h = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
          return { p: [0, 1, 2].map((i) => a[i] + (b[i] - a[i]) * f), h: [(b[0] - a[0]) / h, (b[1] - a[1]) / h] };
        }
      }
      return { p: pts[pts.length - 1], h: [1, 0] };
    },
    nearest(x, y) {
      let best = null;
      for (let k = 0; k < pts.length - 1; k++) {
        const a = pts[k], b = pts[k + 1], dx = b[0] - a[0], dy = b[1] - a[1], n = dx * dx + dy * dy;
        const t = n ? Math.min(1, Math.max(0, ((x - a[0]) * dx + (y - a[1]) * dy) / n)) : 0;
        const d = Math.hypot(a[0] + dx * t - x, a[1] + dy * t - y);
        if (!best || d < best.off) {
          const h = Math.sqrt(n) || 1;
          best = { off: d, dist: acc[k] + t * (acc[k + 1] - acc[k]) - this.offset, h: [dx / h, dy / h] };
        }
      }
      return best;
    },
  };
}

// ---------------------------------------------------------------- building the scene
function disposeGroup(group) {
  for (const child of [...group.children]) {
    group.remove(child);
    child.traverse?.((o) => {
      o.geometry?.dispose?.();
      if (o.element) o.element.remove();
    });
  }
}

const colouredMaterial = new THREE.MeshLambertMaterial({ vertexColors: true, side: THREE.DoubleSide });
const lowColour = new THREE.Color(0x5b7d9c), highColour = new THREE.Color(0xf4f8fb);
const changedColour = new THREE.Color(0xff9a3c);
const gameLook = () => $('gameLook').checked;

// ---- game look: the PS2 combines raw texel bytes, so these shaders do too (no colour management).
const fog = { colour: new THREE.Vector3(0.70, 0.82, 1.0), near: 30, far: 100, max: 0 };
const VERT_TERRAIN = `precision highp float;
uniform mat4 modelViewMatrix; uniform mat4 projectionMatrix;
attribute vec3 position; attribute vec2 uv; attribute vec2 luv; attribute vec3 normal; attribute float hl;
varying vec2 vUv; varying vec2 vLuv; varying float vDepth; varying vec3 vN; varying float vHl;
void main() { vUv = uv; vLuv = luv; vN = normal; vHl = hl; vec4 mv = modelViewMatrix * vec4(position, 1.0);
  vDepth = -mv.z; gl_Position = projectionMatrix * mv; }`;
const FOG_GLSL = `uniform vec3 fogColour; uniform float fogNear; uniform float fogFar; uniform float fogMax;
vec3 applyFog(vec3 c, float d) { return mix(c, fogColour, fogMax * clamp((d - fogNear) / max(1.0, fogFar - fogNear), 0.0, 1.0)); }`;
// Terrain (PS2 0x81 blend of the light page over the base texture): C = (T - L.rgb) * L.a / 128.
// relief: the game's lighting is baked, so a new jump would look flat; shade by the surface normal too.
const FRAG_TERRAIN = `precision highp float;
uniform sampler2D map; uniform sampler2D lmap; uniform float hasMap; uniform float hasLight;
uniform float relief; uniform float showHl;
${FOG_GLSL}
varying vec2 vUv; varying vec2 vLuv; varying float vDepth; varying vec3 vN; varying float vHl;
void main() {
  vec3 t = hasMap > 0.5 ? texture2D(map, vUv).rgb : vec3(0.86, 0.89, 0.93);
  vec3 c = t;
  if (hasLight > 0.5) { vec4 l = texture2D(lmap, vLuv); c = clamp((t - l.rgb) * l.a * (255.0 / 128.0), 0.0, 1.0); }
  if (relief > 0.5) c *= 0.45 + 0.75 * max(dot(normalize(vN), normalize(vec3(-0.5, 0.75, 0.45))), 0.0);
  if (showHl > 0.5 && vHl > 0.5) c = mix(c, vec3(1.0, 0.45, 0.0), 0.5);
  gl_FragColor = vec4(applyFog(c, vDepth), 1.0);
}`;
const VERT_MODEL = `precision highp float;
uniform mat4 modelViewMatrix; uniform mat4 projectionMatrix;
attribute vec3 position; attribute vec2 uv; attribute vec4 color;
varying vec2 vUv; varying vec4 vCol; varying float vDepth;
void main() { vUv = uv; vCol = color; vec4 mv = modelViewMatrix * vec4(position, 1.0); vDepth = -mv.z;
  gl_Position = projectionMatrix * mv; }`;
// Static models (TFX MODULATE with the baked colour): C = T * (c5 << 3) >> 7, A = Ta when bit 15 is set.
// The sky (blend = 1) is alpha blended, and parts whose texture did not load are left out.
const FRAG_MODEL = `precision highp float;
uniform sampler2D map; uniform float hasMap; uniform float alphaRef; uniform float useFog; uniform float blend;
${FOG_GLSL}
varying vec2 vUv; varying vec4 vCol; varying float vDepth;
void main() {
  if (blend > 0.5 && hasMap < 0.5) discard;
  vec4 t = hasMap > 0.5 ? texture2D(map, vUv) : vec4(0.75, 0.75, 0.75, 1.0);
  float a = t.a * vCol.a;
  if (a < alphaRef) discard;
  vec3 c = clamp(t.rgb * vCol.rgb * (255.0 / 128.0), 0.0, 1.0);
  gl_FragColor = vec4(useFog > 0.5 ? applyFog(c, vDepth) : c, blend > 0.5 ? a : 1.0);
}`;
const fogUniforms = () => ({ fogColour: { value: fog.colour }, fogNear: { value: fog.near },
  fogFar: { value: fog.far }, fogMax: { value: fog.max } });
const gameMaterials = new Set();

function updateFog() {
  for (const m of gameMaterials) {
    m.uniforms.fogNear.value = fog.near; m.uniforms.fogFar.value = fog.far;
    m.uniforms.fogMax.value = $('showFog').checked ? fog.max : 0;
  }
  renderer.setClearColor(gameLook()
    ? new THREE.Color().setRGB(fog.colour.x, fog.colour.y, fog.colour.z, THREE.SRGBColorSpace)
    : new THREE.Color(0xa9c2d6));
}

function rawTexture(url, clamp, onLoad) {
  // Raw texel bytes, PS2 orientation (v = 0 at the top), no sRGB decoding.
  return new THREE.TextureLoader().load(url, (tex) => {
    tex.flipY = false;
    tex.colorSpace = THREE.NoColorSpace;
    tex.wrapS = tex.wrapT = clamp ? THREE.ClampToEdgeWrapping : THREE.RepeatWrapping;
    tex.needsUpdate = true;
    onLoad(tex);
  }, undefined, () => onLoad(null));
}

function cachedTexture(kind, id, code, clamp, apply) {
  const key = `${kind}:${id}:${clamp ? 'c' : 'r'}`;
  let entry = state.textures.get(key);
  if (!entry) {
    entry = { tex: undefined, waiting: [] };
    state.textures.set(key, entry);
    rawTexture(`/api/${kind}?id=${id}&code=${code}`, clamp, (tex) => {
      entry.tex = tex;
      for (const fn of entry.waiting) fn(tex);
      entry.waiting = [];
    });
  }
  if (entry.tex !== undefined) apply(entry.tex);
  else entry.waiting.push(apply);
}

function terrainMaterial(tex, page) {
  const m = new THREE.RawShaderMaterial({ vertexShader: VERT_TERRAIN, fragmentShader: FRAG_TERRAIN,
    side: THREE.DoubleSide, uniforms: { map: { value: null }, lmap: { value: null }, hasMap: { value: 0 },
      hasLight: { value: 0 }, relief: { value: $('relief').checked ? 1 : 0 },
      showHl: { value: $('showChanges').checked ? 1 : 0 }, ...fogUniforms() } });
  gameMaterials.add(m);
  if (tex >= 0) cachedTexture('texture', tex, state.code, false, (t) => {
    if (t) { m.uniforms.map.value = t; m.uniforms.hasMap.value = 1; } });
  if (page >= 0) cachedTexture('lightpage', page, state.code, true, (t) => {
    if (t) { m.uniforms.lmap.value = t; m.uniforms.hasLight.value = 1; } });
  updateFog();
  return m;
}

function modelMaterial(tex, code, sky) {
  const m = new THREE.RawShaderMaterial({ vertexShader: VERT_MODEL, fragmentShader: FRAG_MODEL,
    side: THREE.DoubleSide, depthTest: !sky, depthWrite: !sky, transparent: !!sky,
    uniforms: { map: { value: null }, hasMap: { value: 0 }, alphaRef: { value: sky ? 0.02 : 0.3 },
      useFog: { value: sky ? 0 : 1 }, blend: { value: sky ? 1 : 0 }, ...fogUniforms() } });
  gameMaterials.add(m);
  if (tex >= 0) cachedTexture('texture', tex, code, sky, (t) => {
    if (t) { m.uniforms.map.value = t; m.uniforms.hasMap.value = 1; } });
  updateFog();
  return m;
}

async function loadPack(code) {
  if (state.packs.has(code)) return state.packs.get(code);
  const res = await fetch(`/api/models?code=${encodeURIComponent(code)}`);
  if (!res.ok) { state.packs.set(code, null); return null; }
  const buf = await res.arrayBuffer();
  const n = new DataView(buf).getUint32(4, true);
  const head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 8, n)));
  let at = 8 + n;
  const verts = new Float32Array(buf, at, head.vertices * 5); at += head.vertices * 20;
  const idx = new Uint32Array(buf, at, head.indices); at += head.indices * 4;
  const cols = new Uint8Array(buf, at, head.colorCount * 4);
  const pack = { ...head, verts, idx, cols };
  state.packs.set(code, pack);
  if (head.errors) log(`${code}: ${head.errors} models could not be read`, 'err');
  return pack;
}

// Merge every placed model into one geometry per texture (static scenery, one draw per texture).
function mergeModels(pack, placements, transformPoint, code, sky) {
  const groups = new Map();
  for (const pl of placements) {
    const meshes = pack.models[pl.mod];
    if (!meshes) continue;
    const colours = pack.colors[pl.k];
    for (const mesh of meshes) {
      if (!groups.has(mesh.tex)) groups.set(mesh.tex, { pos: [], uv: [], col: [], idx: [] });
      const g = groups.get(mesh.tex);
      const base = g.pos.length / 3;
      for (let v = 0; v < mesh.vn; v++) {
        const o = (mesh.v0 + v) * 5;
        const p = transformPoint(pack.verts[o], pack.verts[o + 1], pack.verts[o + 2], pl);
        g.pos.push(p.x, p.y, p.z);
        g.uv.push(pack.verts[o + 3], pack.verts[o + 4]);
        const ci = colours && mesh.co + v < colours[1] ? (colours[0] + mesh.co + v) * 4 : -1;
        if (ci >= 0) g.col.push(pack.cols[ci], pack.cols[ci + 1], pack.cols[ci + 2], pack.cols[ci + 3]);
        else g.col.push(128, 128, 128, 255);
      }
      for (let i = 0; i < mesh.ni; i++) g.idx.push(base + pack.idx[mesh.i0 + i]);
    }
  }
  const out = [];
  for (const [tex, g] of groups) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(g.pos, 3));
    geo.setAttribute('uv', new THREE.Float32BufferAttribute(g.uv, 2));
    geo.setAttribute('color', new THREE.Uint8BufferAttribute(g.col, 4, true));
    geo.setIndex(g.idx);
    geo.computeBoundingSphere();
    const mesh = new THREE.Mesh(geo, modelMaterial(tex, code, sky));
    if (sky) { mesh.renderOrder = -1000; mesh.frustumCulled = false; }
    out.push(mesh);
  }
  return out;
}

function buildModels() {
  disposeGroup(state.models);
  const pack = state.packs.get(state.code);
  if (!pack || !gameLook() || !$('showObjects').checked) return;
  const place = (x, y, z, pl) => {
    const m = pl.m, s = pl.s;
    x *= s; y *= s; z *= s;
    // Row vectors (PS2 convention): world = v * M.
    return toScene([x * m[0] + y * m[4] + z * m[8] + m[12], x * m[1] + y * m[5] + z * m[9] + m[13],
      x * m[2] + y * m[6] + z * m[10] + m[14]]);
  };
  for (const mesh of mergeModels(pack, visibleObjects(), place, state.code, false)) state.models.add(mesh);
}

function buildSky() {
  disposeGroup(state.sky);
  const code = state.course && state.course.sky;
  const pack = code && state.packs.get(code);
  if (!pack || !gameLook()) return;
  // The sky dome is ~3 m across and drawn around the camera before everything else.
  const place = (x, y, z) => new THREE.Vector3(x / CM, z / CM, -y / CM);
  const all = Object.keys(pack.models).map((mod) => ({ k: '', mod }));
  for (const mesh of mergeModels(pack, all, place, code, true)) state.sky.add(mesh);
}

function buildTerrain() {
  disposeGroup(state.terrain);
  const groups = new Map();
  const { zmin, zmax } = state;
  const col = new THREE.Color();
  for (const p of state.course.patches) {
    const key = `${p.t}|${p.lp}`;
    if (!groups.has(key)) groups.set(key, { tex: p.t, page: p.lp, pos: [], nrm: [], uv: [], luv: [], col: [], hl: [], idx: [] });
    const g = groups.get(key);
    const lr = p.l;     // lighting rectangle in the light page: u0, v0, du, dv
    const base = g.pos.length / 3;
    const uvc = p.uv;     // corners (0,0), (0,1), (1,0), (1,1)
    for (let b = 0; b <= SEG; b++) for (let a = 0; a <= SEG; a++) {
      const u = a / SEG, v = b / SEG;
      const q = evalPatch(p.c, u, v);
      const [du, dv] = evalDerivs(p.c, u, v);
      let n = [du[1] * dv[2] - du[2] * dv[1], du[2] * dv[0] - du[0] * dv[2], du[0] * dv[1] - du[1] * dv[0]];
      if (n[2] < 0) n = n.map((x) => -x);
      const len = Math.hypot(...n) || 1;
      const s = toScene(q);
      g.pos.push(s.x, s.y, s.z);
      g.nrm.push(n[0] / len, n[2] / len, -n[1] / len);
      g.uv.push(uvc[0] * (1 - u) * (1 - v) + uvc[4] * u * (1 - v) + uvc[2] * (1 - u) * v + uvc[6] * u * v,
        uvc[1] * (1 - u) * (1 - v) + uvc[5] * u * (1 - v) + uvc[3] * (1 - u) * v + uvc[7] * u * v);
      g.luv.push(lr[0] + u * lr[2], lr[1] + v * lr[3]);
      col.lerpColors(lowColour, highColour, Math.min(1, Math.max(0, (q[2] - zmin) / (zmax - zmin || 1))));
      if (p.ch && $('showChanges').checked) col.lerp(changedColour, 0.5);
      g.col.push(col.r, col.g, col.b);
      g.hl.push(p.ch ? 1 : 0);
    }
    for (let b = 0; b < SEG; b++) for (let a = 0; a < SEG; a++) {
      const i0 = base + b * (SEG + 1) + a, i1 = i0 + 1, i2 = i0 + SEG + 1, i3 = i2 + 1;
      g.idx.push(i0, i1, i2, i1, i3, i2);
    }
  }
  for (const g of groups.values()) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(g.pos, 3));
    geo.setAttribute('normal', new THREE.Float32BufferAttribute(g.nrm, 3));
    geo.setAttribute('uv', new THREE.Float32BufferAttribute(g.uv, 2));
    geo.setAttribute('luv', new THREE.Float32BufferAttribute(g.luv, 2));
    geo.setAttribute('color', new THREE.Float32BufferAttribute(g.col, 3));
    geo.setAttribute('hl', new THREE.Float32BufferAttribute(g.hl, 1));
    geo.setIndex(g.idx);
    geo.computeBoundingSphere();
    const mesh = new THREE.Mesh(geo, gameLook() ? terrainMaterial(g.tex, g.page) : colouredMaterial);
    state.terrain.add(mesh);
  }
}

function visibleObjects() {
  const show = $('showObjects').checked, helpers = $('showHelpers').checked;
  return state.course.objects.filter((o) => show && (helpers || !o.p) && o.lo[2] > state.zmin - SINK_LIMIT);
}

function buildObjects() {
  if (state.objects) { scene.remove(state.objects); state.objects.geometry.dispose(); state.objects = null; }
  const list = visibleObjects();
  if (!list.length) return;
  const boxes = !gameLook() || $('showBoxes').checked;
  const mesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1),
    new THREE.MeshLambertMaterial({ transparent: true, opacity: 0.6, visible: boxes }), list.length);
  const m = new THREE.Matrix4(), q = new THREE.Quaternion(), colour = new THREE.Color();
  list.forEach((o, i) => {
    const centre = toScene([(o.lo[0] + o.hi[0]) / 2, (o.lo[1] + o.hi[1]) / 2, (o.lo[2] + o.hi[2]) / 2]);
    const size = new THREE.Vector3(Math.max(0.3, (o.hi[0] - o.lo[0]) / CM), Math.max(0.3, (o.hi[2] - o.lo[2]) / CM),
      Math.max(0.3, (o.hi[1] - o.lo[1]) / CM));
    mesh.setMatrixAt(i, m.compose(centre, q, size));
    colour.set(state.sel && state.sel.k === o.k ? 0xffeb3b : o.p ? 0xff9800 : 0x2e9d48);
    mesh.setColorAt(i, colour);
  });
  mesh.instanceMatrix.needsUpdate = true;
  mesh.instanceColor.needsUpdate = true;
  mesh.userData.list = list;
  state.objects = mesh;
  scene.add(mesh);
  if (state.selBox) { scene.remove(state.selBox); state.selBox.geometry.dispose(); state.selBox = null; }
  const sel = state.sel && list.find((o) => o.k === state.sel.k);
  if (sel) {
    const centre = toScene([(sel.lo[0] + sel.hi[0]) / 2, (sel.lo[1] + sel.hi[1]) / 2, (sel.lo[2] + sel.hi[2]) / 2]);
    const box = new THREE.BoxGeometry(Math.max(0.3, (sel.hi[0] - sel.lo[0]) / CM), Math.max(0.3, (sel.hi[2] - sel.lo[2]) / CM),
      Math.max(0.3, (sel.hi[1] - sel.lo[1]) / CM));
    state.selBox = new THREE.LineSegments(new THREE.EdgesGeometry(box), new THREE.LineBasicMaterial({ color: 0xffeb3b }));
    state.selBox.position.copy(centre);
    scene.add(state.selBox);
  }
}

function label(text, cls, pos) {
  const div = document.createElement('div');
  div.className = 'label3d ' + cls;
  div.textContent = text;
  const obj = new CSS2DObject(div);
  obj.position.copy(pos);
  return obj;
}

function buildOverlay() {
  disposeGroup(state.overlay);
  const c = state.course;
  if (state.line) {
    const pts = state.line.pts.map((p) => toScene([p[0], p[1], p[2] + 60]));
    state.overlay.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: 0xff5252 })));
    const step = state.line.length > 60000 ? 10000 : 1000;
    for (let d = 0; d <= state.line.length; d += step) {
      const { p } = state.line.at(d);
      const pos = toScene([p[0], p[1], p[2] + 150]);
      const dot = new THREE.Mesh(new THREE.SphereGeometry(0.8, 8, 6), new THREE.MeshBasicMaterial({ color: 0xff5252 }));
      dot.position.copy(pos);
      state.overlay.add(dot, label(`${Math.round(d / CM)} m`, 'mark', pos));
    }
  }
  for (const r of c.regions) {
    const pos = toScene([r.p[0], r.p[1], r.p[2] + 100]);
    const colour = r.kind === 0 ? 0x00e676 : 0x40c4ff;
    const ball = new THREE.Mesh(new THREE.SphereGeometry(r.kind === 0 ? 1 : 1.4, 10, 8),
      new THREE.MeshBasicMaterial({ color: colour }));
    ball.position.copy(pos);
    state.overlay.add(ball);
    if (r.kind === 1) state.overlay.add(label(`S${r.slot}`, 'session', pos));
    else if (r.slot === 0) state.overlay.add(label('START', 'start', pos));
  }
  for (const m of c.marks || []) {
    const z = m.z ?? surfaceZ(m.x, m.y) ?? state.zmin;
    const foot = toScene([m.x, m.y, z]), top = toScene([m.x, m.y, z + 2500]);
    state.overlay.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([foot, top]),
      new THREE.LineBasicMaterial({ color: 0xffa726 })));
    const tag = label(m.t, 'recipe', top);
    tag.userData.far = true;
    state.overlay.add(tag);
  }
  if ($('showRails').checked) {
    const mat = new THREE.LineBasicMaterial({ color: 0xe040fb });
    for (const rail of c.rails) {
      if (rail.pts.length < 2) continue;
      state.overlay.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(rail.pts.map(toScene)), mat));
    }
  }
}

// ---------------------------------------------------------------- loading
async function loadCourse(code, keepView) {
  busy(true, 'loading the course…');
  try {
    const course = await api(`/api/course?code=${encodeURIComponent(code)}`);
    if (code !== state.code) state.textures.clear();
    state.code = code;
    state.course = course;
    if (!keepView || !state.framed) {
      let lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
      for (const p of course.patches) for (const k of [0, 1, 2]) {
        lo[k] = Math.min(lo[k], p.c[k]); hi[k] = Math.max(hi[k], p.c[k]);   // constant terms: P(0,0)
      }
      state.origin = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]];
      state.zmin = lo[2]; state.zmax = hi[2];
    }
    state.line = makeLine(course.line);
    state.index = buildIndex(course.patches);
    if (course.fog) {
      fog.colour.set(course.fog.r, course.fog.g, course.fog.b);
      // The painted near/far (30..100 m on Snow Jam) feed the PS2 fog composite; as plain linear
      // fog that would hide the course, so the page stretches it (an approximation).
      fog.near = course.fog.near_cm / CM; fog.far = Math.max(fog.near + 1, course.fog.far_cm / CM) * 8; fog.max = 0.75;
    } else fog.max = 0;
    for (const m of gameMaterials) m.dispose();
    gameMaterials.clear();
    await Promise.all([loadPack(code), course.sky ? loadPack(course.sky) : null]);
    buildTerrain();
    buildModels();
    buildSky();
    buildObjects();
    buildOverlay();
    updateFog();
    const len = state.line ? state.line.length / CM : 0;
    $('along').max = Math.max(10, Math.round(len));
    $('courseInfo').textContent = `${course.name}: ${course.locations.join(', ')} · ${course.patches.length} patches, ` +
      `${course.objects.length} objects` + (len ? ` · course ${Math.round(len)} m` : '');
    showMarks(course);
    updateUndo(course.undo);
    if (!keepView || !state.framed) { flyAlong(0); state.framed = true; }
    if (state.sel) reselect();
  } catch (e) {
    log(e.message, 'err');
  } finally {
    busy(false);
  }
}

function updateUndo(list) {
  $('undo').disabled = !list.length;
  $('undoInfo').textContent = list.length ? `last: ${list[list.length - 1]}` : 'no edits';
}

function flyTo(x, y, z) {
  // Look at (x, y) from 90 m back along the course and 45 m up.
  const h = state.line ? state.line.nearest(x, y).h : [1, 0];
  const target = toScene([x, y, z ?? surfaceZ(x, y) ?? state.zmin]);
  controls.target.copy(target);
  camera.position.copy(target).addScaledVector(new THREE.Vector3(h[0], 0, -h[1]), -90).add(new THREE.Vector3(0, 45, 0));
  controls.update();
}

function showMarks(course) {
  const box = $('marks');
  box.innerHTML = '';
  if (course.changed) {
    box.append(Object.assign(document.createElement('div'), { className: 'changed',
      textContent: `changed patches: ${course.changed}` + (course.compare ? ' (against the original game)' : ' (against the opened ISO)') }));
  }
  for (const m of course.marks || []) {
    const b = Object.assign(document.createElement('button'), { className: 'small', textContent: m.t });
    b.addEventListener('click', () => flyTo(m.x, m.y, m.z));
    box.append(b);
  }
}

function syncRecipeFields() {
  const flat = $('recipe').value === '__flat__';
  $('flatParams').classList.toggle('hidden', !flat);
  $('recipeNoWarpLabel').classList.toggle('hidden', flat);
}

async function applyFlat() {
  if (!confirm(`Wipe the course ${state.course ? state.course.name : ''} and build a plain slope? (Undo brings it back.)`)) return;
  busy(true, 'building the plain slope…');
  try {
    const res = await api('/api/flat', { code: state.code, grade: $('flatGrade').value, width: $('flatWidth').value,
      walls: $('flatWalls').value, every: $('flatEvery').value, heights: $('flatHeights').value });
    log(res.message, 'ok');
    await loadCourse(state.code, false);
  } catch (e) {
    log(e.message, 'err');
  } finally {
    busy(false);
  }
}

async function applyRecipe() {
  const name = $('recipe').value;
  if (!name) return;
  if (name === '__flat__') return applyFlat();
  busy(true, 'building the course from the recipe… (checking the space in the game data takes a few minutes)');
  try {
    const res = await api('/api/recipe', { name, skip: $('recipeNoWarp').checked ? ['warp'] : [] });
    for (const st of res.steps.slice().reverse()) log(`${st.n}. ${st.tag}: ${st.ok ? 'ok' : 'not possible: ' + st.msg}`, st.ok ? '' : 'err');
    log(res.message, 'ok');
    if (res.code !== state.code) { $('course').value = res.code; state.framed = false; }
    await loadCourse(res.code, res.code === state.code);
  } catch (e) {
    log(e.message, 'err');
  } finally {
    busy(false);
  }
}

function flyAlong(metres) {
  $('along').value = metres;
  $('alongLabel').textContent = `${Math.round(metres)} m`;
  let target, heading;
  if (state.line) {
    const { p, h } = state.line.at(metres * CM);
    target = toScene(p); heading = new THREE.Vector3(h[0], 0, -h[1]);
  } else {
    target = new THREE.Vector3(0, (state.zmax - state.zmin) / CM / 2, 0); heading = new THREE.Vector3(1, 0, 0);
  }
  controls.target.copy(target);
  camera.position.copy(target).addScaledVector(heading, -90).add(new THREE.Vector3(0, 45, 0));
  controls.update();
}

// ---------------------------------------------------------------- picking
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();

function setPointer(e) {
  const r = renderer.domElement.getBoundingClientRect();
  pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
}

function hitTerrain() {
  const hits = raycaster.intersectObjects(state.terrain.children, false);
  return hits.length ? hits[0].point : null;
}

function hitObject() {
  if (!state.objects) return null;
  const hits = raycaster.intersectObject(state.objects, false);
  if (!hits.length) return null;
  return state.objects.userData.list[hits[0].instanceId];
}

// ---------------------------------------------------------------- terrain brush
function brushParams(x, y) {
  const unit = localPatchSize(x, y) || 10;
  const auto = shapeDims(unit);
  const dims = {};
  for (const k of ['radius', 'edge', 'length', 'width', 'drop']) {
    const v = parseFloat($(k).value);
    dims[k] = $('autoDims').checked || !(v > 0) ? auto[k] : v;
    if ($('autoDims').checked) $(k).value = auto[k].toFixed(0);
  }
  let h = state.line ? state.line.nearest(x, y).h : [1, 0];
  const a = (parseFloat($('rotate').value) || 0) * Math.PI / 180;
  h = [h[0] * Math.cos(a) - h[1] * Math.sin(a), h[0] * Math.sin(a) + h[1] * Math.cos(a)];
  return { dims, heading: h, unit };
}

function drapedLoop(points, colour, fallbackZ) {
  let last = fallbackZ;
  const out = points.map(([x, y]) => {
    const z = surfaceZ(x, y);
    if (z !== null) last = z;          // off the terrain: keep the last height instead of dropping
    return toScene([x, y, last + 40]);
  });
  return new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(out), new THREE.LineBasicMaterial({ color: colour }));
}

function drapedFan(cx, cy, points, colour, fallbackZ) {
  // A translucent filled footprint, drawn over the terrain so it never hides.
  const zc = surfaceZ(cx, cy) ?? fallbackZ;
  const centre = toScene([cx, cy, zc + 40]);
  let last = zc;
  const ring = points.map(([x, y]) => {
    const z = surfaceZ(x, y);
    if (z !== null) last = z;
    return toScene([x, y, last + 40]);
  });
  const pos = [];
  for (let k = 0; k < ring.length; k++) {
    const a = ring[k], b = ring[(k + 1) % ring.length];
    pos.push(centre.x, centre.y, centre.z, a.x, a.y, a.z, b.x, b.y, b.z);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: colour, transparent: true, opacity: 0.22,
    depthTest: false, side: THREE.DoubleSide }));
  mesh.renderOrder = 10;
  return mesh;
}

function circle(x, y, r) {
  const pts = [];
  for (let k = 0; k < 64; k++) { const a = (k / 64) * Math.PI * 2; pts.push([x + r * Math.cos(a), y + r * Math.sin(a)]); }
  return pts;
}

function rect(x, y, h, back, front, half) {
  const f = h, r = [h[1], -h[0]];
  const at = (along, side) => [x + along * f[0] + side * r[0], y + along * f[1] + side * r[1]];
  const pts = [];
  const n = 16;
  for (let k = 0; k <= n; k++) pts.push(at(-back + (back + front) * k / n, -half));
  for (let k = 0; k <= n; k++) pts.push(at(front, -half + 2 * half * k / n));
  for (let k = 0; k <= n; k++) pts.push(at(front - (back + front) * k / n, half));
  for (let k = 0; k <= n; k++) pts.push(at(-back, half - 2 * half * k / n));
  return pts;
}

const BRUSH_COLOURS = { raise: 0xffeb3b, lower: 0x40c4ff, flatten: 0xffffff, smooth: 0x69f0ae };

function brushRadius(x, y) {
  const v = parseFloat($('brushRadius').value);
  if (v > 0) return v;
  return Math.max(12, 2 * (localPatchSize(x, y) || 10));
}

function drapedPath(points, colour, fallbackZ) {
  let last = fallbackZ;
  const out = points.map(([x, y]) => {
    const z = surfaceZ(x, y);
    if (z !== null) last = z;
    return toScene([x, y, last + 50]);
  });
  return new THREE.Line(new THREE.BufferGeometry().setFromPoints(out),
    new THREE.LineBasicMaterial({ color: colour, depthTest: false }));
}

function updateBrush(game) {
  disposeGroup(state.brush);
  if (!game && !(state.tool === 'warp' && state.grab)) return;
  if (state.tool === 'brush') {
    const [x, y, z] = game;
    const mode = $('brushMode').value;
    const r = (state.stroke ? state.stroke.radius : brushRadius(x, y)) * CM;
    state.brush.add(drapedFan(x, y, circle(x, y, r), BRUSH_COLOURS[mode], z));
    state.brush.add(drapedLoop(circle(x, y, r), BRUSH_COLOURS[mode], z));
    if (state.stroke && state.stroke.points.length > 1) state.brush.add(drapedPath(state.stroke.points, BRUSH_COLOURS[mode], z));
    return;
  }
  if (state.tool === 'warp') {
    drawWarp(game);
    return;
  }
  if (state.tool === 'objects' && state.placing && state.sel) {
    const obj = state.course.objects.find((o) => o.k === state.sel.k);
    if (obj) {
      const r = Math.max(100, Math.hypot(obj.hi[0] - obj.lo[0], obj.hi[1] - obj.lo[1]) / 2);
      state.brush.add(drapedLoop(circle(game[0], game[1], r), 0xffeb3b, game[2]));
    }
    return;
  }
  if (state.tool !== 'terrain') return;
  const [x, y, z] = game;
  const { dims, heading } = brushParams(x, y);
  const shape = $('shape').value;
  const m = (v) => v * CM;
  if (shape === 'kicker') {
    const along = (dims.drop - dims.length) / 2;      // centre of the footprint for the fan
    const fx = x + m(along) * heading[0], fy = y + m(along) * heading[1];
    state.brush.add(drapedFan(fx, fy, rect(x, y, heading, m(dims.length), m(dims.drop), m(dims.width / 2)),
      parseFloat($('height').value) < 0 ? 0x40c4ff : 0xffeb3b, z));
    state.brush.add(drapedLoop(rect(x, y, heading, m(dims.length), m(dims.drop), m(dims.width / 2)), 0xffeb3b, z));
    state.brush.add(drapedLoop(rect(x, y, heading, m(dims.length), m(dims.drop), m(dims.width / 2 + dims.edge)),
      0xffa000, z));
    state.brush.add(drapedLoop(rect(x, y, heading, 0, m(1), m(dims.width / 2)), 0xff1744, z));   // the lip
  } else {
    state.brush.add(drapedFan(x, y, circle(x, y, m(dims.radius)),
      parseFloat($('height').value) < 0 ? 0x40c4ff : 0xffeb3b, z));
    state.brush.add(drapedLoop(circle(x, y, m(dims.radius)), 0xffeb3b, z));
    if (shape !== 'bump') state.brush.add(drapedLoop(circle(x, y, m(dims.radius + dims.edge)), 0xffa000, z));
  }
}

// ---------------------------------------------------------------- moving a piece of the course
const WARP_HELP = $('warpInfo').textContent;

function warpDims(moveCm) {
  const radius = Math.max(0, parseFloat($('warpRadius').value) || 0);
  const v = parseFloat($('warpEdge').value);
  const edge = v > 0 ? v : Math.max(40, 2.5 * moveCm / CM);
  return { radius, edge, squeeze: 1 - 1.5 * (moveCm / CM) / edge };
}

function groundAt(z) {
  // Where the pointer ray crosses the horizontal plane at game height z (the grab follows the ground's level).
  const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -toScene([0, 0, z]).y);
  const p = raycaster.ray.intersectPlane(plane, new THREE.Vector3());
  return p ? toGame(p) : null;
}

function drawWarp(game) {
  const g = state.grab;
  const [x, y, z] = g ? [g.x, g.y, g.z] : game;
  const move = g ? Math.hypot(g.tx - g.x, g.ty - g.y) : 0;
  const { radius, edge, squeeze } = warpDims(move);
  const bad = squeeze < 0.35;
  state.brush.add(drapedFan(x, y, circle(x, y, radius * CM), 0xffeb3b, z));
  state.brush.add(drapedLoop(circle(x, y, radius * CM), 0xffeb3b, z));
  state.brush.add(drapedLoop(circle(x, y, (radius + edge) * CM), bad ? 0xff1744 : 0xffa000, z));
  if (!g || move < 1) return;
  state.brush.add(drapedLoop(circle(g.tx, g.ty, radius * CM), 0x69f0ae, z));
  state.brush.add(drapedPath([[x, y], [g.tx, g.ty]], 0xff5252, z));
  $('warpInfo').textContent = `move ${(move / CM).toFixed(1)} m, edge ${edge.toFixed(0)} m, terrain at the edge ` +
    `squeezed to ${Math.max(0, squeeze * 100).toFixed(0)} %` + (bad ? ' – too much, widen the edge' : '');
  $('warpInfo').classList.toggle('err', bad);
}

async function commitWarp(g) {
  const move = Math.hypot(g.tx - g.x, g.ty - g.y);
  const turn = parseFloat($('warpTurn').value) || 0;
  const lift = parseFloat($('warpLift').value) || 0;
  if (move < 30 && !turn && !lift) {
    log('drag the terrain with the mouse (or set a turn or lift and click)');
    updateBrush(null);
    return;
  }
  const { radius, edge } = warpDims(move);
  const [tx, ty] = move < 30 ? [g.x, g.y] : [g.tx, g.ty];
  busy(true, 'moving a piece of the course…');
  try {
    const res = await api('/api/warp', { code: state.code, x: g.x, y: g.y, tx, ty, radius, edge, turn, lift,
      force: $('forceWarp').checked });
    log(res.message, 'ok');
    await loadCourse(state.code, true);
  } catch (e) {
    log(e.message, 'err');
  } finally {
    busy(false);
    $('warpInfo').textContent = WARP_HELP;
    $('warpInfo').classList.remove('err');
    updateBrush(null);
  }
}

async function applyTerrain(game) {
  const [x, y] = game;
  const { heading } = brushParams(x, y);
  const body = { code: state.code, x, y, heading, shape: $('shape').value, height: parseFloat($('height').value),
    force: $('forceTerrain').checked };
  if (!$('autoDims').checked) for (const k of ['radius', 'edge', 'length', 'width', 'drop']) body[k] = parseFloat($(k).value) || null;
  busy(true, 'editing the terrain…');
  try {
    const res = await api('/api/terrain', body);
    log(res.message, 'ok');
    await loadCourse(state.code, true);
  } catch (e) {
    log(e.message, 'err');
  } finally {
    busy(false);
  }
}

async function commitStroke(stroke) {
  const mode = $('brushMode').value;
  const body = { code: state.code, points: stroke.points.map(([x, y]) => [x, y]), mode, radius: stroke.radius,
    height: parseFloat($('brushHeight').value) || 0, strength: parseFloat($('brushStrength').value),
    force: $('forceBrush').checked };
  busy(true, 'editing the terrain…');
  try {
    const res = await api('/api/stroke', body);
    log(res.message, 'ok');
    await loadCourse(state.code, true);
  } catch (e) {
    log(e.message, 'err');
  } finally {
    busy(false);
    updateBrush(null);
  }
}

function syncBrushFields() {
  const mode = $('brushMode').value;
  $('brushHeightLabel').classList.toggle('hidden', mode === 'smooth');
  $('brushStrengthLabel').classList.toggle('hidden', mode === 'raise' || mode === 'lower');
  $('brushHeightLabel').firstChild.textContent = mode === 'flatten' ? 'height shift (m) ' : 'height (m) ';
  if (mode === 'flatten') $('brushHeight').value = 0;
  else if (!(parseFloat($('brushHeight').value) > 0)) $('brushHeight').value = 2;
  $('brushStrengthValue').textContent = $('brushStrength').value;
}

// ---------------------------------------------------------------- objects
function select(obj) {
  state.sel = obj ? { k: obj.k } : null;
  transform.detach();
  if (obj) {
    proxy.position.copy(toScene([(obj.lo[0] + obj.hi[0]) / 2, (obj.lo[1] + obj.hi[1]) / 2, (obj.lo[2] + obj.hi[2]) / 2]));
    state.sel.start = proxy.position.clone();
    transform.attach(proxy);
    const size = [0, 1, 2].map((k) => ((obj.hi[k] - obj.lo[k]) / CM).toFixed(1)).join(' × ');
    $('selection').innerHTML = '';
    $('selection').append(Object.assign(document.createElement('b'), { textContent: obj.n }),
      document.createElement('br'),
      `x ${(((obj.lo[0] + obj.hi[0]) / 2) / CM).toFixed(1)} m, y ${(((obj.lo[1] + obj.hi[1]) / 2) / CM).toFixed(1)} m, ` +
      `size ${size} m` + (obj.p ? ' · game helper object' : ''));
  } else {
    $('selection').textContent = 'Click an object (a tree, a building…).';
  }
  for (const id of ['objUp', 'objDown', 'objRemove', 'objLeft', 'objRight', 'objPlace']) $(id).disabled = !obj;
  setPlacing(false);
  buildObjects();
}

function setPlacing(on) {
  state.placing = on && !!state.sel;
  $('objPlace').classList.toggle('active', state.placing);
  $('objPlace').textContent = state.placing ? 'Click the terrain… (Esc cancels)' : 'Place by click (P)';
  if (!state.placing) updateBrush(null);
}

function reselect() {
  const obj = state.course.objects.find((o) => o.k === state.sel.k);
  select(obj && obj.lo[2] > state.zmin - SINK_LIMIT ? obj : null);
}

async function objectAction(action, extra = {}) {
  if (!state.sel) return;
  busy(true, 'editing the object…');
  try {
    const res = await api('/api/objects', { code: state.code, action, keys: [state.sel.k], ...extra,
      force: $('forceObjects').checked });
    log(res.message, 'ok');
  } catch (e) {
    log(e.message, 'err');
  } finally {
    busy(false);
  }
  await loadCourse(state.code, true);
}

function commitMove() {
  if (!state.sel || !state.sel.start) return;
  const d = proxy.position.clone().sub(state.sel.start);
  if (d.length() < 0.01) return;
  objectAction('move', { delta: [d.x * CM, -d.z * CM, d.y * CM] });
}

// ---------------------------------------------------------------- input
let down = null;
let lastMove = null;
renderer.domElement.addEventListener('pointerdown', (e) => {
  down = { x: e.clientX, y: e.clientY };
  if (state.tool === 'brush' && e.button === 0 && state.course) {
    setPointer(e);
    const p = hitTerrain();
    if (p) {
      const g = toGame(p);
      state.stroke = { points: [[g[0], g[1]]], radius: brushRadius(g[0], g[1]) };
      renderer.domElement.setPointerCapture(e.pointerId);
    }
  }
  if (state.tool === 'warp' && e.button === 0 && state.course) {
    setPointer(e);
    const p = hitTerrain();
    if (p) {
      const g = toGame(p);
      state.grab = { x: g[0], y: g[1], z: g[2], tx: g[0], ty: g[1] };
      renderer.domElement.setPointerCapture(e.pointerId);
      updateBrush(g);
    }
  }
});
renderer.domElement.addEventListener('pointerup', (e) => {
  if (state.grab) {
    const grab = state.grab;
    state.grab = null;
    down = null;
    commitWarp(grab);
    return;
  }
  if (state.stroke) {
    const stroke = state.stroke;
    state.stroke = null;
    down = null;
    commitStroke(stroke);
    return;
  }
  if (!down) return;
  const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
  down = null;
  if (moved > 5 || transform.dragging || transform.axis || e.button !== 0) return;
  setPointer(e);
  if (state.tool === 'terrain') {
    const p = hitTerrain();
    if (p) applyTerrain(toGame(p));
  } else if (state.tool === 'objects') {
    if (state.placing && state.sel) {
      const p = hitTerrain();
      if (p) { const g = toGame(p); setPlacing(false); objectAction('place', { x: g[0], y: g[1] }); }
    } else select(hitObject());
  }
});
renderer.domElement.addEventListener('pointermove', (e) => {
  lastMove = e;
  if (state.grab) {
    setPointer(e);
    const g = groundAt(state.grab.z);
    if (g) { state.grab.tx = g[0]; state.grab.ty = g[1]; updateBrush(null); }
    return;
  }
  if (!state.stroke) return;
  setPointer(e);
  const p = hitTerrain();
  if (!p) return;
  const g = toGame(p);
  const last = state.stroke.points[state.stroke.points.length - 1];
  if (Math.hypot(g[0] - last[0], g[1] - last[1]) >= state.stroke.radius * CM / 4) state.stroke.points.push([g[0], g[1]]);
});
renderer.domElement.addEventListener('pointerleave', () => { lastMove = null; $('tip').classList.add('hidden'); });

function hover() {
  if (!lastMove || !state.course || transform.dragging) return;
  const e = lastMove;
  lastMove = null;
  setPointer(e);
  const tip = $('tip');
  let text = null;
  const obj = !['terrain', 'brush', 'warp'].includes(state.tool) && !state.placing ? hitObject() : null;
  if (obj) text = obj.n;
  else {
    const p = hitTerrain();
    if (p) {
      const g = toGame(p);
      text = `x ${(g[0] / CM).toFixed(1)}  y ${(g[1] / CM).toFixed(1)}  height ${(g[2] / CM).toFixed(1)} m`;
      if (state.line) {
        const n = state.line.nearest(g[0], g[1]);
        text += ` · ${Math.round(n.dist / CM)} m from the start, ${Math.round(n.off / CM)} m off the course`;
      }
      if (!state.grab) updateBrush(g);
    } else if (!state.grab) updateBrush(null);
  }
  tip.classList.toggle('hidden', !text);
  if (text) {
    const r = view.getBoundingClientRect();
    tip.textContent = text;
    tip.style.left = `${e.clientX - r.left + 316}px`;
    tip.style.top = `${e.clientY - r.top + 14}px`;
  }
}

window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); }
  else if ((e.key === 'Delete' || e.key === 'Backspace') && state.sel) objectAction('remove');
  else if (e.key.toLowerCase() === 'p' && state.sel) setPlacing(!state.placing);
  else if (e.key.toLowerCase() === 'r' && state.sel) objectAction('rotate', { degrees: e.shiftKey ? -15 : 15 });
  else if (e.key === 'Escape') { if (state.placing) setPlacing(false); else select(null); }
});

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {
    state.tool = tab.dataset.tool;
    for (const t of document.querySelectorAll('.tab')) t.classList.toggle('active', t === tab);
    for (const t of document.querySelectorAll('.tool')) t.classList.toggle('hidden', t.id !== `tool-${state.tool}`);
    if (state.tool !== 'objects') select(null);
    // Sculpting and grabbing use the left button, so the camera moves to the right/middle buttons.
    controls.mouseButtons = ['brush', 'warp'].includes(state.tool)
      ? { LEFT: null, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.ROTATE }
      : { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
    updateBrush(null);
  });
}

$('autoDims').addEventListener('change', () => {
  $('dims').classList.toggle('disabled', $('autoDims').checked);
  for (const k of ['radius', 'edge', 'length', 'width', 'drop']) $(k).disabled = $('autoDims').checked;
});
$('autoDims').dispatchEvent(new Event('change'));
$('gameLook').addEventListener('change', () => { buildTerrain(); buildModels(); buildSky(); buildObjects(); updateFog(); });
$('showFog').addEventListener('change', updateFog);
$('showBoxes').addEventListener('change', buildObjects);
$('showObjects').addEventListener('change', () => { select(null); buildObjects(); buildModels(); });
$('showHelpers').addEventListener('change', () => { select(null); buildObjects(); buildModels(); });
$('showRails').addEventListener('change', buildOverlay);
$('showChanges').addEventListener('change', buildTerrain);
$('relief').addEventListener('change', buildTerrain);
$('applyRecipe').addEventListener('click', applyRecipe);
$('recipe').addEventListener('change', syncRecipeFields);
syncRecipeFields();
$('along').addEventListener('input', () => flyAlong(parseFloat($('along').value)));
$('toStart').addEventListener('click', () => flyAlong(0));
$('course').addEventListener('change', () => { select(null); state.framed = false; loadCourse($('course').value, false); });
$('objUp').addEventListener('click', () => objectAction('move', { delta: [0, 0, 100] }));
$('objDown').addEventListener('click', () => objectAction('move', { delta: [0, 0, -100] }));
$('objLeft').addEventListener('click', () => objectAction('rotate', { degrees: 15 }));
$('objRight').addEventListener('click', () => objectAction('rotate', { degrees: -15 }));
$('objPlace').addEventListener('click', () => setPlacing(!state.placing));
$('brushMode').addEventListener('change', syncBrushFields);
$('brushStrength').addEventListener('input', () => { $('brushStrengthValue').textContent = $('brushStrength').value; });
syncBrushFields();
$('objRemove').addEventListener('click', () => objectAction('remove'));
$('undo').addEventListener('click', undo);
$('save').addEventListener('click', save);

async function undo() {
  try {
    const res = await api('/api/undo', {});
    log(res.message, 'ok');
    await loadCourse(state.code, true);
  } catch (e) {
    log(e.message, 'err');
  }
}

async function save() {
  busy(true, 'saving the edited game… (copying the ISO can take a minute)');
  try {
    const res = await api('/api/save', { output: $('output').value });
    log(res.message, 'ok');
    $('play').disabled = false;
  } catch (e) {
    log(e.message, 'err');
  } finally {
    busy(false);
  }
}

// ---------------------------------------------------------------- start
const LABEL_RANGE = 600;     // metres; farther labels only clutter the view
function cullLabels() {
  for (const o of state.overlay.children) {
    if (o.isCSS2DObject) o.visible = o.position.distanceTo(camera.position) < (o.userData.far ? 3 : 1) * LABEL_RANGE;
  }
}

function animate() {
  requestAnimationFrame(animate);
  hover();
  cullLabels();
  state.sky.position.copy(camera.position);
  controls.update();
  renderer.clear();
  if (state.sky.children.length && $('showSky').checked) {
    renderer.render(skyScene, camera);
    renderer.clearDepth();
  }
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
}

// ---------------------------------------------------------------- choosing the game
let foundGames = [];

function gb(bytes) { return `${(bytes / 1073741824).toFixed(1)} GB`; }

function pickGame(path) {
  $('gamePath').value = path;
  for (const b of document.querySelectorAll('.game')) b.classList.toggle('selected', b.dataset.path === path);
  // Compare an edited disc with the untouched game by default.
  const chosen = foundGames.find((g) => g.path === path);
  const original = foundGames.find((g) => g.original && g.path !== path);
  $('comparePath').value = chosen && !chosen.original && original ? original.path : '';
}

async function showStart(closable) {
  $('startScreen').classList.remove('hidden');
  $('closeStart').classList.toggle('hidden', !closable);
  $('gameList').textContent = 'looking for games…';
  try {
    const res = await api('/api/games');
    foundGames = res.games;
    const list = $('gameList');
    list.innerHTML = '';
    if (!foundGames.length) list.textContent = 'No ISO found. Type its path below.';
    for (const g of foundGames) {
      const b = Object.assign(document.createElement('button'), { className: 'game' });
      b.dataset.path = g.path;
      b.append(Object.assign(document.createElement('b'), { textContent: g.name }));
      b.append(Object.assign(document.createElement('span'), { className: 'badge' + (g.original ? ' original' : ''),
        textContent: g.original ? 'original game' : 'edited' }));
      b.append(Object.assign(document.createElement('div'), { className: 'where',
        textContent: `${g.folder} · ${gb(g.size)} · ${new Date(g.modified * 1000).toLocaleString()}` }));
      b.addEventListener('click', () => pickGame(g.path));
      b.addEventListener('dblclick', () => { pickGame(g.path); openGame(); });
      list.append(b);
    }
    const cmp = $('comparePath');
    cmp.innerHTML = '';
    cmp.append(new Option('nothing (show only the edits made since opening)', ''));
    for (const g of foundGames) cmp.append(new Option(`${g.name}${g.original ? ' (original game)' : ''}`, g.path));
    if (foundGames.length) pickGame(foundGames[0].path);
    $('startMsg').textContent = res.pcsx2 ? `PCSX2: ${res.pcsx2}` : 'PCSX2 not found; start the game yourself.';
  } catch (e) {
    $('gameList').textContent = e.message;
  }
}

async function openGame() {
  const path = $('gamePath').value.trim();
  if (!path) return;
  $('startMsg').textContent = 'opening…';
  try {
    await api('/api/open', { path, compare: $('comparePath').value || null });
    location.reload();
  } catch (e) {
    $('startMsg').textContent = e.message;
  }
}

async function play() {
  try {
    const res = await api('/api/play', {});
    log(res.message, 'ok');
  } catch (e) {
    log(e.message, 'err');
  }
}

$('openGame').addEventListener('click', openGame);
$('refreshGames').addEventListener('click', () => showStart(!$('closeStart').classList.contains('hidden')));
$('closeStart').addEventListener('click', () => $('startScreen').classList.add('hidden'));
$('otherGame').addEventListener('click', () => showStart(true));
$('gamePath').addEventListener('keydown', (e) => { if (e.key === 'Enter') openGame(); });
$('play').addEventListener('click', play);

async function start() {
  resize();
  animate();
  try {
    const info = await api('/api/info');
    if (!info.loaded) { showStart(false); return; }
    $('gameName').textContent = info.source.split('/').pop() + (info.compare ? ` · compared with ${info.compare.split('/').pop()}` : '');
    $('play').disabled = !(info.saved || /\.iso$/i.test(info.source));
    $('output').value = info.output;
    for (const c of info.courses) {
      const opt = document.createElement('option');
      opt.value = c.code;
      opt.textContent = `${c.name} (${c.code})`;
      $('course').append(opt);
    }
    try {
      for (const r of await api('/api/recipes')) {
        const opt = document.createElement('option');
        opt.value = r.name;
        opt.textContent = `${r.title} (${r.steps} steps)`;
        opt.title = r.note;
        $('recipe').append(opt);
      }
    } catch (e) { log(e.message, 'err'); }
    const first = info.courses.find((c) => c.code === 'ARA1') || info.courses[0];
    if (!first) { log('no known course in the game data', 'err'); return; }
    $('course').value = first.code;
    log(`opened: ${info.source}`);
    await loadCourse(first.code, false);
  } catch (e) {
    log(e.message, 'err');
  }
}

window.ssxEditor = {                                          // for debugging and tests
  state, api, loadCourse, flyAlong, flyTo,
  project: (x, y, z) => new THREE.Vector3(x, y, z).project(camera),
};
start();
