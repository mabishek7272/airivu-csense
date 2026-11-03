import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import * as THREE from "three";
import { prefersReducedMotion } from "./tokens";

export interface ParticleFieldHandle {
  /** Morphs the field toward the particle arrangement for this `data-scene` index (0-6). */
  setScene(index: number): void;
}

const PARTICLE_COUNT = 7000;
const FIELD_RADIUS = 2.3;
const TAU = Math.PI * 2;
const PALETTE: Array<[number, number, number]> = [
  [0.655, 0.545, 0.98],
  [0.486, 0.227, 0.929],
  [0.204, 0.827, 0.6],
  [0.92, 0.92, 0.96],
];

type Vec3Array = Float32Array;

/** Builds the 7 particle arrangements the field morphs between as the page scrolls
 *  (one per `data-scene` section, 0-6; scene 6 reuses scene 0's "eye" shape so the field
 *  settles back into its resting form for the closing CTA) - ported verbatim from the
 *  mockup's `buildShapes()`. */
function buildShapes(n: number): Vec3Array[] {
  const mk = (f: (i: number) => [number, number, number]): Vec3Array => {
    const a = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const p = f(i);
      a[i * 3] = p[0];
      a[i * 3 + 1] = p[1];
      a[i * 3 + 2] = p[2];
    }
    return a;
  };

  const eye = mk((i) => {
    const R = FIELD_RADIUS;
    const s = Math.floor(n * 0.55);
    if (i < s) {
      const k = (i + 0.5) / s;
      const y = 1 - 2 * k;
      const r = Math.sqrt(Math.max(0, 1 - y * y));
      const th = i * 2.399963;
      return [Math.cos(th) * r * R, y * R, Math.sin(th) * r * R];
    }
    const ring = Math.floor(Math.random() * 16);
    const rr = 0.42 + ring * 0.068 + Math.random() * 0.012;
    const th = Math.random() * TAU;
    const z = Math.sqrt(Math.max(0, R * R - rr * rr)) + 0.08;
    return [Math.cos(th) * rr, Math.sin(th) * rr, z];
  });

  const cubes = mk((i) => {
    const c = i % 4;
    const cx = (c - 1.5) * 2.5;
    const s = 0.78;
    let p: [number, number, number];
    if (Math.random() < 0.6) {
      const e = Math.floor(Math.random() * 12);
      const t = Math.random() * 2 - 1;
      const ax = Math.floor(e / 4);
      const a = e & 1 ? s : -s;
      const b = e & 2 ? s : -s;
      p = ax === 0 ? [t * s, a, b] : ax === 1 ? [a, t * s, b] : [a, b, t * s];
    } else {
      const f = Math.floor(Math.random() * 6);
      const u = (Math.round((Math.random() * 2 - 1) * 3) / 3) * s;
      const v = (Math.random() * 2 - 1) * s;
      const sg = f & 1 ? s : -s;
      const ax = f >> 1;
      p = ax === 0 ? [sg, u, v] : ax === 1 ? [u, sg, v] : [u, v, sg];
    }
    const ang = c * 0.7 + 0.5;
    const x = p[0] * Math.cos(ang) - p[2] * Math.sin(ang);
    const z = p[0] * Math.sin(ang) + p[2] * Math.cos(ang);
    return [x + cx, p[1] + (c % 2 ? 0.4 : -0.4), z];
  });

  const g = Math.ceil(Math.sqrt(n));
  const terrain = mk((i) => {
    const gx = i % g;
    const gz = Math.floor(i / g);
    const x = (gx / g - 0.5) * 18;
    const z = (gz / g - 0.5) * 14 - 2;
    return [x, -1.5 + 0.6 * Math.sin(x * 0.55) * Math.cos(z * 0.45) + 0.28 * Math.sin(x * 1.4 + z * 0.8), z];
  });

  const tunnel = mk(() => {
    let th: number;
    let z = -16 + Math.random() * 21;
    if (Math.random() < 0.55) {
      th = (Math.floor(Math.random() * 28) / 28) * TAU;
    } else {
      th = Math.random() * TAU;
      z = Math.round(z / 1.1) * 1.1;
    }
    const r = 2.7 + 0.15 * Math.sin(z * 1.5);
    return [Math.cos(th) * r, Math.sin(th) * r, z];
  });

  const knot = mk(() => {
    const t = Math.random() * TAU;
    const r = Math.cos(3 * t) + 2.2;
    const cx = r * Math.cos(2 * t) * 0.85;
    const cy = r * Math.sin(2 * t) * 0.85;
    const cz = -Math.sin(3 * t) * 0.85;
    const a = Math.random() * TAU;
    const s = 0.24 * Math.sqrt(Math.random());
    return [cx + Math.cos(a) * s, cy + Math.sin(a) * s, cz + Math.sin(a + 1) * s];
  });

  const globe = mk(() => {
    let lat: number;
    let lon: number;
    if (Math.random() < 0.5) {
      lat = ((Math.floor(Math.random() * 11) + 1) / 12) * Math.PI;
      lon = Math.random() * TAU;
    } else {
      lon = (Math.floor(Math.random() * 20) / 20) * TAU;
      lat = Math.random() * Math.PI;
    }
    const r2 = 2.5;
    return [Math.sin(lat) * Math.cos(lon) * r2, Math.cos(lat) * r2, Math.sin(lat) * Math.sin(lon) * r2];
  });

  return [eye, cubes, terrain, tunnel, knot, globe, eye];
}

