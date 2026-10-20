// The hand you steer around her room, and her sense of touch.
//
// The 🖐 button (or H) brings up a hand you fly about: the mouse moves it across the
// screen, the scroll wheel nearer and further. A shadow under it shows how high it is.
// While it's out the page has the mouse to itself (pointer lock, like a browser game),
// so the hand can go on moving where the cursor would hit the edge; Esc lets go.
// She's solid to it: whatever part of her is in front of the hand stops it (an invisible
// shell measured from the model), so scrolling in presses it onto her, and moving it
// then rides it over her shape. Resting on her is touching her; a click is a quick pat.
//
// Her body has touch zones, each a bubble riding on one of her bones, marking the places
// that mean something: a touch counts for a zone when the hand is on her overlapping its
// bubble. Touching a zone is one of three kinds: a POKE (a quick touch, like a click's
// pat), a TOUCH (resting there) or a STROKE (moving about on it). For each kind a zone
// says what it means to her: how she moves, the moods she goes through, what she's told,
// and when she says something about it.
//
// The hand also pushes the springy parts of her model (hair, ribbons, tie...), the ones
// ticked in the editor.
//
// It's all kept in the layout (layout.touch) and edited in the room editor's Touch
// section, which this file draws.
//
// The parts, in order below:
//   zones     the zone list (the layout's, or the defaults) and the bubbles on her bones
//   shell     her body's shape, measured from the model: what the hand stops at
//   hand      the hand model and flying it about (mouse, scroll wheel, click to pat)
//   shadow    the spot under the hand that shows how high it is
//   springs   which of her springy parts the hand pushes
//   sensing   which zone the hand is on, and whether that's a poke, touch or stroke
//   readout   a line on screen saying what she feels (until the reactions are in)
//   editor    the Touch section of the room editor
//
// The page starts it with createTouch(page) and calls update(dt) every frame. `page`
// gives it what it needs from the page: see the comment at createTouch.

import * as THREE from 'three';
import { VRMSpringBoneCollider, VRMSpringBoneColliderShapeSphere } from '@pixiv/three-vrm';

// ---------- settings ----------
const KINDS = ['poke', 'touch', 'stroke'];
const KIND_HELP = {
  poke: 'a quick touch (a click pats)',
  touch: 'the hand resting there',
  stroke: 'moving about on it',
};
const MOTIONS = ['none', 'lean in', 'flinch'];

// Flying the hand (the hand's size and the scroll speed are editor settings).
const NEAREST = 0.25, FURTHEST = 6;  // metres from the camera the hand can be
const TURNS_TO_HER_WITHIN = 0.15;    // metres: nearer her than this, the palm turns towards her
const PRESS_DEPTH = 0.04;            // metres: scrolled into her, the hand presses on her (and rides over
                                     // her as it's moved) up to this much "in" before it stops going deeper
const FOLLOW_RATE = 20;              // how quickly the hand on screen catches up (per second)
const PAT_S = 0.3;                   // a click's pat: down and back up in this long...
const PAT_REACH = 0.08;              // ...reaching this far (metres) towards whatever is nearest

// Telling touches apart.
const TOUCHING_WITHIN = 0.015;  // metres between hand and skin that still counts as touching
const POKE_MAX_S = 0.3;         // a touch shorter than this is a poke
const STROKE_SPEED = 0.1;       // metres a second across the zone (at Size 1): faster is a stroke
const SPEED_OVER_S = 0.3;       // ...measured over this long
const REPEAT_WINDOW_S = 4;      // pokes on a zone closer together than this count as repeats
const KEEP_WITHIN = 1.5;        // a touch starts inside the bubble, then carries on until the hand
const KEEP_OUTSIDE_S = 0.25;    // is this many bubble sizes away, or outside it this long

// The springy parts of a model the hand may push, by their names in the model (and so
// the editor's tick list). Skirt and chest springs are left out on purpose.
const SPRING_REACH = 1.3;   // the hand pushes springs within this many times its own size

// Where she can be touched, until the layout has its own list. Positions are from the
// bone, in her own directions at Size 1: x to her left, y up, z forward (metres).
// Moods are tags from the editor's Moods & gestures list; she goes through them in
// order as a touch goes on (every few seconds) or repeats (each poke).
const DEFAULT_ZONES = [
  zone('head', 'top of head', 'head', [0, 0.17, 0], 0.09, {
    poke: { motion: 'none', moods: ['happy'], told: 'Espen patted your head', speakAfter: 4 },
    touch: { motion: 'lean in', moods: ['shy', 'happy'], told: 'Espen has a hand on your head', speakAfter: 0 },
    stroke: { motion: 'lean in', moods: ['shy', 'happy'], told: 'Espen is stroking your hair', speakAfter: 12 },
  }),
  ...['left', 'right'].map(side => zone(`${side}Cheek`, `${side} cheek`, 'head', [side === 'left' ? 0.04 : -0.04, 0.012, 0.075], 0.03, {
    poke: { motion: 'flinch', moods: ['surprised', 'annoyed'], told: 'Espen poked your cheek', speakAfter: 3 },
    touch: { motion: 'lean in', moods: ['shy'], told: 'Espen is touching your cheek', speakAfter: 0 },
    stroke: { motion: 'lean in', moods: ['shy', 'happy'], told: 'Espen is stroking your cheek', speakAfter: 10 },
  })),
  ...['left', 'right'].map(side => zone(`${side}Shoulder`, `${side} shoulder`, `${side}UpperArm`, [0, 0.03, 0], 0.06, {
    poke: { motion: 'flinch', moods: ['surprised'], told: 'Espen tapped your shoulder', speakAfter: 2 },
    touch: { motion: 'none', moods: [], told: 'Espen has a hand on your shoulder', speakAfter: 0 },
    stroke: { motion: 'none', moods: ['shy'], told: 'Espen is rubbing your shoulder', speakAfter: 0 },
  })),
  ...['left', 'right'].map(side => zone(`${side}Hand`, `${side} hand`, `${side}Hand`, [side === 'left' ? 0.05 : -0.05, 0, 0], 0.05, {
    poke: { motion: 'none', moods: ['surprised'], told: 'Espen poked your hand', speakAfter: 3 },
    touch: { motion: 'none', moods: ['shy'], told: 'Espen is holding your hand', speakAfter: 0 },
    stroke: { motion: 'none', moods: ['shy'], told: 'Espen is stroking your hand', speakAfter: 0 },
  })),
];
function zone(id, name, bone, offset, radius, reactions) {
  return { id, name, bone, offset, radius, reactions };
}
const DEFAULT_HAND = { size: 0.045, scrollSpeed: 1 };   // metres (at Size 1); 1 = normal
const copy = value => JSON.parse(JSON.stringify(value));

