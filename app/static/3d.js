/* 3D 持仓星图 —— three.js r128 */
/* global THREE */

const COL = {
  pos: 0x3ddc97,   // 利好
  neg: 0xff5d5d,   // 利空
  neu: 0x7f92ad,   // 中性
  axisX: 0xe8674f,
  axisY: 0x4fa3e8,
  axisZ: 0x59d39a,
};

const state = { scene: null, camera: null, renderer: null, controls: null,
                spheres: [], raycaster: new THREE.Raycaster(), mouse: new THREE.Vector2(),
                hovered: null, autoRotate: true, data: null };

/* ---------------- 工具 ---------------- */
function zscores(vals) {
  const n = vals.length;
  const mu = vals.reduce((a, b) => a + b, 0) / (n || 1);
  const sd = Math.sqrt(vals.reduce((a, b) => a + (b - mu) ** 2, 0) / (n || 1)) || 1;
  return vals.map(v => (v - mu) / sd);
}
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function makeLabel(text, color = "#93a3bb", size = 34) {
  const pad = 8;
  const cv = document.createElement("canvas");
  const ctx = cv.getContext("2d");
  ctx.font = `600 ${size}px "Microsoft YaHei",system-ui,sans-serif`;
  const w = Math.ceil(ctx.measureText(text).width) + pad * 2;
  cv.width = w; cv.height = size * 1.6;
  const c2 = cv.getContext("2d");
  c2.font = `600 ${size}px "Microsoft YaHei",system-ui,sans-serif`;
  c2.fillStyle = color;
  c2.textBaseline = "middle";
  c2.fillText(text, pad, cv.height / 2);
  const tex = new THREE.CanvasTexture(cv);
  tex.minFilter = THREE.LinearFilter;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
  const sp = new THREE.Sprite(mat);
  const hh = 5.2;                       // 世界坐标里的字高
  sp.scale.set(cv.width / cv.height * hh, hh, 1);
  return sp;
}

let HALO_TEX = null;
function haloTexture() {
  if (HALO_TEX) return HALO_TEX;
  const s = 128;
  const cv = document.createElement("canvas");
  cv.width = cv.height = s;
  const ctx = cv.getContext("2d");
  const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  g.addColorStop(0.00, "rgba(255,255,255,0.85)");
  g.addColorStop(0.28, "rgba(255,255,255,0.30)");
  g.addColorStop(0.55, "rgba(255,255,255,0.09)");
  g.addColorStop(1.00, "rgba(255,255,255,0.00)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, s, s);
  HALO_TEX = new THREE.CanvasTexture(cv);
  return HALO_TEX;
}

/* ---------------- 场景 ---------------- */
function init() {
  const host = document.getElementById("app");
  const W = () => host.clientWidth, H = () => host.clientHeight;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x070a10);
  scene.fog = new THREE.FogExp2(0x070a10, 0.0030);

  const camera = new THREE.PerspectiveCamera(45, W() / H(), 0.1, 2000);
  camera.position.set(155, 105, 175);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(W(), H());
  host.appendChild(renderer.domElement);

  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 55;
  controls.maxDistance = 460;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.55;
  controls.addEventListener("start", () => { controls.autoRotate = false; state.autoRotate = false; });

  // 灯光
  scene.add(new THREE.AmbientLight(0x9fb4d6, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 0.85); key.position.set(90, 130, 80); scene.add(key);
  const rim = new THREE.PointLight(0x4a7bff, 0.9, 600); rim.position.set(-120, 60, -90); scene.add(rim);
  const warm = new THREE.PointLight(0xff7a4a, 0.55, 600); warm.position.set(120, -40, 110); scene.add(warm);

  // 网格地板
  const grid = new THREE.GridHelper(320, 32, 0x2a3b5c, 0x1a2538);
  grid.position.y = -60;
  scene.add(grid);

  // 星尘背景
  const g = new THREE.BufferGeometry();
  const N = 1400, pos = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {
    const r = 300 + Math.random() * 420;
    const t = Math.random() * Math.PI * 2, p = Math.acos(2 * Math.random() - 1);
    pos[i * 3] = r * Math.sin(p) * Math.cos(t);
    pos[i * 3 + 1] = r * Math.cos(p) * 0.6;
    pos[i * 3 + 2] = r * Math.sin(p) * Math.sin(t);
  }
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  scene.add(new THREE.Points(g, new THREE.PointsMaterial({
    color: 0x8aa0c8, size: 2.2, transparent: true, opacity: 0.55, sizeAttenuation: true })));

  // 坐标轴
  const L = 78;
  const axes = [
    [new THREE.Vector3(1, 0, 0), COL.axisX, "价值 →", new THREE.Vector3(L + 10, 0, 0)],
    [new THREE.Vector3(0, 1, 0), COL.axisY, "动量 ↑", new THREE.Vector3(0, L + 10, 0)],
    [new THREE.Vector3(0, 0, 1), COL.axisZ, "低风险 ↗", new THREE.Vector3(0, 0, L + 10)],
  ];
  axes.forEach(([dir, color, label, labelPos]) => {
    const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.55 });
    const geo = new THREE.BufferGeometry().setFromPoints([
      dir.clone().multiplyScalar(-L), dir.clone().multiplyScalar(L)]);
    scene.add(new THREE.Line(geo, mat));
    const sp = makeLabel(label, "#" + color.toString(16), 30);
    sp.position.copy(labelPos);
    scene.add(sp);
  });

  state.scene = scene; state.camera = camera;
  state.renderer = renderer; state.controls = controls;

  addEventListener("resize", () => {
    camera.aspect = W() / H();
    camera.updateProjectionMatrix();
    renderer.setSize(W(), H());
  });
  renderer.domElement.addEventListener("pointermove", onMove);
  renderer.domElement.addEventListener("pointerleave", () => hideTip());

  animate();
}

