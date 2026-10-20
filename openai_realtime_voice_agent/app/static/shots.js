// The camera: the shots you've lined up, and which one it uses when.
//
// Outside the editor the camera eases from shot to shot by itself, picking one from
// where she is and what's going on:
//   where     standing at home (the Default row), at one of the sit spots, or dancing
//   when      quiet, woken (listening or thinking), or talking
// The room editor's Camera section has the table of which shot for each, and the list
// of shots. A row left on "as Default" uses the Default row's shot.
//
// A shot is either fixed in the room (Wide: the view of the room) or follows her: kept
// relative to her, so it works wherever she stands or sits and whichever way she faces.
// You line it up with the editor's camera and click "Set to this view". Three come
// built in: Wide, Full body and Close-up; until you set one of them, it's worked out
// (Wide from in front of her, Full body and Close-up framing her body and her face, so
// a seated pose is framed as well as a standing one). Your own shots go after them.
//
// A gesture can ask for its own shot while it plays (its Camera in Moods & gestures).
//
// Kept in the layout: Wide's view in layout.camera, Full body's and Close-up's in
// layout.cameraShots, your own shots and the table in layout.shots.
//
// The parts, in order below:
//   shots     the list, and where each one puts the camera
//   choosing  which shot, from where she is and what's going on
//   moving    easing the camera to it
//   editor    the Camera section of the room editor
//
// The page starts it with createShots(page) and calls update(dt, now) every frame
// outside the editor. `page` gives it what it needs: see the comment at createShots.

import * as THREE from 'three';

// When: the columns of the table.
const WHEN = [
  ['quiet', 'Quiet'],
  ['listening', 'Woken'],
  ['talking', 'Talking'],
];
// What it does until you choose: the Default row, and other rows that differ from it.
const DEFAULT_ROW = { quiet: 'wide', listening: 'full', talking: 'close' };
const ROW_DEFAULTS = { dance: { listening: 'full', talking: 'full' } };   // the whole dance, even while she talks
const AS_DEFAULT = '';         // a row's choice to use the Default row's shot
const BACK_TO_QUIET_S = 12;    // after a conversation, the woken shot stays this long before the quiet one
const SHOT_MIN_S = 1.5;        // shortest time on a shot, so quick phase flips don't jerk it around
const EASE_RATE = 2.2;         // how quickly the camera eases to a new shot (per second)