// page: {
//   scene, camera, canvas,
//   active()          the character on screen ({ vrm, root }), or null
//   layout()          the layout object (saved with Save room)
//   editing()         whether the room editor is open
//   moodTags()        the mood tags from the editor's Moods & gestures list
//   pickables()       the room and furniture (for placing the hand and its shadow)
//   registerEditorKind(kind, { label, selected, moved, removed })  so the editor can select bubbles
//   selectInEditor(object)  select a bubble in the editor (gizmo on it)
// }
export function createTouch(page) {
  // The layout's touch settings, with defaults for what it doesn't have yet. Anything
  // changed in the editor is written into the layout (and saved with Save room).
  const settings = () => page.layout().touch ?? {};
  function ownSettings() { return page.layout().touch ??= {}; }
  const handSettings = () => ({ ...DEFAULT_HAND, ...settings().hand });

  // ---------- zones ----------
  let defaults = copy(DEFAULT_ZONES);
  const zones = () => settings().zones ?? defaults;
  function ownZones() { return ownSettings().zones ??= defaults; }

  // One bubble per zone, parented to its bone on the character on screen: the editor
  // shows and drags them, and the hand's touches are looked up in them.
  const bubbleMaterial = new THREE.MeshBasicMaterial({ color: 0x66ccff, transparent: true, opacity: 0.25, depthTest: false });
  const bubbleGeometry = new THREE.SphereGeometry(1, 20, 14);
  let bubbles = [];             // [{ zone, mesh }]
  let builtFor = null, builtFrom = null;

  function rebuild() {
    for (const { mesh } of bubbles) mesh.removeFromParent();
    bubbles = [];
    const ch = page.active();
    if (ch !== builtFor) { rebuildShell(ch); rebuildSprings(ch); }
    builtFor = ch;
    builtFrom = zones();
    if (!ch) return;
    for (const z of builtFrom) {
      const bone = ch.vrm.humanoid.getNormalizedBoneNode(z.bone);
      if (!bone) continue;
      const mesh = new THREE.Mesh(bubbleGeometry, bubbleMaterial);
      mesh.renderOrder = 10;
      mesh.userData = { kind: 'touchZone', id: z.id };
      bone.add(mesh);
      placeBubble(ch, z, mesh);
      bubbles.push({ zone: z, mesh });
    }
  }
  // A zone's position is kept in her own directions; a bubble sits in its bone's frame.
  // In the rest pose every normalized bone is lined up with the model, so the turn from
  // her directions to any bone's frame is the model's turn in her holder (nothing for
  // VRM 1.0, the 180° flip for VRM 0.x), whatever pose she's in now.
  function restTurn(ch) {
    return ch.vrm.scene.getWorldQuaternion(new THREE.Quaternion()).invert()
      .multiply(ch.root.getWorldQuaternion(new THREE.Quaternion()));
  }
  function placeBubble(ch, z, mesh) {
    mesh.position.fromArray(z.offset).applyQuaternion(restTurn(ch));
    mesh.scale.setScalar(z.radius);
  }
  const bubbleOf = id => bubbles.find(b => b.zone.id === id);
  // How far into a zone the hand is, touching her at a point: below 1 inside, 1 at the
  // edge. The hand counts as in a zone when it overlaps the bubble (a palm is bigger
  // than a pinpoint): within the bubble's size plus half the hand's.
  function insideness(z, point) {
    const mesh = bubbleOf(z.id)?.mesh;
    if (!mesh) return Infinity;
    const center = mesh.getWorldPosition(new THREE.Vector3());
    return center.distanceTo(point) / (mesh.getWorldScale(new THREE.Vector3()).x + handRadius() / 2);
  }
  // The zone a point is in (the most central one, where bubbles overlap).
  function zoneAt(point) {
    let best = null, bestDepth = 1;
    for (const { zone: z } of bubbles) {
      const depth = insideness(z, point);
      if (depth <= bestDepth) { best = z; bestDepth = depth; }
    }
    return best;
  }

  // ---------- shell ----------
  // Her body's shape for the hand: a rounded tube along each limb and body bone and an
  // egg shape for her head, each as thick as the model is there (clothes too, but not
  // hair: a hand goes into hair down to her head, pushing the strands), measured once
  // per model. The hand stops at it, and the shadow falls on it.
  const SHELL_BONES = [   // [bone, the bone it reaches to]; null: an egg shape (the head)
    ['hips', 'spine'], ['spine', 'chest'], ['chest', 'neck'], ['neck', 'head'], ['head', null],
    ...['left', 'right'].flatMap(side => [
      [`${side}UpperArm`, `${side}LowerArm`], [`${side}LowerArm`, `${side}Hand`], [`${side}Hand`, `${side}MiddleProximal`],
      [`${side}UpperLeg`, `${side}LowerLeg`], [`${side}LowerLeg`, `${side}Foot`], [`${side}Foot`, `${side}Toes`],
    ]),
  ];
  const shellMaterial = new THREE.MeshBasicMaterial({ visible: false });
  const shellSizes = new Map();  // model uuid → [{ bone, to, radius } | { bone, center, size }] (bone frames, Size 1)
  let shell = [];                // [{ part, node, mesh }] on the character on screen

  function rebuildShell(ch) {
    for (const { mesh } of shell) mesh.removeFromParent();
    shell = [];
    if (!ch) return;
    if (!shellSizes.has(ch.vrm.scene.uuid)) shellSizes.set(ch.vrm.scene.uuid, measureShell(ch));
    for (const part of shellSizes.get(ch.vrm.scene.uuid)) {
      const node = ch.vrm.humanoid.getNormalizedBoneNode(part.bone);
      let mesh;
      if (part.to) {
        const along = new THREE.Vector3().fromArray(part.to);
        mesh = new THREE.Mesh(new THREE.CapsuleGeometry(part.radius, along.length(), 4, 10), shellMaterial);
        mesh.position.copy(along).multiplyScalar(0.5);
        mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), along.clone().normalize());
      } else {
        mesh = new THREE.Mesh(new THREE.SphereGeometry(1, 20, 14), shellMaterial);
        mesh.position.fromArray(part.center);
        mesh.scale.fromArray(part.size);
      }
      mesh.userData = { kind: 'touchShell' };
      node.add(mesh);
      shell.push({ part, node, mesh });
    }
  }
  // How thick she is around each bone: the skin (and clothes) mostly moved by that
  // bone, measured from the bone's line (most of the way out, not the furthest stray
  // point). The head is fitted to the outer edges of the head instead: the face has far
  // more points than the top of the head, which a "most of the way out" measure would
  // leave sticking out. In each bone's own frame, so the pose doesn't matter.
  function measureShell(ch) {
    const h = ch.vrm.humanoid;
    const parts = SHELL_BONES
      .map(([bone, to]) => ({ bone, to, raw: h.getRawBoneNode(bone), node: h.getNormalizedBoneNode(bone),
                              toNode: to && h.getNormalizedBoneNode(to) }))
      .filter(p => p.raw && p.node && (p.to === null || p.toNode));
    const byRaw = new Map(parts.map(p => [p.raw, { ...p, points: [] }]));
    ch.root.updateMatrixWorld(true);
    const v = new THREE.Vector3();
    ch.vrm.scene.traverse(m => {
      if (!m.isSkinnedMesh || !m.visible) return;
      const { skinIndex, skinWeight } = m.geometry.attributes;
      for (let i = 0; i < skinIndex.count; i += 3) {   // every third vertex is plenty
        let best = 0, bone = -1;
        for (let k = 0; k < 4; k++) if (skinWeight.getComponent(i, k) > best) { best = skinWeight.getComponent(i, k); bone = skinIndex.getComponent(i, k); }
        const part = byRaw.get(m.skeleton.bones[bone]);
        if (part) part.points.push(m.getVertexPosition(i, v).applyMatrix4(m.matrixWorld).clone());
      }
    });
    const sizes = [];
    for (const part of byRaw.values()) {
      if (part.points.length < 10) continue;
      const toLocal = part.node.matrixWorld.clone().invert();
      const local = part.points.map(q => q.applyMatrix4(toLocal));
      if (part.to) {
        const end = part.node.worldToLocal(part.toNode.getWorldPosition(new THREE.Vector3()));
        const line = new THREE.Line3(new THREE.Vector3(), end), closest = new THREE.Vector3();
        const radius = quantile(local.map(q => line.closestPointToPoint(q, true, closest).distanceTo(q)), 0.8);
        sizes.push({ bone: part.bone, to: end.toArray(), radius: Math.max(0.01, radius) });
      } else {
        const range = axis => [quantile(local.map(q => q[axis]), 0.01), quantile(local.map(q => q[axis]), 0.99)];
        const [x, y, z] = ['x', 'y', 'z'].map(range);
        sizes.push({ bone: part.bone, to: null,
                     center: [x, y, z].map(([lo, hi]) => (lo + hi) / 2),
                     size: [x, y, z].map(([lo, hi]) => Math.max(0.02, (hi - lo) / 2)) });
      }
    }
    return sizes;
  }
  const quantile = (values, q) => values.sort((a, b) => a - b)[Math.floor((values.length - 1) * q)];

  // The nearest point of her body to a point: { point, normal (outwards), distance }
  // (distance below 0 inside her). Worked out from the shapes, so it's cheap.
  function nearestOnHer(at) {
    let best = null;
    const local = new THREE.Vector3(), surface = new THREE.Vector3(), out = new THREE.Vector3();
    for (const { part, node } of shell) {
      node.worldToLocal(local.copy(at));
      if (part.to) {
        const end = new THREE.Vector3().fromArray(part.to);
        new THREE.Line3(new THREE.Vector3(), end).closestPointToPoint(local, true, surface);
        out.subVectors(local, surface);
        const fromAxis = out.length();
        if (fromAxis < 1e-6) out.set(0, 0, 1); else out.divideScalar(fromAxis);
        surface.addScaledVector(out, part.radius);
      } else {
        // An egg: squash it to a ball, find the point there, stretch back (close enough).
        const center = new THREE.Vector3().fromArray(part.center), size = new THREE.Vector3().fromArray(part.size);
        out.subVectors(local, center).divide(size);
        if (out.lengthSq() < 1e-9) out.set(0, 1, 0);
        out.normalize();
        surface.copy(out).multiply(size).add(center);
        out.divide(size).normalize();   // the egg's outward direction there
      }
      const inside = local.clone().sub(surface).dot(out) < 0;
      const worldSurface = node.localToWorld(surface.clone());
      const distance = worldSurface.distanceTo(at) * (inside ? -1 : 1);
      if (!best || distance < best.distance) {
        best = { point: worldSurface, normal: out.clone().transformDirection(node.matrixWorld), distance };
      }
    }
    return best;
  }

  // ---------- hand ----------
  const hand = buildHand();
  hand.visible = false;
  page.scene.add(hand);
  let handOn = false, pointerInside = false;
  const handAt = new THREE.Vector3();   // where you've flown it (the hand on screen follows, plus any pat)
  let pat = null;                       // a click's pat: { since }
  let lastPointer = null;
  const ray = new THREE.Raycaster();

  function setHand(on) {
    handOn = on;
    document.body.classList.toggle('hand', on);
    hand.userData.placed = false;
    lastPointer = null;
    if (on) lockMouse();
    else {
      endContact();
      if (document.pointerLockElement === page.canvas) document.exitPointerLock();
    }
  }
  // Pointer lock needs a click or key press to start, so it's asked for when the hand
  // comes out, and again on a click if the browser said no. Losing it (Esc) puts the
  // hand away.
  let wasLocked = false;
  function lockMouse() {
    try { page.canvas.requestPointerLock()?.catch?.(() => {}); } catch { /* asked again on a click */ }
  }
  const mouseLocked = () => document.pointerLockElement === page.canvas;
  document.addEventListener('pointerlockchange', () => {
    if (mouseLocked()) { wasLocked = true; return; }
    if (wasLocked && handOn) setHand(false);
    wasLocked = false;
  });
  const scale = () => page.active()?.root.scale.y ?? 1;
  const handRadius = () => handSettings().size * scale();

  // The mouse moves it across the screen (each pixel about a pixel's worth at the hand's
  // distance), the wheel nearer and further.
  page.canvas.addEventListener('pointermove', e => {
    pointerInside = true;
    if (!handOn || page.editing()) return;
    if (!hand.userData.placed) placeAtPointer(e);
    // Locked, the mouse's own movement; otherwise how far the cursor moved.
    const moved = mouseLocked() ? [e.movementX, e.movementY]
      : lastPointer ? [e.clientX - lastPointer[0], e.clientY - lastPointer[1]] : null;
    if (moved) {
      const [dx, dy] = moved;
      const distance = handAt.distanceTo(page.camera.position);
      const perPixel = 2 * distance * Math.tan(THREE.MathUtils.degToRad(page.camera.fov) / 2) / innerHeight;
      const right = new THREE.Vector3().setFromMatrixColumn(page.camera.matrixWorld, 0);
      const up = new THREE.Vector3().setFromMatrixColumn(page.camera.matrixWorld, 1);
      moveHandBy(right.multiplyScalar(dx * perPixel).addScaledVector(up, -dy * perPixel));
    }
    lastPointer = [e.clientX, e.clientY];
  });
  page.canvas.addEventListener('pointerleave', () => { if (!mouseLocked()) { pointerInside = false; lastPointer = null; } });
  page.canvas.addEventListener('wheel', e => {
    if (!handOn || page.editing()) return;
    e.preventDefault();
    // Wheel away from you pushes it away, along the line from your eye through the hand
    // (so it stays put on screen); the step grows with the distance, so it feels the
    // same near and far.
    const sight = handAt.clone().sub(page.camera.position);
    const distance = sight.length();
    moveHandBy(sight.normalize().multiplyScalar(-e.deltaY * 0.0012 * handSettings().scrollSpeed * distance));
  }, { passive: false });
  // Where you've moved it to; she's in the way of that each frame (see fly).
  function moveHandBy(delta) { handAt.add(delta); }
  page.canvas.addEventListener('pointerdown', e => {
    if (!handOn || page.editing() || e.button !== 0) return;
    if (!mouseLocked()) lockMouse();
    pat = { since: performance.now() / 1000 };
  });

  // Where it starts: just in front of whatever is under the pointer.
  function placeAtPointer(e) {
    const pointer = new THREE.Vector2((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
    ray.setFromCamera(pointer, page.camera);
    const hit = ray.intersectObjects([...shell.map(s => s.mesh), ...page.pickables()], true)[0];
    handAt.copy(hit ? hit.point.clone().addScaledVector(ray.ray.direction, -0.15) : ray.ray.at(1.5, new THREE.Vector3()));
    hand.position.copy(handAt);
    hand.userData.placed = true;
  }

  function fly(dt, now) {
    hand.visible = handOn && pointerInside && hand.userData.placed && !page.editing();
    if (!hand.visible) return null;
    // Keep it within reach of the camera.
    const camera = page.camera.position;
    const sight = handAt.clone().sub(camera);
    const depth = THREE.MathUtils.clamp(sight.length(), NEAREST, FURTHEST);
    sight.normalize();
    handAt.copy(camera).addScaledVector(sight, depth);
    // Solid: the part of her in front of the hand (along your line of sight) stops it
    // there. Scrolled in further, it's pressing on her, so moving it rides it over her;
    // it never gets more than PRESS_DEPTH "in", or moving off her edge would drop it
    // behind her. Nothing pulls it towards her otherwise.
    const shown = handAt.clone();
    if (page.active()) {
      ray.set(camera, sight);
      ray.far = depth + handRadius();
      const hit = ray.intersectObjects(shell.map(s => s.mesh), false)[0];
      ray.far = Infinity;
      const stop = hit ? hit.distance - handRadius() : Infinity;
      if (depth > stop) {
        shown.copy(camera).addScaledVector(sight, Math.max(NEAREST, stop));
        if (depth > stop + PRESS_DEPTH * scale()) handAt.copy(camera).addScaledVector(sight, stop + PRESS_DEPTH * scale());
      }
    }
    // She can also move into it herself: out to her surface then.
    let near = page.active() ? nearestOnHer(shown) : null;
    if (near && near.distance < handRadius()) {
      shown.copy(near.point).addScaledVector(near.normal, handRadius());
      near = nearestOnHer(shown);
    }
    // A click's pat: a quick dip towards her (or straight ahead), and back.
    if (pat) {
      const t = (now - pat.since) / PAT_S;
      if (t >= 1) pat = null;
      else {
        const toward = near && near.distance < PAT_REACH + handRadius() ? near.normal.clone().negate()
          : page.camera.getWorldDirection(new THREE.Vector3());
        const reach = near ? Math.min(PAT_REACH, Math.max(0, near.distance - handRadius() + 0.003)) : PAT_REACH;
        shown.addScaledVector(toward, reach * Math.sin(Math.PI * t));
      }
    }
    // Palm towards her when it's close to her, otherwise palm down.
    const facing = near && near.distance < TURNS_TO_HER_WITHIN + handRadius() ? near.normal : new THREE.Vector3(0, 1, 0);
    const k = 1 - Math.exp(-dt * FOLLOW_RATE);
    hand.position.lerp(shown, k);
    hand.quaternion.slerp(palmFacing(facing), k);
    hand.scale.setScalar(handSettings().size / DEFAULT_HAND.size * scale());
    return near;
  }
  function palmFacing(normal) {
    // The palm (the hand's -y) towards the surface. The fingers (-z) point up along an
    // upright surface, like a hand against glass, and away from the camera along a flat
    // one, blending between the two as the surface tilts.
    const up = normal.clone().normalize();
    const flat = page.camera.getWorldDirection(new THREE.Vector3());
    flat.addScaledVector(up, -flat.dot(up));
    const upright = new THREE.Vector3(0, 1, 0).addScaledVector(up, -up.y);
    const fingers = flat.add(upright);
    if (fingers.lengthSq() < 1e-6) fingers.set(0, 0, -1).addScaledVector(up, -up.z);
    fingers.normalize();
    const back = fingers.clone().negate();
    const side = new THREE.Vector3().crossVectors(up, back);
    return new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(side, up, back));
  }

  // ---------- shadow ----------
  // A soft dark spot on whatever is straight under the hand: smaller and darker the
  // closer the hand gets, which is how you can tell its height on a flat screen.
  const shadow = new THREE.Mesh(new THREE.CircleGeometry(1, 24),
    new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, depthWrite: false, opacity: 0.4 }));
  shadow.renderOrder = 4;
  page.scene.add(shadow);
  function castShadow() {
    shadow.visible = hand.visible;
    if (!hand.visible) return;
    ray.set(hand.position, new THREE.Vector3(0, -1, 0));
    const hit = ray.intersectObjects([...shell.map(s => s.mesh), ...page.pickables()], true).find(h => h.face);
    if (!hit) { shadow.visible = false; return; }
    const normal = (hit.normal ?? hit.face.normal).clone().transformDirection(hit.object.matrixWorld);
    if (normal.y < 0) normal.negate();
    shadow.position.copy(hit.point).addScaledVector(normal, 0.003);
    shadow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), normal);
    const height = hit.distance;
    shadow.scale.setScalar(handRadius() * (1 + height * 2.5));
    shadow.material.opacity = THREE.MathUtils.clamp(0.55 - height * 0.4, 0.08, 0.55);
  }

  // ---------- springs ----------
  // Her model's springy parts (hair, ribbons, tie...) bump off the hand, the groups
  // ticked in the editor. A group is what hangs from one "...Root" bone (Hair_FrontRoot:
  // "Hair Front"), or a chain of its own (TieRoot: "Tie").
  const handCollider = new VRMSpringBoneCollider(new VRMSpringBoneColliderShapeSphere({ radius: DEFAULT_HAND.size }));
  page.scene.add(handCollider);
  const handColliderGroup = { colliders: [handCollider], name: 'touch hand' };
  let springGroups = new Map();   // group name → [joint]

  function rebuildSprings(ch) {
    for (const joints of springGroups.values()) for (const j of joints) dropCollider(j);
    springGroups = new Map();
    if (!ch?.vrm.springBoneManager) return;
    const joints = [...ch.vrm.springBoneManager.joints];
    const springBones = new Set(joints.map(j => j.bone));
    const chainTop = j => { let b = j.bone; while (springBones.has(b.parent)) b = b.parent; return b; };
    for (const j of joints) {
      const name = springGroupName(chainTop(j));
      if (SPRINGS_LEFT_OUT.test(name)) continue;
      if (!springGroups.has(name)) springGroups.set(name, []);
      springGroups.get(name).push(j);
    }
  }
  function springGroupName(top) {
    const from = /root$/i.test(top.parent?.name ?? '') ? top.parent.name : top.name;
    return from.replace(/root$/i, '').replace(/[_.]?([LRC]|\d+)$/g, '').replace(/[_.]+/g, ' ').trim() || top.name;
  }
  const pushedGroups = () => settings().springs ?? [...springGroups.keys()];
  function updateSprings() {
    const pushed = new Set(handOn && hand.visible ? pushedGroups() : []);
    handCollider.position.copy(hand.position);
    handCollider.shape.radius = handRadius() * SPRING_REACH;   // in metres
    // updateWorldMatrix, not updateMatrixWorld: only it refreshes the matrix the springs
    // actually test against (the collider's colliderMatrix).
    handCollider.updateWorldMatrix(false, false);
    for (const [name, joints] of springGroups) {
      for (const j of joints) {
        if (pushed.has(name)) { if (!j.colliderGroups.includes(handColliderGroup)) j.colliderGroups.push(handColliderGroup); }
        else dropCollider(j);
      }
    }
  }
  function dropCollider(j) {
    const i = j.colliderGroups.indexOf(handColliderGroup);
    if (i >= 0) j.colliderGroups.splice(i, 1);
  }

  // ---------- sensing ----------
  // A contact is the hand on her inside a zone. It starts strictly inside the bubble,
  // then carries on while the hand stays near (a stroke runs over the edge now and
  // then). While it lasts it's a touch or a stroke, by how fast the hand moves across
  // the zone; over within POKE_MAX_S, it was a poke.
  const listeners = [];
  let contact = null;           // { zone, since, lastInside, kind, trail: [{ t, at }] }  (at: in the bone's frame)
  const lastPokes = new Map();  // zone id → { at, count }

  let lastSensed = null;   // for looking in from the console (touch.state)
  function sense(near, now) {
    const touching = near && hand.visible && hand.position.distanceTo(near.point) <= handRadius() + TOUCHING_WITHIN * scale() + 0.005;
    lastSensed = { near: near && { distance: +near.distance.toFixed(3), point: near.point.toArray().map(v => +v.toFixed(3)) },
                   handToPoint: near ? +hand.position.distanceTo(near.point).toFixed(3) : null, touching: !!touching,
                   zone: touching ? zoneAt(near.point)?.name ?? null : null, contact: contact?.zone.name ?? null };
    const point = touching ? near.point : null;
    if (contact) {
      const depth = point ? insideness(contact.zone, point) : Infinity;
      if (depth <= 1) contact.lastInside = now;
      const gone = depth > KEEP_WITHIN || now - contact.lastInside > KEEP_OUTSIDE_S;
      if (gone) endContact(now);
    }
    if (!contact && point) {
      const z = zoneAt(point);
      if (z) contact = { zone: z, since: now, lastInside: now, kind: null, trail: [] };
    }
    if (!contact || !point) return;
    const at = bubbleOf(contact.zone.id).mesh.parent.worldToLocal(point.clone());
    contact.trail.push({ t: now, at });
    while (contact.trail.length > 2 && now - contact.trail[0].t > SPEED_OVER_S) contact.trail.shift();
    if (now - contact.since < POKE_MAX_S) return;      // could still turn out to be a poke
    const kind = speedOf(contact.trail) > STROKE_SPEED ? 'stroke' : 'touch';
    if (kind !== contact.kind) {
      if (contact.kind) emit({ zone: contact.zone, kind: contact.kind, phase: 'end', seconds: now - contact.since });
      contact.kind = kind;
      emit({ zone: contact.zone, kind, phase: 'start', seconds: now - contact.since });
    } else {
      emit({ zone: contact.zone, kind, phase: 'going', seconds: now - contact.since });
    }
  }
  function endContact(now = performance.now() / 1000) {
    if (!contact) return;
    if (!contact.kind) {   // over before it became a touch: a poke
      const last = lastPokes.get(contact.zone.id);
      const count = last && now - last.at < REPEAT_WINDOW_S ? last.count + 1 : 1;
      lastPokes.set(contact.zone.id, { at: now, count });
      emit({ zone: contact.zone, kind: 'poke', phase: 'once', repeats: count });
    } else {
      emit({ zone: contact.zone, kind: contact.kind, phase: 'end', seconds: now - contact.since });
    }
    contact = null;
  }
  function speedOf(trail) {
    if (trail.length < 2) return 0;
    let travel = 0;
    for (let i = 1; i < trail.length; i++) travel += trail[i].at.distanceTo(trail[i - 1].at);
    const span = trail[trail.length - 1].t - trail[0].t;
    // Measured in the bone's own frame, which is at Size 1 whatever her Size.
    return span > 0 ? travel / span : 0;
  }
  function emit(event) { for (const listen of listeners) listen(event); }

  // ---------- readout ----------
  // Until her reactions are in, a line on screen says what she feels.
  const readout = document.createElement('div');
  readout.id = 'touchReadout';
  document.body.append(readout);
  let readoutUntil = 0;
  listeners.push(e => {
    const what = e.kind === 'poke' ? `poke${e.repeats > 1 ? ` ×${e.repeats}` : ''}` : `${e.kind} ${e.seconds.toFixed(1)} s${e.phase === 'end' ? ' (ended)' : ''}`;
    readout.textContent = `🖐 ${e.zone.name}: ${what}`;
    readoutUntil = performance.now() / 1000 + (e.phase === 'going' ? 0.5 : 2.5);
  });

  // ---------- editor ----------
  // The Touch section: the zones in a list and the selected one's settings (its bubble
  // is selected with it, for dragging with Move and sizing with Scale), what the hand
  // pushes, and the hand itself.
  const section = document.getElementById('touchEditor');
  let editingId = null;

  page.registerEditorKind('touchZone', {
    label: obj => `Touch zone: ${zones().find(z => z.id === obj.userData.id)?.name ?? ''}`,
    selected: obj => { editingId = obj.userData.id; drawEditor(); },
    moved: obj => {
      const z = ownZones().find(z => z.id === obj.userData.id);
      if (!z) return;
      // Scale sets the radius from whichever axis was dragged (the bubble stays round);
      // the position goes back into her directions.
      const dragged = [obj.scale.x, obj.scale.y, obj.scale.z].reduce((a, b) => Math.abs(b - z.radius) > Math.abs(a - z.radius) ? b : a);
      z.radius = round(Math.max(0.01, dragged));
      obj.scale.setScalar(z.radius);
      z.offset = obj.position.clone().applyQuaternion(restTurn(page.active()).invert()).toArray().map(round);
      drawEditor();
    },
    removed: obj => removeZone(obj.userData.id),
  });

  function drawEditor() {
    if (!section) return;
    section.innerHTML = '';
    drawZones();
    drawSprings();
    drawHand();
  }
  function drawZones() {
    const list = zones();
    const current = list.find(z => z.id === editingId);
    section.append(
      heading('Zones'),
      row(selectBox(['—', ...list.map(z => z.name)], current?.name ?? '—', name => {
        const z = list.find(z => z.name === name);
        editingId = z?.id ?? null;
        drawEditor();
        if (z) page.selectInEditor(bubbleOf(z.id)?.mesh ?? null);
      }), button('+ Zone', addZone)),
      row(button('Reset zones to defaults', () => {
        delete ownSettings().zones;
        defaults = copy(DEFAULT_ZONES);
        editingId = null;
        rebuild();
        drawEditor();
      })),
    );
    if (!current) {
      section.append(note('Pick a zone, or click its bubble on her. Move and Scale (in Selected) place and size it.'));
      return;
    }
    const z = ownZones().find(z => z.id === current.id);
    const ch = page.active();
    const boneNames = ch ? Object.keys(ch.vrm.humanoid.humanBones).filter(b => ch.vrm.humanoid.getNormalizedBoneNode(b)) : [z.bone];
    section.append(
      row(textBox(z.name, 'Name', v => { z.name = v; }, () => drawEditor())),
      row('On bone', selectBox(boneNames, z.bone, v => { z.bone = v; rebuild(); page.selectInEditor(bubbleOf(z.id)?.mesh ?? null); })),
      row('Size', numberBox(z.radius, 0.01, v => { z.radius = v; bubbleOf(z.id)?.mesh.scale.setScalar(v); }), 'm'),
      row(button('Remove zone', () => removeZone(z.id))),
    );
    for (const kind of KINDS) {
      const r = z.reactions[kind] ??= { motion: 'none', moods: [], told: '', speakAfter: 0 };
      const block = document.createElement('fieldset');
      block.className = 'touchKind';
      block.append(Object.assign(document.createElement('legend'), { textContent: `${kind} — ${KIND_HELP[kind]}` }));
      block.append(
        row('Moves', selectBox(MOTIONS, r.motion, v => { r.motion = v; })),
        row('Moods', textBox(r.moods.join(', '), 'e.g. shy, happy', v => { r.moods = v.split(',').map(s => s.trim()).filter(Boolean); }, null, 'touchMoods')),
        row(textBox(r.told, 'What she\'s told', v => { r.told = v; })),
        row('Says something after', numberBox(r.speakAfter, 1, v => { r.speakAfter = v; }), kind === 'poke' ? 'pokes' : 's', '(0 = never)'),
      );
      section.append(block);
    }
    moodSuggestions();
  }
  function drawSprings() {
    section.append(heading('The hand pushes'));
    if (!springGroups.size) { section.append(note('No springy parts on this model.')); return; }
    const pushed = new Set(pushedGroups());
    const boxes = document.createElement('div');
    boxes.className = 'row';
    for (const name of springGroups.keys()) {
      const label = document.createElement('label');
      const box = Object.assign(document.createElement('input'), { type: 'checkbox', checked: pushed.has(name) });
      box.onchange = () => {
        const now = new Set(pushedGroups());
        if (box.checked) now.add(name); else now.delete(name);
        ownSettings().springs = [...springGroups.keys()].filter(n => now.has(n));
      };
      label.append(box, ` ${name}`);
      boxes.append(label);
    }
    section.append(boxes, note('Her springy parts, as the model names them. Skirt and chest are left out.'));
  }
  function drawHand() {
    const h = handSettings();
    const set = (key, v) => { ownSettings().hand = { ...handSettings(), [key]: v }; };
    section.append(
      heading('The hand'),
      row('Size', numberBox(h.size, 0.005, v => set('size', Math.max(0.01, v))), 'm', '(for bumping into her and her hair)'),
      row('Scroll speed', numberBox(h.scrollSpeed, 0.1, v => set('scrollSpeed', v)), '(1 = normal)'),
    );
  }
  function addZone() {
    const list = ownZones();
    let n = list.length + 1;
    while (list.some(z => z.id === `zone${n}`)) n++;
    const z = zone(`zone${n}`, `new zone ${n}`, 'head', [0, 0.1, 0.1], 0.05, copy(DEFAULT_ZONES[0].reactions));
    list.push(z);
    editingId = z.id;
    rebuild();
    drawEditor();
    page.selectInEditor(bubbleOf(z.id)?.mesh ?? null);
  }
  function removeZone(id) {
    const list = ownZones();
    const i = list.findIndex(z => z.id === id);
    if (i >= 0) list.splice(i, 1);
    editingId = null;
    page.selectInEditor(null);
    rebuild();
    drawEditor();
  }
  // The moods boxes suggest the editor's mood tags.
  function moodSuggestions() {
    let list = document.getElementById('touchMoodTags');
    if (!list) { list = document.createElement('datalist'); list.id = 'touchMoodTags'; document.body.append(list); }
    list.innerHTML = page.moodTags().map(t => `<option value="${t}">`).join('');
    section.querySelectorAll('.touchMoods').forEach(box => box.setAttribute('list', 'touchMoodTags'));
  }

  // Small builders for the editor rows.
  function heading(text) { return Object.assign(document.createElement('h3'), { textContent: text }); }
  function row(...parts) {
    const div = document.createElement('div');
    div.className = 'row';
    for (const p of parts) div.append(typeof p === 'string' ? document.createTextNode(` ${p} `) : p);
    return div;
  }
  function note(text) { return Object.assign(document.createElement('p'), { textContent: text }); }
  function button(text, onclick) { return Object.assign(document.createElement('button'), { textContent: text, onclick }); }
  function selectBox(options, value, onchange) {
    const sel = document.createElement('select');
    for (const o of options) sel.append(Object.assign(document.createElement('option'), { value: o, textContent: o }));
    sel.value = value;
    sel.onchange = () => onchange(sel.value);
    return sel;
  }
  function textBox(value, placeholder, oninput, onchange, className) {
    const box = Object.assign(document.createElement('input'), { type: 'text', value, placeholder });
    if (className) box.className = className;
    box.oninput = () => oninput(box.value);
    if (onchange) box.onchange = onchange;
    return box;
  }
  function numberBox(value, step, oninput) {
    const box = Object.assign(document.createElement('input'), { type: 'number', value, step, min: 0, className: 'w' });
    box.oninput = () => { const v = Number(box.value); if (v >= 0) oninput(v); };
    return box;
  }
  const round = v => Math.round(v * 1000) / 1000;

  // ---------- every frame ----------
  function update(dt) {
    const now = performance.now() / 1000;
    if (page.editing() && mouseLocked()) document.exitPointerLock();
    if (page.active() !== builtFor || zones() !== builtFrom) rebuild();
    for (const { mesh } of bubbles) mesh.visible = page.editing();
    const near = fly(dt, now);
    castShadow();
    updateSprings();
    sense(near, now);
    readout.style.opacity = handOn && now < readoutUntil ? 1 : 0;
  }

  return {
    update,
    setHand,
    get handOn() { return handOn; },
    get state() { return lastSensed; },          // what the hand is near and touching (for checking)
    placeHandAt: point => { handAt.copy(point); hand.position.copy(point); hand.userData.placed = true; pointerInside = true; },   // (for checking)
    onFeel: listen => listeners.push(listen),   // (event) → { zone, kind, phase, seconds | repeats }
    editorOpened: drawEditor,
  };
}