/* ---------------- 数据 ---------------- */
async function load() {
  let sig;
  try {
    sig = await (await fetch("/api/signal")).json();
  } catch (e) {
    return showEmpty("读取信号失败：" + e.message);
  }
  if (!sig || !sig.picks || !sig.picks.length) {
    return showEmpty("还没有选股信号<br>先回到「选股信号」页点一次「生成信号」");
  }
  state.data = sig;
  build(sig);
}

function showEmpty(html) {
  document.getElementById("loading").style.display = "none";
  const d = document.createElement("div");
  d.className = "empty";
  d.innerHTML = `<div style="font-size:30px">✦</div><div>${html}</div>`;
  document.getElementById("app").appendChild(d);
}

function build(sig) {
  const picks = sig.picks.filter(p => p.factors);
  if (!picks.length) return showEmpty("信号里没有因子数据");

  const fx = k => picks.map(p => Number(p.factors[k] ?? 0));
  const z = {
    ep: zscores(fx("ep")), bp: zscores(fx("bp")),
    mom: zscores(fx("momentum")),
    vol: zscores(fx("lowvol")), turn: zscores(fx("turnover")),
  };
  const S = 46;                      // 坐标缩放
  const maxW = Math.max(...picks.map(p => p.weight || 0)) || 1;

  const group = new THREE.Group();
  state.scene.add(group);
  state.group = group;

  picks.forEach((p, i) => {
    const value = (z.ep[i] + z.bp[i]) / 2;
    const risk = -(z.vol[i] + z.turn[i]) / 2;
    const x = clamp(value, -2.4, 2.4) * S;
    const y = clamp(z.mom[i], -2.4, 2.4) * S;
    const zz = clamp(risk, -2.4, 2.4) * S;

    const nz = p.news ? Number(p.news.z) : 0;
    const color = !p.news ? COL.neu : (nz > 0.6 ? COL.pos : nz < -0.6 ? COL.neg : COL.neu);
    const r = 2.0 + 3.4 * Math.sqrt((p.weight || 0) / maxW) + Math.min(Math.abs(p.score || 0), 1.6) * 0.8;

    const geo = new THREE.SphereGeometry(r, 32, 24);
    const mat = new THREE.MeshStandardMaterial({
      color, emissive: color, emissiveIntensity: 0.95,
      roughness: 0.28, metalness: 0.35,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set(x, y, zz);
    mesh.userData = { pick: p, baseColor: color, baseR: r };

    // 光晕
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({
      map: haloTexture(), color, transparent: true, opacity: 0.55,
      depthWrite: false, blending: THREE.AdditiveBlending,
    }));
    halo.scale.set(r * 7.5, r * 7.5, 1);
    mesh.add(halo);
    mesh.userData.halo = halo;

    // 到底面的虚线（增强空间感）
    const lineG = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(x, y, zz), new THREE.Vector3(x, -60, zz)]);
    const line = new THREE.Line(lineG, new THREE.LineBasicMaterial({
      color, transparent: true, opacity: 0.10 }));
    group.add(line);

    group.add(mesh);
    state.spheres.push(mesh);
  });

  document.getElementById("loading").style.display = "none";
  const nPos = picks.filter(p => p.news && p.news.z > 0.6).length;
  const nNeg = picks.filter(p => p.news && p.news.z < -0.6).length;
  document.getElementById("sub").textContent =
    `${sig.date} ｜ ${picks.length} 只 ｜ 利好 ${nPos} · 利空 ${nNeg} ｜ 综合分 ${picks[0].score}`;
}

