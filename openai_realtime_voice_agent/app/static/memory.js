// Her memory, in the room editor's Memory section: the notes she keeps and her diary.
//
// Only she adds to them (with [remember: ...] in her replies, and a diary entry when a
// conversation ends; see the add-on's memory.py). Here you can fix the wording of one
// or delete it. Every change is saved right away (PUT /memory), separately from
// "Save room". The link at the top opens the chat log: every turn as it was sent to her.
//
// The page calls createMemoryEditor(container) once and load() each time the editor opens.

const DIARY_SHOWN = 30;   // newest diary entries listed (all of them stay in the file)

function el(tag, props = {}, ...children) {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...children);
  return e;
}

export function createMemoryEditor(container) {
  let data = { notes: [], diary: [], last_talked: null };

  async function load() {
    try {
      data = await (await fetch('/memory', { cache: 'no-store' })).json();
    } catch (e) {
      container.replaceChildren(el('p', { textContent: `Couldn't load her memory: ${e}` }));
      return;
    }
    build();
  }

  async function save() {
    const status = container.querySelector('.memStatus');
    status.textContent = 'Saving…';
    try {
      const r = await fetch('/memory', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                                         body: JSON.stringify({ notes: data.notes, diary: data.diary }) });
      if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
      // The lists on screen stay the ones edited here (the ✕ buttons hold on to them).
      status.textContent = 'Saved.';
    } catch (e) {
      status.textContent = `Not saved: ${e.message}`;
    }
  }

  const shortDate = stamp => {
    const d = new Date(stamp);
    return isNaN(d) ? '' : d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  };

  // One note or diary entry: its date, its text to edit, and ✕ to delete it.
  function entry(list, item, stamp) {
    const box = el('div', { className: 'act' });
    const text = el('textarea', { value: item.text, rows: Math.min(6, Math.ceil(item.text.length / 38) || 1) });
    text.onchange = () => {
      if (!text.value.trim()) return;   // emptying it doesn't delete it; ✕ does
      item.text = text.value.trim();
      save();
    };
    const remove = el('button', { textContent: '✕', className: 'x danger', title: 'Delete' });
    remove.onclick = () => {
      list.splice(list.indexOf(item), 1);
      save();
      build();
    };
    const date = el('span', { textContent: shortDate(stamp) });
    date.style.cssText = 'color: var(--muted); font-size: 12px; flex: 1';
    box.append(el('div', { className: 'row' }, date, remove), text);
    return box;
  }

  function section(title, list, stampOf, limit) {
    const shown = list.slice(-limit).reverse();   // newest first
    const details = el('details', {}, el('summary', { textContent: `${title} (${list.length})` }));
    details.querySelector('summary').style.cssText = 'cursor: pointer; color: var(--muted); font-size: 12px';
    const items = el('div');
    items.style.cssText = 'display: flex; flex-direction: column; gap: 6px; margin-top: 6px';
    for (const item of shown) items.append(entry(list, item, stampOf(item)));
    if (!shown.length) items.append(el('p', { textContent: 'Nothing yet.' }));
    details.append(items);
    return details;
  }

  function build() {
    const last = data.last_talked ? `Last talked: ${shortDate(data.last_talked)}.` : 'She hasn\'t talked with you yet.';
    const link = el('a', { href: '/chat', target: '_blank', textContent: 'Open the chat log ↗' });
    link.style.color = 'var(--accent)';
    container.replaceChildren(
      el('div', { className: 'row' }, link),
      el('p', { textContent: last }),
      section('Notes', data.notes, n => n.made, Infinity),
      section('Diary', data.diary, d => d.at, DIARY_SHOWN),
      el('p', { className: 'memStatus' }),
    );
  }

  return { load };
}
