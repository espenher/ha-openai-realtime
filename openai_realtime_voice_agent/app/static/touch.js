// The hand you steer around her room, and her sense of touch.
//
// The 🖐 button brings up a hand that follows the mouse (or a finger) and hovers over
// whatever is under it. Press and hold to lower it onto that: a quick press is a POKE,
// held still is a TOUCH, held and moved is a STROKE.
//
// Her body has touch zones, each a bubble riding on one of her bones. They live in the
// layout (layout.touch.zones) and are edited in the room editor's Touch section, which
// this file also draws. For each kind of touch a zone says what it means to her: how
// she moves, the moods she goes through as it goes on, what she's told, and when she
// says something about it.
//
// The parts, in order below:
//   zones     the zone list (the layout's, or the defaults) and the bubbles on her bones
//   hand      the hand model, and steering it with the pointer
//   sensing   which zone the hand is on, and whether that's a poke, touch or stroke
//   readout   a line on screen saying what she feels (until the reactions are in)
//   editor    the Touch section of the room editor
//
// The page starts it with createTouch(page) and calls update(dt) every frame. `page`
// gives it what it needs from the page: see the comment at createTouch.

import * as THREE from 'three';

// ---------- settings ----------
const KINDS = ['poke', 'touch', 'stroke'];
const KIND_HELP = {
  poke: 'a quick press',
  touch: 'pressed and held still',
  stroke: 'pressed and moved about',
};
const MOTIONS = ['none', 'lean in', 'flinch'];
const POKE_MAX_S = 0.3;         // a press shorter than this is a poke
const STROKE_SPEED = 0.1;       // metres a second across the zone (at Size 1): faster is a stroke
const SPEED_OVER_S = 0.3;       // ...measured over this long
const REPEAT_WINDOW_S = 4;      // pokes on a zone closer together than this count as repeats
const HOVER_GAP = 0.05;         // metres the hand floats above what's under it
const TOUCH_GAP = 0.008;        // ...and when pressed down onto it
const FOLLOW_RATE = 18;         // how quickly the hand catches up with the pointer (per second)

