const form = document.querySelector('#search-form');
const input = document.querySelector('#query');
const statusEl = document.querySelector('#status');
const chips = document.querySelector('#chips');
const notes = document.querySelector('#notes');
const clarify = document.querySelector('#clarify');
const preview = document.querySelector('#preview');
const results = document.querySelector('#results');
const dialog = document.querySelector('#player-dialog');
const player = document.querySelector('#player');
const playerMessage = document.querySelector('#player-message');
let searchSequence = 0;
let searchAbort;
let playerSequence = 0;
let playerAbort;

// All text is inserted with textContent, never innerHTML.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function secondsLabel(value) {
  const total = Math.max(0, Math.floor(Number(value) || 0));
  return `${String(Math.floor(total / 3600)).padStart(2, '0')}:${String(Math.floor(total % 3600 / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}
const sceneLabel = (s) => `${s.start_time || secondsLabel(s.start_seconds)} – ${s.end_time || secondsLabel(s.end_seconds)}`;

async function openPlayer(movie, scene) {
  const sequence = ++playerSequence;
  playerAbort?.abort();
  playerAbort = new AbortController();
  player.pause();
  player.removeAttribute('src');
  player.load();
  document.querySelector('#player-title').textContent = movie.title;
  document.querySelector('#player-time').textContent = `Scene ${sceneLabel(scene)}`;
  playerMessage.textContent = 'Loading video from the movie’s GCS URI…';
  if (!dialog.open) dialog.showModal();
  try {
    const response = await fetch(scene.playback_url, { headers: { Range: 'bytes=0-0' }, signal: playerAbort.signal });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Video request failed (${response.status})`);
    }
    if (!dialog.open || sequence !== playerSequence) return;
    player.src = scene.playback_url;
    player.onloadedmetadata = () => { if (sequence === playerSequence && dialog.open) { player.currentTime = scene.start_seconds; playerMessage.textContent = ''; } };
    player.onerror = () => { if (sequence === playerSequence && dialog.open) playerMessage.textContent = 'Video streaming failed after connecting to GCS.'; };
    player.play().catch(() => {});
  } catch (error) {
    if (dialog.open && sequence === playerSequence && error.name !== 'AbortError') playerMessage.textContent = error.message;
  }
}

function renderScene(movie, scene) {
  const row = el('div', 'scene');
  const copy = el('div', 'scene-copy');
  const head = el('div', 'scene-time', sceneLabel(scene));
  if (scene.literal_match) head.append(el('span', 'tag tag-ok', 'exact words found'));
  if (scene.actor_in_scene) head.append(el('span', 'tag', 'cast in scene'));
  copy.append(head, el('div', 'scene-summary', scene.description || scene.summary || 'Scene details unavailable'));
  const lines = (scene.dialogue || []).slice(0, 2);
  for (const t of lines) {
    const text = [t.original, t.gloss ? `(${t.gloss})` : ''].filter(Boolean).join(' ');
    copy.append(el('div', 'dialogue', `${t.speaker ? t.speaker + ': ' : ''}${text}`));
  }
  const button = el('button', '', 'Try video');
  button.type = 'button';
  button.addEventListener('click', () => openPlayer(movie, scene));
  row.append(copy, button);
  return row;
}

function renderMovie(movie) {
  const article = el('article', 'movie');
  let poster;
  if (movie.thumbnail_url) {
    poster = el('img', 'poster');
    poster.src = movie.thumbnail_url;
    poster.alt = `${movie.title} poster`;
    poster.loading = 'lazy';
    poster.onerror = () => poster.replaceWith(el('div', 'poster poster-fallback', movie.title.slice(0, 1)));
  } else {
    poster = el('div', 'poster poster-fallback', movie.title.slice(0, 1));
  }
  article.append(poster);
  const body = el('div');
  const top = el('div', 'movie-top');
  top.append(el('h2', '', movie.title), el('span', 'rank', `#${movie.rank}`));
  body.append(top);
  body.append(el('div', 'meta', [movie.language, movie.content_type, (movie.genres || []).join(' · '),
    (movie.cast || []).slice(0, 3).join(', ')].filter(Boolean).join('  •  ')));
  if (movie.description) body.append(el('p', 'description', movie.description));
  if (movie.scenes?.length) {
    body.append(el('h3', 'scene-heading', 'Matching scenes'));
    const list = el('div', 'scene-list');
    for (const scene of movie.scenes) list.append(renderScene(movie, scene));
    body.append(list);
  } else {
    const play = el('button', 'play-title', 'Try video');
    play.type = 'button';
    play.addEventListener('click', () => openPlayer(movie, { playback_url: movie.playback_url, start_seconds: 0, start_time: '00:00:00', end_time: '' }));
    body.append(play);
  }
  article.append(body);
  return article;
}