// page: {
//   camera, orbit()      the camera; the editor's orbit controls (their target is where it looks)
//   active()             the character on screen ({ vrm, root, away, sitSpotId, move }), or null
//   layout()             the layout object (saved with Save room)
//   phase()              'idle', 'listening', 'thinking' or 'replying'
//   spots()              the layout's spots; spotLabel(spot) a spot's name for lists
//   headHeight(ch)       how high her head is above her feet
//   status(text)         a line under the Save button
// }
export function createShots(page) {
  const camera = page.camera;
  const layout = () => page.layout();

  // ---------- shots ----------
  const BUILT_IN = [
    { id: 'wide', name: 'Wide', follows: false },
    { id: 'full', name: 'Full body', follows: true },
    { id: 'close', name: 'Close-up', follows: true },
  ];
  const ownShots = () => layout().shots?.list ?? [];
  const allShots = () => [...BUILT_IN, ...ownShots()];
  const shotOf = id => allShots().find(s => s.id === id);
  // A shot's saved view ({ position, target }), or undefined: worked out.
  function viewOf(shot) {
    if (shot.id === 'wide') return layout().camera;
    if (shot.id === 'full' || shot.id === 'close') return layout().cameraShots?.[shot.id];
    return shot.view;
  }
  function setViewOf(shot, view) {
    if (shot.id === 'wide') layout().camera = view;
    else if (shot.id === 'full' || shot.id === 'close') {
      if (view) (layout().cameraShots ??= {})[shot.id] = view;
      else delete layout().cameraShots?.[shot.id];
    } else shot.view = view;
  }

  // Where a shot puts the camera, and what it looks at.
  function poseOf(id, outPos, outTarget) {
    const shot = shotOf(id) ?? BUILT_IN[0];
    const ch = page.active();
    const view = viewOf(shot);
    if (view && (!shot.follows || ch)) {
      outPos.fromArray(view.position);
      outTarget.fromArray(view.target);
      if (shot.follows) { ch.root.localToWorld(outPos); ch.root.localToWorld(outTarget); }
      return;
    }
    if (shot.id === 'full' || shot.id === 'close') framed(shot.id, ch, outPos, outTarget);
    else if (shot.id !== 'wide' && !view) poseOf('wide', outPos, outTarget);   // one of yours, not set yet
    else inFrontOf(ch, outPos, outTarget);
  }
  // Wide, until it's set: a little way in front of her, at about head height.
  function inFrontOf(ch, outPos, outTarget) {
    const p = ch ? ch.root.position : new THREE.Vector3(), h = ch ? page.headHeight(ch) : 1.5;
    outPos.set(p.x, p.y + h * 0.82, p.z + h * 1.55);
    outTarget.set(p.x, p.y + h * 0.72, p.z);
  }
  // Full body and Close-up, until they're set: framing her whole body, or her face,
  // from between her front and the wide view.
  function framed(id, ch, outPos, outTarget) {
    if (!ch) { inFrontOf(ch, outPos, outTarget); return; }
    const root = ch.root.position, h = page.headHeight(ch);
    const dir = new THREE.Vector3(0, 0, 1).applyQuaternion(ch.root.quaternion);
    const wide = layout().camera;
    if (wide) dir.lerp(new THREE.Vector3().fromArray(wide.position).sub(root).setY(0).normalize(), 0.5).normalize();
    const bone = name => ch.vrm.humanoid.getNormalizedBoneNode(name)?.getWorldPosition(new THREE.Vector3());
    let span;
    if (id === 'close') {
      outTarget.copy(bone('head') ?? new THREE.Vector3(root.x, root.y + h, root.z));
      outTarget.y += 0.02;
      span = 0.62;
    } else {
      // From her actual head and feet, so a seated pose is framed as well as a standing one.
      const head = bone('head'), hips = bone('hips'), feet = [bone('leftFoot'), bone('rightFoot')].filter(Boolean);
      if (head && hips && feet.length) {
        const top = head.y + 0.2 * ch.root.scale.y, bottom = Math.min(...feet.map(f => f.y)) - 0.05;
        outTarget.set(hips.x, (top + bottom) / 2, hips.z);
        span = Math.max(0.6, (top - bottom) * 1.15);
      } else {
        outTarget.set(root.x, root.y + h * 0.52, root.z);
        span = h * 1.3;
      }
    }
    const distance = (span / 2) / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    outPos.copy(outTarget).addScaledVector(dir, distance);
    outPos.y += span * 0.06;
  }

  // ---------- choosing ----------
  // Where she is: a row of the table ('default', a sit spot's id, or 'dance').
  function whereNow() {
    const ch = page.active();
    if (!ch || ch.move) return 'default';
    if (ch.away === 'sit' && ch.sitSpotId) return ch.sitSpotId;
    if (ch.away === 'dance') return 'dance';
    return 'default';
  }
  const ownTable = () => layout().shots?.use ?? {};
  // A row's own choice for a column ('' = as Default), as the editor shows it.
  function chosen(where, when) {
    const own = ownTable()[where]?.[when];
    if (own !== undefined) return own;
    if (where === 'default') return DEFAULT_ROW[when];
    return ROW_DEFAULTS[where]?.[when] ?? AS_DEFAULT;
  }
  // The shot used there and then.
  function shotFor(where, when) {
    const id = chosen(where, when);
    return id && shotOf(id) ? id : chosen('default', when) || DEFAULT_ROW[when];
  }

  let gestureShot = null, gestureShotUntil = 0;
  let lastWhen = 'quiet', lastPhase = 'idle', idleSince = -Infinity;
  function wantedShot(now) {
    if (gestureShot && now < gestureShotUntil) return gestureShot;
    const phase = page.phase();
    if (phase === 'idle' && lastPhase !== 'idle') idleSince = now;
    lastPhase = phase;
    const when = phase === 'replying' ? 'talking'
      : phase === 'listening' || phase === 'thinking' ? 'listening'
      : lastWhen !== 'quiet' && now - idleSince < BACK_TO_QUIET_S ? 'listening' : 'quiet';
    lastWhen = when;
    return shotFor(whereNow(), when);
  }
  // A gesture's own shot while it plays ('keep': the camera stays as it is).
  function frameGesture(id, seconds) {
    if (id === 'keep') return;
    gestureShot = id && shotOf(id) ? id : 'full';
    gestureShotUntil = performance.now() / 1000 + seconds;
  }

  // ---------- moving ----------
  const goalPos = new THREE.Vector3(), goalTarget = new THREE.Vector3(), lookAt = new THREE.Vector3();
  let shot = 'wide', shotSince = -Infinity, snapping = true;
  function update(dt, now) {
    const want = wantedShot(now);
    if (want !== shot && (snapping || now - shotSince >= SHOT_MIN_S)) { shot = want; shotSince = now; }
    poseOf(shot, goalPos, goalTarget);
    if (!shotOf(shot)?.follows) {   // a slow drift on a fixed shot, so the room doesn't look frozen
      goalPos.x += Math.sin(now * 0.07) * 0.08;
      goalPos.y += Math.sin(now * 0.05 + 1) * 0.03;
    }
    const k = snapping ? 1 : 1 - Math.exp(-dt * EASE_RATE);
    camera.position.lerp(goalPos, k);
    lookAt.lerp(goalTarget, k);
    camera.lookAt(lookAt);
    snapping = false;
  }
  // Cut straight to the current shot next frame (on load, a new character, leaving the editor).
  function snap() { snapping = true; }

  // ---------- editor ----------
  // The Camera section: pick where (Default, a sit spot, Dancing) and choose its shot
  // for each "when"; then the list of shots, each with buttons to look through it and
  // to set it to the editor camera's view.
  const section = document.getElementById('cameraEditor');
  let editingWhere = 'default';

  function drawEditor() {
    if (!section) return;
    section.innerHTML = '';
    drawWhichShot();
    drawShots();
  }
  function drawWhichShot() {
    const places = [
      ['default', 'Default (standing)'],
      ...page.spots().filter(s => s.type === 'sit').map(s => [s.id, page.spotLabel(s)]),
      ['dance', 'Dancing'],
    ];
    if (!places.some(([id]) => id === editingWhere)) editingWhere = 'default';
    section.append(
      heading('Which shot when'),
      row('Where', selectBox(places, editingWhere, v => { editingWhere = v; drawEditor(); })),
    );
    const options = [...(editingWhere === 'default' ? [] : [[AS_DEFAULT, 'as Default']]), ...allShots().map(s => [s.id, s.name])];
    for (const [when, label] of WHEN) {
      section.append(row(label, selectBox(options, chosen(editingWhere, when), id => {
        const use = (layout().shots ??= {}).use ??= {};
        (use[editingWhere] ??= {})[when] = id;
      })));
    }
    section.append(note('Woken: listening or thinking (and a little while after a conversation).'));
  }
  function drawShots() {
    section.append(heading('Shots'));
    for (const shot of allShots()) {
      const builtIn = BUILT_IN.includes(shot);
      const box = document.createElement('fieldset');
      box.className = 'touchKind';
      const name = builtIn
        ? Object.assign(document.createElement('span'), { textContent: shot.name, className: 'shotName' })
        : textBox(shot.name, 'Name', v => { shot.name = v; }, () => drawEditor());
      box.append(row(name, button('Look', () => lookThrough(shot.id)),
                     button('Set to this view', () => { setToView(shot); drawEditor(); })));
      const second = [];
      if (builtIn) second.push(Object.assign(document.createElement('span'), { textContent: shot.follows ? 'follows her' : 'fixed in the room' }));
      else second.push(selectBox([['room', 'fixed in the room'], ['her', 'follows her']], shot.follows ? 'her' : 'room', v => {
        shot.follows = v === 'her';
        shot.view = undefined;   // its view was kept the other way; set it again
        drawEditor();
      }));
      if (!viewOf(shot)) second.push(Object.assign(document.createElement('span'), { textContent: builtIn ? '· worked out' : '· not set yet', className: 'muted' }));
      else if (builtIn) second.push(button('Automatic', () => { setViewOf(shot, undefined); status(`${shot.name} is worked out again — remember to save.`); drawEditor(); }));
      if (!builtIn) second.push(button('✕', () => removeShot(shot)));
      box.append(row(...second));
      section.append(box);
    }
    section.append(row(button('+ Shot from this view', addShot)),
      note('Line the editor camera up, then "Set to this view". A shot that follows her keeps the angle it had on her ' +
           'when you set it, wherever she is: set one meant for a sit spot while she sits there ("Sit here" in Selected).'));
  }
  function lookThrough(id) {
    const pos = new THREE.Vector3(), target = new THREE.Vector3();
    poseOf(id, pos, target);
    camera.position.copy(pos);
    page.orbit().target.copy(target);
    page.orbit().update();
  }
  function setToView(shot) {
    const ch = page.active();
    if (shot.follows && !ch) return;
    const pos = camera.position.clone(), target = page.orbit().target.clone();
    if (shot.follows) { ch.root.worldToLocal(pos); ch.root.worldToLocal(target); }
    setViewOf(shot, { position: pos.toArray().map(round), target: target.toArray().map(round) });
    status(`${shot.name} set — remember to save.`);
  }
  function addShot() {
    const list = (layout().shots ??= {}).list ??= [];
    let n = list.length + 1;
    while (list.some(s => s.id === `shot${n}`)) n++;
    const shot = { id: `shot${n}`, name: `Shot ${n}`, follows: false };   // what you see is what you get
    list.push(shot);
    setToView(shot);
    drawEditor();
  }
  function removeShot(shot) {
    const list = layout().shots?.list ?? [];
    list.splice(list.indexOf(shot), 1);
    for (const row of Object.values(ownTable())) for (const when in row) if (row[when] === shot.id) row[when] = AS_DEFAULT;
    drawEditor();
  }
  const status = text => page.status(text);

  // Small builders for the section.
  function heading(text) { return Object.assign(document.createElement('h3'), { textContent: text }); }
  function note(text) { return Object.assign(document.createElement('p'), { textContent: text }); }
  function row(...parts) {
    const div = document.createElement('div');
    div.className = 'row';
    div.append(...parts);
    return div;
  }
  function button(text, onclick) { return Object.assign(document.createElement('button'), { textContent: text, onclick }); }
  function selectBox(options, value, onchange) {   // options: [[value, label]]
    const sel = document.createElement('select');
    for (const [v, label] of options) sel.append(Object.assign(document.createElement('option'), { value: v, textContent: label }));
    sel.value = value;
    sel.onchange = () => onchange(sel.value);
    return sel;
  }
  function textBox(value, placeholder, oninput, onchange) {
    const box = Object.assign(document.createElement('input'), { type: 'text', value, placeholder });
    box.oninput = () => oninput(box.value);
    if (onchange) box.onchange = onchange;
    return box;
  }
  const round = v => Math.round(v * 10000) / 10000;

  return {
    update,
    snap,
    frameGesture,
    // For the gesture's Camera choice in Moods & gestures: [[id, name]], and 'keep'.
    choices: () => [...allShots().map(s => [s.id, s.name]), ['keep', 'Camera stays']],
    wideTarget: () => { const p = new THREE.Vector3(), t = new THREE.Vector3(); poseOf('wide', p, t); return t; },
    editorChanged: drawEditor,   // the editor opened, or the spots changed
  };
}
