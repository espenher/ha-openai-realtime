// The screen: a monitor in her room that shows what she sees through Espen's phone camera.
//
// When she looks (script.look_at_camera in Home Assistant saves three snapshots, about
// 0.3 s apart, to /media/look_1–3.jpg), the add-on tells the page about each new frame
// ({"type": "look", "frame": n, "v": <when it was taken>}) and serves it at /look/<n> for
// half a minute. Each frame goes on the screen as it arrives; the last one stays up for
// LOOK_HOLD_S and then fades back to the standby glow. While it shows, she looks at the
// screen (eyes and head).
//
// The room editor's Add section has "Add screen"; a screen is then selected, moved,
// turned and sized like furniture (Scale changes its size; it keeps its shape), and
// Remove takes it away. Kept in layout.screens: [{ id, position, rotation, size }],
// size being its width in metres. More than one screen all show the same.
//
// The page starts it with createScreen(page), calls update(dt, now) every frame and
// look(msg) for each "look" message. `page` gives it what it needs: see createScreen.

import * as THREE from 'three';

const ASPECT = 16 / 9;          // the screen's shape (width / height)
const BEZEL = 0.04;             // frame around the picture, as a share of the width
const DEFAULT_SIZE = 0.8;       // metres wide, for a new screen
const LOOK_HOLD_S = 8;          // how long the last frame stays up
const FADE_S = 1.2;             // fading back to standby
const STANDBY = 0x0e1a2b;       // the screen's glow when nothing is showing

// page: {
//   scene, layout(),
//   registerEditorKind(kind, handlers), selectInEditor(obj),
//   watch(point | null)   where she looks (eyes and head) while a frame shows; null when done
// }
export function createScreen(page) {
  const group = new THREE.Group();
  group.name = 'screens';
  page.scene.add(group);
  const screens = new Map();   // id → { root, picture }
  const loader = new THREE.TextureLoader();
  let texture = null, shownAt = -Infinity, opacity = 0;

  const list = () => (page.layout().screens ??= []);
  const round = x => Math.round(x * 10000) / 10000;

  // One screen: a dark frame, the standby glow and the picture in front of it (unlit, so
  // the camera frame looks like a lit screen whatever the room's light).
  function build(s) {
    const root = new THREE.Group();
    root.userData = { kind: 'screen', id: s.id };
    const height = 1 / ASPECT;
    const frame = new THREE.Mesh(new THREE.BoxGeometry(1, height, 0.035),
      new THREE.MeshStandardMaterial({ color: 0x15171c, roughness: 0.6 }));
    const inner = [1 - 2 * BEZEL, height - 2 * BEZEL];
    const standby = new THREE.Mesh(new THREE.PlaneGeometry(...inner), new THREE.MeshBasicMaterial({ color: STANDBY }));
    standby.position.z = 0.0181;
    const picture = new THREE.Mesh(new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, toneMapped: false }));
    picture.position.z = 0.0185;
    picture.userData.inner = inner;
    root.add(frame, standby, picture);
    place(root, s);
    group.add(root);
    screens.set(s.id, { root, picture });
    if (texture) fit(picture);
  }
  function place(root, s) {
    root.position.fromArray(s.position || [0, 1, 0]);
    root.rotation.fromArray(s.rotation || [0, 0, 0]);
    root.scale.setScalar(s.size || DEFAULT_SIZE);
  }
  // The frame fills as much of the screen as it can at its own shape (black bars aside).
  function fit(picture) {
    const [w, h] = picture.userData.inner;
    const image = texture.image, ratio = image.width / image.height;
    const scale = ratio > w / h ? [w, w / ratio] : [h * ratio, h];
    picture.scale.set(scale[0], scale[1], 1);
    picture.material.map = texture;
    picture.material.needsUpdate = true;
  }

  function rebuild() {
    for (const { root } of screens.values()) group.remove(root);
    screens.clear();
    for (const s of list()) build(s);
  }

  // ---------- showing what she sees ----------
  function look(msg) {
    if (!screens.size) return;
    loader.load(`/look/${msg.frame}?v=${msg.v}`, next => {
      next.colorSpace = THREE.SRGBColorSpace;
      texture?.dispose();
      texture = next;
      for (const { picture } of screens.values()) fit(picture);
      shownAt = performance.now() / 1000;
    });
  }

  function update(dt, now) {
    const want = now - shownAt < LOOK_HOLD_S ? 1 : 0;
    opacity = want ? 1 : Math.max(0, opacity - dt / FADE_S);
    for (const { picture } of screens.values()) picture.material.opacity = opacity;
    const first = screens.values().next().value;
    page.watch(want && first ? first.root.getWorldPosition(new THREE.Vector3()) : null);
  }

  // ---------- editor ----------
  page.registerEditorKind('screen', {
    label: () => 'Screen',
    selected: () => {},
    pickables: () => [...screens.values()].map(s => s.root),
    moved: obj => {
      const s = list().find(x => x.id === obj.userData.id);
      if (!s) return;
      // Scale sizes it from whichever axis was dragged (it keeps its shape).
      const size = s.size || DEFAULT_SIZE;
      const dragged = [obj.scale.x, obj.scale.y, obj.scale.z].reduce((a, b) => Math.abs(b - size) > Math.abs(a - size) ? b : a);
      s.size = round(Math.max(0.1, dragged));
      obj.scale.setScalar(s.size);
      s.position = obj.position.toArray().map(round);
      s.rotation = [obj.rotation.x, obj.rotation.y, obj.rotation.z].map(round);
    },
    removed: obj => {
      page.layout().screens = list().filter(x => x.id !== obj.userData.id);
      group.remove(obj);
      screens.delete(obj.userData.id);
      page.selectInEditor(null);
    },
  });

  // A new screen in front of where the editor camera looks, facing it.
  function add(at, facing) {
    const s = { id: Math.random().toString(36).slice(2, 10), position: at.toArray().map(round),
                rotation: [0, round(facing), 0], size: DEFAULT_SIZE };
    list().push(s);
    build(s);
    page.selectInEditor(screens.get(s.id).root);
  }

  return { rebuild, look, update, add };
}