/* ---------------- 交互 ---------------- */
function onMove(e) {
  const rect = state.renderer.domElement.getBoundingClientRect();
  state.mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  state.mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  state.raycaster.setFromCamera(state.mouse, state.camera);
  const hits = state.raycaster.intersectObjects(state.spheres, false);
  if (hits.length) {
    const m = hits[0].object;
    if (state.hovered !== m) {
      if (state.hovered) scaleBack(state.hovered);
      state.hovered = m;
      m.scale.setScalar(1.25);
      if (m.userData.halo) m.userData.halo.material.opacity = 0.85;
    }
    showTip(m.userData.pick, e.clientX, e.clientY);
    state.renderer.domElement.style.cursor = "pointer";
  } else {
    if (state.hovered) scaleBack(state.hovered);
    state.hovered = null;
    hideTip();
    state.renderer.domElement.style.cursor = "grab";
  }
}

function scaleBack(m) {
  m.scale.setScalar(1);
  if (m.userData.halo) m.userData.halo.material.opacity = 0.55;
}

function showTip(p, cx, cy) {
  const tip = document.getElementById("tip");
  const nw = p.news;
  const nl = nw ? (nw.z > 0.6 ? "利好" : nw.z < -0.6 ? "利空" : "中性") : "—";
  const ncolor = nw ? (nw.z > 0.6 ? "#3ddc97" : nw.z < -0.6 ? "#ff5d5d" : "#7f92ad") : "#7f92ad";
  tip.innerHTML = `
    <div class="n">${p.name}</div>
    <div class="c">${p.code} · ${p.industry}</div>
    <div class="r"><span>权重</span><span>${(p.weight * 100).toFixed(2)}%</span></div>
    <div class="r"><span>综合分</span><span>${(p.score ?? 0).toFixed(3)}</span></div>
    <div class="r"><span>参考价</span><span>${p.price ?? "—"}</span></div>
    <div class="r"><span>新闻</span><span style="color:${ncolor}">${nl} ${nw ? (nw.z > 0 ? "+" : "") + nw.z.toFixed(2) : ""}</span></div>`;
  tip.style.display = "block";
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = cx + 16, y = cy + 14;
  if (x + w > innerWidth - 12) x = cx - w - 16;
  if (y + h > innerHeight - 12) y = cy - h - 14;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}

function hideTip() { document.getElementById("tip").style.display = "none"; }

/* ---------------- 渲染 ---------------- */
function animate() {
  requestAnimationFrame(animate);
  if (state.group) {
    const t = performance.now() * 0.001;
    state.spheres.forEach((m, i) => {
      if (m === state.hovered) return;
      m.position.y += Math.sin(t * 0.9 + i) * 0.012;
    });
  }
  state.controls.update();
  state.renderer.render(state.scene, state.camera);
}

init();
load();