function renderResults(container, items) {
  container.replaceChildren(...items.map(renderMovie));
}

function renderUnderstood(data) {
  chips.replaceChildren();
  const i = data.interpretation || {};
  const add = (label) => chips.append(el('span', 'chip', label));
  if (i.mode) add(`type: ${i.mode}`);
  for (const f of i.filters || []) add(f);
  if (i.years) add(`year: ${i.years}`);
  if (i.sort) add(`sort: ${i.sort}`);
  if (i.limit) add(`top ${i.limit}`);
  for (const c of i.corrections || []) add(`“${c.typed}” → ${c.used}`);
}

async function search(query) {
  const sequence = ++searchSequence;
  searchAbort?.abort();
  searchAbort = new AbortController();
  input.value = query;
  statusEl.textContent = 'Searching…';
  statusEl.className = 'status';
  for (const node of [chips, notes, results]) node.replaceChildren();
  clarify.hidden = preview.hidden = true;
  const submit = form.querySelector('button[type=submit]');
  submit.disabled = true;
  try {
    const response = await fetch('/api/search', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, limit: 10 }),
      signal: searchAbort.signal,
    });
    const data = await response.json();
    if (sequence !== searchSequence) return;
    if (!response.ok) throw new Error(data.detail || `Search failed (${response.status})`);
    statusEl.className = `status status-${data.status}`;
    const n = data.results.length;
    statusEl.textContent = `${data.status.replaceAll('_', ' ')} · ${n} ${n === 1 ? 'title' : 'titles'} · ${data.timings_ms.total} ms`
      + (data.message ? ` — ${data.message}` : '');
    renderUnderstood(data);
    for (const note of data.notes || []) notes.append(el('li', '', note));
    // the question itself is in the status line; the panel only exists to offer choices
    if (data.clarification?.suggestions?.length) {
      clarify.hidden = false;
      clarify.replaceChildren(el('strong', '', 'Did you mean:'));
      for (const s of data.clarification.suggestions) {
        const b = el('button', 'suggestion', s);
        b.type = 'button';
        b.addEventListener('click', () => search(s));
        clarify.append(b);
      }
    }
    if (data.preview?.results?.length) {
      preview.hidden = false;
      preview.replaceChildren(el('h3', '', data.preview.label));
      const box = el('div', 'results');
      renderResults(box, data.preview.results);
      preview.append(box);
    }
    renderResults(results, data.results);
    if (!data.results.length && !data.clarification) {
      results.append(el('div', 'empty', 'No matching title or scene. Try a title, actor, genre, or a different description.'));
    }
  } catch (error) {
    if (sequence !== searchSequence || error.name === 'AbortError') return;
    statusEl.textContent = error.message;
    results.append(el('div', 'empty', 'Search could not run. Check that PostgreSQL and the V2 API are running.'));
  } finally {
    if (sequence === searchSequence) submit.disabled = false;
  }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const query = input.value.trim();
  if (query) search(query);
});
for (const button of document.querySelectorAll('[data-query]')) {
  button.addEventListener('click', () => search(button.dataset.query));
}
document.querySelector('#player-close').addEventListener('click', () => dialog.close());
dialog.addEventListener('close', () => {
  ++playerSequence;
  playerAbort?.abort();
  player.onloadedmetadata = player.onerror = null;
  player.pause(); player.removeAttribute('src'); player.load();
});
