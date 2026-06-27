/*
 * Voice Orb — dependency-free 2D canvas audio-reactive visualization.
 *
 * Adapted from the audio-reactive orb concept in the personal-use-licensed
 * "JARVIS" by Ethan Rogers (Three.js particle orb). This is an original 2D
 * Canvas reimplementation — no Three.js, no npm packages, no external scripts.
 *
 * API:
 *   const orb = createVoiceOrb(canvasEl);
 *   orb.setLevel(0..1);                          // amplitude drive
 *   orb.setState('idle'|'listening'|'speaking'); // behavior preset
 *   orb.start();                                  // begin animation loop
 *   orb.stop();                                   // halt loop / cleanup
 *
 * Everything is guarded so a missing canvas / 2D context never throws.
 */

function createVoiceOrb(canvas) {
  // ── No-op fallback so callers never have to null-check ────────────────────
  const noop = {
    setLevel() {},
    setState() {},
    start() {},
    stop() {},
  };
  if (!canvas || typeof canvas.getContext !== "function") return noop;

  let ctx;
  try {
    ctx = canvas.getContext("2d");
  } catch (_) {
    ctx = null;
  }
  if (!ctx) return noop;

  // ── Theme accent (read from CSS, fall back to dashboard cyan) ─────────────
  let accent = { r: 0, g: 200, b: 255 };
  try {
    const raw = getComputedStyle(document.documentElement)
      .getPropertyValue("--accent")
      .trim();
    const parsed = parseColor(raw);
    if (parsed) accent = parsed;
  } catch (_) {
    /* keep default */
  }

  const reduceMotion =
    typeof window !== "undefined" &&
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ── Geometry / DPR handling ───────────────────────────────────────────────
  let cssW = 0;
  let cssH = 0;
  let cx = 0;
  let cy = 0;
  let baseR = 0;

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = canvas.getBoundingClientRect();
    cssW = rect.width || canvas.clientWidth || 96;
    cssH = rect.height || canvas.clientHeight || 96;
    canvas.width = Math.max(1, Math.round(cssW * dpr));
    canvas.height = Math.max(1, Math.round(cssH * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx = cssW / 2;
    cy = cssH / 2;
    baseR = Math.min(cssW, cssH) * 0.28;
  }

  // ── Particles arranged in concentric rings ────────────────────────────────
  const RING_COUNT = 3;
  const PER_RING = 22;
  const particles = [];
  for (let ring = 0; ring < RING_COUNT; ring++) {
    for (let i = 0; i < PER_RING; i++) {
      particles.push({
        ring,
        angle: (i / PER_RING) * Math.PI * 2 + ring * 0.4,
        // each ring spins at a slightly different rate / direction
        speed: (0.12 + ring * 0.05) * (ring % 2 === 0 ? 1 : -1),
        phase: Math.random() * Math.PI * 2,
        size: 1.1 + Math.random() * 1.0,
      });
    }
  }

  // ── State ─────────────────────────────────────────────────────────────────
  let state = "idle";
  let rafId = 0;
  let running = false;
  let t = 0;

  let level = 0; // raw incoming amplitude 0..1
  let smoothLevel = 0; // smoothed for rendering
  let pulse = 0; // eased toward state intensity

  function stateIntensity() {
    switch (state) {
      case "speaking":
        return 0.85;
      case "listening":
        return 0.6;
      default:
        return 0.18; // idle ambient breathing
    }
  }

  function rgba(a) {
    return `rgba(${accent.r},${accent.g},${accent.b},${a})`;
  }

  // ── Single static frame (used for reduced-motion + initial paint) ─────────
  function drawStatic() {
    ctx.clearRect(0, 0, cssW, cssH);
    const r = baseR;

    // core glow
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, r * 2.2);
    grad.addColorStop(0, rgba(0.5));
    grad.addColorStop(0.4, rgba(0.16));
    grad.addColorStop(1, rgba(0));
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, r * 2.2, 0, Math.PI * 2);
    ctx.fill();

    // concentric rings
    for (let ring = 0; ring < RING_COUNT; ring++) {
      ctx.beginPath();
      ctx.arc(cx, cy, r * (0.5 + ring * 0.32), 0, Math.PI * 2);
      ctx.strokeStyle = rgba(0.28 - ring * 0.06);
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // particles at rest
    for (const p of particles) {
      const pr = r * (0.5 + p.ring * 0.32);
      const px = cx + Math.cos(p.angle) * pr;
      const py = cy + Math.sin(p.angle) * pr;
      ctx.beginPath();
      ctx.arc(px, py, p.size, 0, Math.PI * 2);
      ctx.fillStyle = rgba(0.6);
      ctx.fill();
    }

    // bright center
    ctx.beginPath();
    ctx.arc(cx, cy, Math.max(1.5, r * 0.12), 0, Math.PI * 2);
    ctx.fillStyle = rgba(0.9);
    ctx.fill();
  }

  // ── Animated frame ────────────────────────────────────────────────────────
  function frame() {
    if (!running) return;
    t += 0.016;

    // smooth the level + ease the state pulse so transitions are gentle
    smoothLevel += (level - smoothLevel) * 0.18;
    const target = stateIntensity();
    pulse += (target - pulse) * 0.05;

    // combined drive: state intensity + live amplitude, plus idle breathing
    const breathe = (Math.sin(t * 1.2) * 0.5 + 0.5) * 0.12;
    const drive = Math.min(1, pulse + smoothLevel * 0.9 + breathe * (1 - pulse));
    const jitter = smoothLevel * 0.9 + pulse * 0.25;

    ctx.clearRect(0, 0, cssW, cssH);

    const r = baseR * (1 + drive * 0.32);

    // ── Outer glow halo ──
    const glowR = r * 2.4;
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, glowR);
    grad.addColorStop(0, rgba(0.32 + drive * 0.4));
    grad.addColorStop(0.35, rgba(0.12 + drive * 0.16));
    grad.addColorStop(1, rgba(0));
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, glowR, 0, Math.PI * 2);
    ctx.fill();

    // ── Concentric pulsing rings ──
    ctx.lineWidth = 1;
    for (let ring = 0; ring < RING_COUNT; ring++) {
      const wobble = Math.sin(t * (1.4 + ring * 0.5)) * drive * 0.06;
      const rr = r * (0.5 + ring * 0.32) * (1 + wobble);
      ctx.beginPath();
      ctx.arc(cx, cy, rr, 0, Math.PI * 2);
      ctx.strokeStyle = rgba(0.18 + drive * 0.3 - ring * 0.04);
      ctx.stroke();
    }

    // ── Particles orbiting on their rings ──
    for (const p of particles) {
      p.angle += p.speed * 0.016 * (1 + drive * 1.5);
      const ringR = r * (0.5 + p.ring * 0.32);
      // radial jitter reacts to amplitude
      const wob = Math.sin(t * 3 + p.phase) * jitter * (r * 0.12);
      const pr = ringR + wob;
      const px = cx + Math.cos(p.angle) * pr;
      const py = cy + Math.sin(p.angle) * pr;
      const a = 0.4 + drive * 0.5;
      ctx.beginPath();
      ctx.arc(px, py, p.size * (1 + drive * 0.6), 0, Math.PI * 2);
      ctx.fillStyle = rgba(Math.min(1, a));
      ctx.fill();
    }

    // ── Bright reactive core ──
    const coreR = Math.max(1.5, r * (0.12 + drive * 0.1));
    const coreGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR * 2.5);
    coreGrad.addColorStop(0, rgba(0.95));
    coreGrad.addColorStop(0.5, rgba(0.5 + drive * 0.3));
    coreGrad.addColorStop(1, rgba(0));
    ctx.fillStyle = coreGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, coreR * 2.5, 0, Math.PI * 2);
    ctx.fill();

    rafId = window.requestAnimationFrame(frame);
  }

  // ── Public API ────────────────────────────────────────────────────────────
  function start() {
    resize();
    if (reduceMotion) {
      // static render only — mirror the bg-canvas reduced-motion guard
      drawStatic();
      return;
    }
    if (running) return;
    running = true;
    rafId = window.requestAnimationFrame(frame);
  }

  function stop() {
    running = false;
    if (rafId) {
      window.cancelAnimationFrame(rafId);
      rafId = 0;
    }
  }

  function onResize() {
    resize();
    if (reduceMotion || !running) drawStatic();
  }

  window.addEventListener("resize", onResize);

  // initial sizing + paint
  resize();
  drawStatic();

  return {
    setLevel(v) {
      const n = Number(v);
      level = Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : 0;
    },
    setState(s) {
      if (s === "idle" || s === "listening" || s === "speaking") {
        state = s;
        if (reduceMotion) drawStatic();
      }
    },
    start,
    stop,
    destroy() {
      stop();
      window.removeEventListener("resize", onResize);
    },
  };
}

// Parse "#rrggbb" / "#rgb" / "rgb(...)" into {r,g,b}; null if unrecognized.
function parseColor(str) {
  if (!str) return null;
  str = str.trim();
  let m = str.match(/^#([0-9a-f]{6})$/i);
  if (m) {
    const n = parseInt(m[1], 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  m = str.match(/^#([0-9a-f]{3})$/i);
  if (m) {
    const h = m[1];
    return {
      r: parseInt(h[0] + h[0], 16),
      g: parseInt(h[1] + h[1], 16),
      b: parseInt(h[2] + h[2], 16),
    };
  }
  m = str.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (m) {
    return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]) };
  }
  return null;
}

// Expose for the no-bundler dashboard.
if (typeof window !== "undefined") {
  window.createVoiceOrb = createVoiceOrb;
}