/** The ambient particle field behind the page, morphing between arrangements as the
 *  reader scrolls through sections (see Landing3rdiPage's scroll handler, which calls
 *  `setScene` with whichever `data-scene` section is centered in the viewport). A fixed,
 *  full-viewport WebGL canvas - genuinely needs its own renderer/camera/rAF loop, which
 *  is why this isn't just a styled div like the rest of the page's decorative layers.
 *  Skips mounting entirely under `prefers-reduced-motion`. */
export const ParticleField = forwardRef<ParticleFieldHandle>(function ParticleField(_props, ref) {
  const hostRef = useRef<HTMLDivElement>(null);
  const targetSceneRef = useRef(0);

  useImperativeHandle(ref, () => ({
    setScene(index: number) {
      targetSceneRef.current = index;
    },
  }));

  useEffect(() => {
    const host = hostRef.current;
    if (!host || prefersReducedMotion()) return;

    const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(window.innerWidth, window.innerHeight);
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.1, 100);
    camera.position.z = 8;

    const shapes = buildShapes(PARTICLE_COUNT);
    const pos = new Float32Array(PARTICLE_COUNT * 3);
    const col = new Float32Array(PARTICLE_COUNT * 3);
    const jit = new Float32Array(PARTICLE_COUNT * 3);
    pos.set(shapes[0]);
    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const r = Math.random();
      const c = r < 0.6 ? PALETTE[0] : r < 0.8 ? PALETTE[1] : r < 0.93 ? PALETTE[2] : PALETTE[3];
      col.set(c, i * 3);
      jit[i * 3] = (Math.random() * 2 - 1) * 3;
      jit[i * 3 + 1] = (Math.random() * 2 - 1) * 3;
      jit[i * 3 + 2] = (Math.random() * 2 - 1) * 3;
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geometry.setAttribute("color", new THREE.BufferAttribute(col, 3));

    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 64;
    const ctx = canvas.getContext("2d")!;
    const gradient = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
    gradient.addColorStop(0, "rgba(255,255,255,1)");
    gradient.addColorStop(0.3, "rgba(255,255,255,.6)");
    gradient.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 64, 64);

    const material = new THREE.PointsMaterial({
      size: 0.06,
      vertexColors: true,
      map: new THREE.CanvasTexture(canvas),
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      opacity: 0.9,
    });
    const points = new THREE.Points(geometry, material);
    points.frustumCulled = false;
    const group = new THREE.Group();
    group.add(points);
    scene.add(group);

    const mouse = { x: 0, y: 0, tx: 0, ty: 0 };
    const onPointerMove = (e: PointerEvent) => {
      mouse.tx = (e.clientX / window.innerWidth) * 2 - 1;
      mouse.ty = (e.clientY / window.innerHeight) * 2 - 1;
    };
    const onResize = () => {
      renderer.setSize(window.innerWidth, window.innerHeight);
      camera.aspect = window.innerWidth / window.innerHeight;
      camera.updateProjectionMatrix();
    };
    window.addEventListener("pointermove", onPointerMove, { passive: true });
    window.addEventListener("resize", onResize);

    let morph = 0;
    let rx = 0;
    let raf = 0;
    const tiltX = [0, 0.35, 0.3, 0, 0.4, 0.25, 0];
    const sway = [0, 1, 1, 0.3, 1, 1, 0];

    const tick = (t: number) => {
      raf = requestAnimationFrame(tick);
      mouse.x += (mouse.tx - mouse.x) * 0.06;
      mouse.y += (mouse.ty - mouse.y) * 0.06;

      morph += (targetSceneRef.current - morph) * 0.045;
      if (Math.abs(targetSceneRef.current - morph) < 0.0005) morph = targetSceneRef.current;
      const a = Math.floor(morph);
      const b = Math.min(shapes.length - 1, a + 1);
      let f = morph - a;
      f = f * f * (3 - 2 * f);
      const shapeA = shapes[a];
      const shapeB = shapes[b];
      const sc = Math.sin(f * Math.PI) * 0.9;
      const w3 = (a === 3 ? 1 - f : 0) + (b === 3 ? f : 0);
      const dz = ((t * 0.0012) % 1.1) * w3;
      for (let i = 0; i < pos.length; i += 3) {
        pos[i] = shapeA[i] + (shapeB[i] - shapeA[i]) * f + jit[i] * sc;
        pos[i + 1] = shapeA[i + 1] + (shapeB[i + 1] - shapeA[i + 1]) * f + jit[i + 1] * sc;
        pos[i + 2] = shapeA[i + 2] + (shapeB[i + 2] - shapeA[i + 2]) * f + jit[i + 2] * sc + dz;
      }
      geometry.attributes.position.needsUpdate = true;

      const k = Math.round(morph);
      rx += ((tiltX[k] ?? 0) - rx) * 0.04;
      group.rotation.x = rx + mouse.y * 0.25;
      group.rotation.y = mouse.x * 0.5 + Math.sin(t * 0.00022) * 0.35 * (sway[k] ?? 0) + (k === 4 || k === 5 ? t * 0.00012 : 0);
      group.rotation.z = k === 3 ? t * 0.00008 : 0;

      renderer.render(scene, camera);
    };
    raf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("resize", onResize);
      renderer.dispose();
      renderer.domElement.remove();
      geometry.dispose();
      material.dispose();
      material.map?.dispose();
    };
  }, []);

  return (
    <div
      ref={hostRef}
      style={{ position: "fixed", inset: 0, zIndex: 0, pointerEvents: "none" }}
    />
  );
});