// A cartoon hand built from simple shapes (a right hand, palm down, fingers pointing
// along -z), sized for DEFAULT_HAND.size. Swappable for a proper model later.
function buildHand() {
  // A little glow of its own, so it reads as a hand in a dim room.
  const skin = new THREE.MeshStandardMaterial({ color: 0xf2cdb8, roughness: 0.75, emissive: 0x5a3c30 });
  const hand = new THREE.Group();
  const palm = new THREE.Mesh(new THREE.SphereGeometry(1, 20, 12), skin);
  palm.scale.set(0.042, 0.014, 0.046);
  hand.add(palm);
  const finger = (x, length, z = -0.044) => {
    const g = new THREE.CapsuleGeometry(0.0085, length, 4, 10);
    g.rotateX(Math.PI / 2);
    const m = new THREE.Mesh(g, skin);
    m.position.set(x, 0, z - length / 2);
    hand.add(m);
  };
  finger(0.027, 0.042, -0.038);   // little finger
  finger(0.010, 0.052);
  finger(-0.008, 0.056);
  finger(-0.026, 0.050, -0.040);   // index
  const thumbGeometry = new THREE.CapsuleGeometry(0.0095, 0.038, 4, 10);
  thumbGeometry.rotateX(Math.PI / 2);
  const thumb = new THREE.Mesh(thumbGeometry, skin);
  thumb.position.set(-0.045, -0.004, -0.012);
  thumb.rotation.y = 0.75;
  hand.add(thumb);
  hand.traverse(o => { if (o.isMesh) o.renderOrder = 5; });
  return hand;
}