// Where she can be touched, until the layout has its own list. Positions are from the
// bone, in her own directions at Size 1: x to her left, y up, z forward (metres).
// Moods are tags from the editor's Moods & gestures list; she goes through them in
// order as a touch goes on (every few seconds) or repeats (each poke).
const DEFAULT_ZONES = [
  zone('head', 'top of head', 'head', [0, 0.17, -0.02], 0.075, {
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
const copy = value => JSON.parse(JSON.stringify(value));

// page: {
//   scene, camera, canvas,
//   active()          the character on screen ({ vrm, root }), or null
//   layout()          the layout object (saved with Save room)
//   editing()         whether the room editor is open
//   moodTags()        the mood tags from the editor's Moods & gestures list
//   pickables()       objects the hand can rest on besides her (room, furniture)
//   registerEditorKind(kind, { label, moved, removed })  so the editor can select bubbles
//   selectInEditor(object)  select a bubble in the editor (gizmo on it)
// }
export function createTouch(page) {
  // ---------- zones ----------
  // The layout's list once it has one; until then the defaults (copied into the
  // layout as soon as anything is changed in the editor).
  let defaults = copy(DEFAULT_ZONES);
  const zones = () => page.layout().touch?.zones ?? defaults;
  function ownZones() {
    const layout = page.layout();
    layout.touch ??= {};
    layout.touch.zones ??= defaults;
    return layout.touch.zones;
  }

  // One bubble per zone, parented to its bone on the character on screen. They're
  // what the hand detects touches on, and what the editor shows and drags.
  const bubbleMaterial = new THREE.MeshBasicMaterial({ color: 0x66ccff, transparent: true, opacity: 0.25, depthTest: false });
  const bubbleGeometry = new THREE.SphereGeometry(1, 20, 14);
  let bubbles = [];             // [{ zone, mesh }]
  let builtFor = null, builtFrom = null;
  // A zone's position is kept in her own directions; a bubble sits in its bone's
  // frame. In the rest pose every normalized bone is lined up with the model, so the
  // turn from her directions to any bone's frame is the model's turn in her holder
  // (nothing for VRM 1.0, the 180° flip for VRM 0.x), whatever pose she's in now.

  function rebuildBubbles() {
    for (const { mesh } of bubbles) mesh.removeFromParent();
    bubbles = [];
    const ch = page.active();
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
  function restTurn(ch) {
    return ch.vrm.scene.getWorldQuaternion(new THREE.Quaternion()).invert()
      .multiply(ch.root.getWorldQuaternion(new THREE.Quaternion()));
  }
  function placeBubble(ch, z, mesh) {
    mesh.position.fromArray(z.offset).applyQuaternion(restTurn(ch));
    mesh.scale.setScalar(z.radius);
  }
  function bubbleOf(id) { return bubbles.find(b => b.zone.id === id); }

  // ---------- hand ----------
  const hand = buildHand();
  hand.visible = false;
  page.scene.add(hand);
  let handOn = false;
  const pointer = new THREE.Vector2();
  let pointerInside = false, pressed = false;
  const ray = new THREE.Raycaster();
  const handGoal = new THREE.Vector3(), handTurnGoal = new THREE.Quaternion();
  let under = null;             // what the hand is over: { zone, point, normal } (zone null for furniture)

  page.canvas.addEventListener('pointermove', e => {
    pointer.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
    pointerInside = true;
  });
  page.canvas.addEventListener('pointerleave', () => { pointerInside = false; pressed = false; });
  page.canvas.addEventListener('pointerdown', e => {
    if (!handOn || page.editing()) return;
    pointer.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
    pointerInside = true;
    pressed = true;
  });
  addEventListener('pointerup', () => { pressed = false; });

  function setHand(on) {
    handOn = on;
    hand.visible = false;
    document.body.classList.toggle('hand', on);
    if (!on) endContact();
  }

  // Where the hand goes this frame: onto the first thing under the pointer (a zone
  // bubble, the room, furniture), its palm facing that surface, fingers pointing away
  // from the camera.
  function steer(dt) {
    hand.visible = handOn && pointerInside && !page.editing();
    if (!hand.visible) { under = null; return; }
    ray.setFromCamera(pointer, page.camera);
    const targets = [...bubbles.map(b => b.mesh), ...page.pickables()];
    const hit = ray.intersectObjects(targets, true).find(h => h.face && isShown(h.object));
    if (hit) {
      const bubble = bubbles.find(b => b.mesh === hit.object);
      const normal = hit.face.normal.clone().transformDirection(hit.object.matrixWorld);
      if (normal.dot(ray.ray.direction) > 0) normal.negate();
      under = { zone: bubble?.zone ?? null, point: hit.point, normal };
    } else {
      under = { zone: null, point: ray.ray.at(1.5, new THREE.Vector3()), normal: new THREE.Vector3(0, 1, 0) };
    }
    const gap = (pressed ? TOUCH_GAP : HOVER_GAP) * (page.active()?.root.scale.y ?? 1);
    handGoal.copy(under.point).addScaledVector(under.normal, gap);
    handTurnGoal.copy(palmFacing(under.normal));
    const k = 1 - Math.exp(-dt * FOLLOW_RATE);
    hand.position.lerp(handGoal, hand.userData.placed ? k : 1);
    hand.quaternion.slerp(handTurnGoal, hand.userData.placed ? k : 1);
    hand.userData.placed = true;
  }
  const isShown = o => { for (let x = o; x; x = x.parent) if (!x.visible && !x.userData?.kind?.startsWith('touch')) return false; return true; };
  function palmFacing(normal) {
    // The palm is the hand's -y: it faces down onto the surface, fingers (-z) away from the camera.
    const up = normal.clone();
    const away = new THREE.Vector3();
    page.camera.getWorldDirection(away);
    away.addScaledVector(up, -away.dot(up));
    if (away.lengthSq() < 1e-6) away.set(0, 0, -1).addScaledVector(up, -up.z);
    away.normalize();
    const side = new THREE.Vector3().crossVectors(up, away.clone().negate());
    const m = new THREE.Matrix4().makeBasis(side, up, away.clone().negate());
    return new THREE.Quaternion().setFromRotationMatrix(m);
  }

  // ---------- sensing ----------
  // A contact is the hand pressed onto one zone. While it lasts it's a touch or a
  // stroke (by how fast the hand moves across the zone); let go quickly, it was a poke.
  const listeners = [];
  let contact = null;           // { zone, since, kind, trail: [{ t, at }] }  (at: in the bone's frame)
  const lastPokes = new Map();  // zone id → { at, count }

  function sense(now) {
    const zoneNow = pressed && hand.visible ? under?.zone ?? null : null;
    if (contact && contact.zone !== zoneNow) endContact(now);
    if (!zoneNow) return;
    const bubble = bubbleOf(zoneNow.id);
    const at = bubble.mesh.parent.worldToLocal(hand.position.clone());
    if (!contact) {
      contact = { zone: zoneNow, since: now, kind: null, trail: [] };
    }
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
    if (!contact.kind) {   // let go before it became a touch: a poke
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
  // The Touch section: the zones in a list, and the selected one's settings. Its
  // bubble is selected with it, for dragging (Move) and sizing (Scale) on her.
  const section = document.getElementById('touchEditor');
  let editingId = null;

  page.registerEditorKind('touchZone', {
    label: obj => `Touch zone: ${zones().find(z => z.id === obj.userData.id)?.name ?? ''}`,
    selected: obj => { editingId = obj.userData.id; drawEditor(); },
    moved: obj => {
      const z = ownZones().find(z => z.id === obj.userData.id);
      if (!z) return;
      const ch = page.active();
      // Scale sets the radius from whichever axis was dragged (the bubble stays round);
      // the position goes back into her directions.
      const dragged = [obj.scale.x, obj.scale.y, obj.scale.z].reduce((a, b) => Math.abs(b - z.radius) > Math.abs(a - z.radius) ? b : a);
      z.radius = round(Math.max(0.01, dragged));
      obj.scale.setScalar(z.radius);
      z.offset = obj.position.clone().applyQuaternion(restTurn(ch).invert()).toArray().map(round);
      drawEditor();
    },
    removed: obj => removeZone(obj.userData.id),
  });

  function drawEditor() {
    if (!section) return;
    const list = zones();
    const current = list.find(z => z.id === editingId);
    section.innerHTML = '';
    section.append(
      row(selectBox(['—', ...list.map(z => z.name)], current?.name ?? '—', name => {
        const z = list.find(z => z.name === name);
        editingId = z?.id ?? null;
        drawEditor();
        if (z) page.selectInEditor(bubbleOf(z.id)?.mesh ?? null);
      }), button('+ Zone', addZone)),
      row(button('Reset to defaults', () => {
        if (page.layout().touch) delete page.layout().touch.zones;
        defaults = copy(DEFAULT_ZONES);
        editingId = null;
        rebuildBubbles();
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
      row('On bone', selectBox(boneNames, z.bone, v => { z.bone = v; rebuildBubbles(); page.selectInEditor(bubbleOf(z.id)?.mesh ?? null); })),
      row('Size', numberBox(z.radius, 0.01, v => { z.radius = v; const b = bubbleOf(z.id); if (b) b.mesh.scale.setScalar(v); }), 'm'),
      button('Remove zone', () => removeZone(z.id)),
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
  function addZone() {
    const list = ownZones();
    let n = list.length + 1;
    while (list.some(z => z.id === `zone${n}`)) n++;
    const z = zone(`zone${n}`, `new zone ${n}`, 'head', [0, 0.1, 0.1], 0.05, copy(DEFAULT_ZONES[0].reactions));
    list.push(z);
    editingId = z.id;
    rebuildBubbles();
    drawEditor();
    page.selectInEditor(bubbleOf(z.id)?.mesh ?? null);
  }
  function removeZone(id) {
    const list = ownZones();
    const i = list.findIndex(z => z.id === id);
    if (i >= 0) list.splice(i, 1);
    editingId = null;
    page.selectInEditor(null);
    rebuildBubbles();
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
    if (page.active() !== builtFor || zones() !== builtFrom) rebuildBubbles();
    const showBubbles = page.editing();
    for (const { mesh } of bubbles) mesh.visible = showBubbles;
    steer(dt);
    sense(now);
    readout.style.opacity = handOn && now < readoutUntil ? 1 : 0;
  }

  return {
    update,
    setHand,
    get handOn() { return handOn; },
    onFeel: listen => listeners.push(listen),   // (event) → { zone, kind, phase, seconds | repeats }
    editorOpened: drawEditor,
  };
}

// A cartoon hand built from simple shapes (a right hand, palm down, fingers pointing
// along -z). Swappable for a proper model later.
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
