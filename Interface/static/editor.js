/* ==========================================================================
   Montaż: granice plansz i punkty cięcia.

   Serwer jest jedynym źródłem prawdy — każda operacja idzie POST-em i odświeża
   stan z odpowiedzi. Wyjątkiem jest przeciąganie cięcia po fali, gdzie zapis
   leci dopiero po puszczeniu przycisku; inaczej jedno przeciągnięcie robiłoby
   kilkadziesiąt zapisów na dysk.
   ========================================================================== */

const ED = {
  overview: [],
  bnd: { ch: null, data: null, drag: null },
  cut: {
    ch: null, data: null, peaks: null, audio: null,
    sel: 1, win: 4, onlyAtt: false, times: [], playing: false, stopAt: null,
  },
};

/* ------------------------------------------------------------- pomocnicze */

function edFmt(t) {
  const m = Math.floor(t / 60), s = Math.floor(t % 60), ms = Math.round((t % 1) * 1000);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')},${String(ms).padStart(3, '0')}`;
}

function edEsc(s) {
  return String(s).replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
}

function edWords(text, n, fromEnd) {
  const w = String(text).replace(/\s+/g, ' ').trim().split(' ');
  return (fromEnd ? w.slice(-n) : w.slice(0, n)).join(' ');
}

function edLineCount(text, maxChars) {
  let lines = 0;
  for (const para of text.split('\n')) {
    const t = para.trim();
    if (!t) continue;
    let cur = 0, pl = 1;
    for (const w of t.split(/\s+/)) {
      if (cur === 0) cur = w.length;
      else if (cur + 1 + w.length <= maxChars) cur += 1 + w.length;
      else { pl++; cur = w.length; }
    }
    lines += pl;
  }
  return Math.max(1, lines);
}

function edCssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

async function edLoadOverview() {
  const res = await api('/api/layout/overview');
  ED.overview = res.chapters || [];
  const ready = ED.overview.filter(c => c.processed);
  for (const [selId, key] of [['bndChapter', 'bnd'], ['cutChapter', 'cut']]) {
    const sel = $(selId);
    const keep = ED[key].ch;
    sel.innerHTML = ready.map(c =>
      `<option value="${c.chapter_num}">Rozdział ${String(c.chapter_num).padStart(2, '0')} — ${edEsc(c.title || c.header)}${c.attention ? ` · ${c.attention} spornych` : ''}</option>`
    ).join('');
    if (keep && ready.some(c => c.chapter_num === keep)) sel.value = String(keep);
  }
  return ready;
}

/* ==========================================================================
   Granice plansz
   ========================================================================== */

async function edLoadBoundaries(chapterNum) {
  try {
    ED.bnd.data = await api(`/api/layout/${chapterNum}`);
    ED.bnd.ch = chapterNum;
    edRenderBoundaries();
  } catch (err) {
    $('bndDoc').innerHTML = `<div class="ed-empty">${edEsc(err.message)}</div>`;
  }
}

function edBounds(d) { return [0].concat(d.breaks, [d.token_count]); }

function edBoardText(d, lo, hi) {
  let s = '';
  for (let i = lo; i < hi; i++) { s += d.tokens[i][0]; if (i + 1 < hi) s += d.tokens[i][1]; }
  return s;
}

function edIsDialogue(text) {
  const lines = text.split('\n').filter(l => l.trim());
  return lines.length > 0 && lines.every(l => /^[—–-]/.test(l.trim()));
}

function edRenderBoundaries() {
  const d = ED.bnd.data;
  if (!d) return;
  const doc = $('bndDoc');
  const bs = edBounds(d);
  const frag = document.createDocumentFragment();
  let over = 0, maxLines = 0;

  for (let b = 0; b < bs.length - 1; b++) {
    const lo = bs[b], hi = bs[b + 1];
    const text = edBoardText(d, lo, hi);
    const lines = edLineCount(text, d.max_chars);
    if (lines > maxLines) maxLines = lines;
    if (lines > d.max_lines) over++;

    const board = document.createElement('div');
    board.className = 'bnd-board';
    const gut = document.createElement('div');
    gut.className = 'bnd-gutter';
    gut.innerHTML =
      `<span class="no">${String(b + 1).padStart(2, '0')}</span>` +
      `<span class="li${lines > d.max_lines ? ' over' : ''}">${lines} lin</span>` +
      `<span class="kind">${edIsDialogue(text) ? 'dialog' : 'narracja'}</span>` +
      (hi - lo >= 2
        ? `<button class="bnd-add" data-board="${b}" data-mid="${lo + Math.floor((hi - lo) / 2)}"` +
          ` title="Wstaw nowy podział w środku tej planszy — potem przeciągnij go, gdzie chcesz">✚ podział</button>`
        : '');
    board.appendChild(gut);

    const t = document.createElement('div');
    t.className = 'bnd-text';
    for (let i = lo; i < hi; i++) {
      const w = document.createElement('span');
      w.textContent = d.tokens[i][0];
      t.appendChild(w);
      if (i + 1 < hi) {
        if (d.tokens[i][1] === '\n') t.appendChild(document.createElement('br'));
        const gap = document.createElement('span');
        gap.className = 'bnd-gap';
        gap.dataset.t = i + 1;
        gap.title = 'Podziel planszę w tym miejscu';
        t.appendChild(gap);
      }
    }
    board.appendChild(t);
    frag.appendChild(board);

    if (b < bs.length - 2) frag.appendChild(edBreakRow(d, b, bs[b + 1]));
  }

  doc.textContent = '';
  doc.appendChild(frag);

  const flagged = d.breaks.filter(t => d.flags[String(t)]).length;
  $('bndStats').innerHTML =
    `<div class="ed-stat"><b>${bs.length - 1}</b><span>plansz</span></div>` +
    `<div class="ed-stat ${flagged ? 'warn' : ''}"><b>${flagged}</b><span>bez pauzy</span></div>` +
    `<div class="ed-stat ${over ? 'warn' : ''}"><b>${maxLines}/${d.max_lines}</b><span>najdłuższa</span></div>`;
  $('bndMeta').textContent =
    `${d.token_count} słów · podział ${d.saved ? 'zapisany' : 'z automatu'}`;
}

function edBreakRow(d, idx, tokenIndex) {
  const flag = d.flags[String(tokenIndex)];
  const row = document.createElement('div');
  row.className = 'bnd-brk' + (flag ? ' flag' : '');
  row.dataset.brk = idx;
  row.innerHTML =
    `<div class="bnd-brk-no">${flag ? '✦ bez pauzy' : String(idx + 1).padStart(2, '0')}</div>` +
    `<div class="bnd-brk-bar">` +
      `<button class="bnd-knob" title="Złap i przeciągnij, aby przesunąć podział"` +
        ` aria-label="Przesuń podział ${idx + 1}">` +
        `<span class="bnd-knob-grip">⋮⋮</span>` +
      `</button>` +
      `<div class="bnd-brk-line"></div>` +
      `<div class="bnd-acts">` +
        `<button data-act="prev" title="O słowo w lewo">◀</button>` +
        `<button data-act="next" title="O słowo w prawo">▶</button>` +
        `<button data-act="merge" class="bnd-del" title="Usuń podział i połącz obie plansze">✕ usuń</button>` +
      `</div>` +
    `</div>`;

  if (flag) {
    const note = document.createElement('div');
    note.className = 'bnd-note';
    note.innerHTML = `<b>Lektor czyta bez przerwy.</b> Nagranie to tu jedna wypowiedź: ` +
      `<q>${edEsc(flag)}</q> — rozważ scalenie plansz albo przesunięcie granicy.`;
    row.appendChild(note);
  }
  return row;
}

async function edLayoutOp(op, params) {
  try {
    ED.bnd.data = await api(`/api/layout/${ED.bnd.ch}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ op }, params || {})),
    });
    edRenderBoundaries();
  } catch (err) {
    notify(err.message, 'error');
  }
}

/* ------------------------------------------------------ zdarzenia granic */

function edInitBoundaries() {
  const doc = $('bndDoc');

  doc.addEventListener('click', e => {
    const add = e.target.closest('.bnd-add');
    if (add) {
      edLayoutOp('split', { board: Number(add.dataset.board), token: Number(add.dataset.mid) });
      return;
    }
    const gap = e.target.closest('.bnd-gap');
    if (gap) {
      const t = Number(gap.dataset.t);
      const bs = edBounds(ED.bnd.data);
      const board = bs.findIndex((b, i) => i < bs.length - 1 && b < t && t < bs[i + 1]);
      if (board >= 0) edLayoutOp('split', { board, token: t });
      return;
    }
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const i = Number(act.closest('.bnd-brk').dataset.brk);
    const token = ED.bnd.data.breaks[i];
    if (act.dataset.act === 'merge') edLayoutOp('merge', { board: i });
    if (act.dataset.act === 'prev') edLayoutOp('move', { boundary: i, token: token - 1 });
    if (act.dataset.act === 'next') edLayoutOp('move', { boundary: i, token: token + 1 });
  });

  doc.addEventListener('pointerdown', e => {
    const grab = e.target.closest('.bnd-knob');
    if (!grab) return;
    e.preventDefault();
    const i = Number(grab.closest('.bnd-brk').dataset.brk);
    const bs = edBounds(ED.bnd.data);
    ED.bnd.drag = { i, aim: null };
    document.body.classList.add('bnd-dragging');
    doc.querySelectorAll('.bnd-gap').forEach(g => {
      const t = Number(g.dataset.t);
      if (t > bs[i] && t < bs[i + 2]) g.classList.add('ok');
    });
  });

  document.addEventListener('pointermove', e => {
    const drag = ED.bnd.drag;
    if (!drag) return;
    const el = document.elementFromPoint(e.clientX, e.clientY);
    const gap = el && el.classList && el.classList.contains('bnd-gap') && el.classList.contains('ok') ? el : null;
    if (drag.aim === gap) return;
    if (drag.aim) drag.aim.classList.remove('aim');
    drag.aim = gap;
    if (gap) gap.classList.add('aim');
  });

  document.addEventListener('pointerup', () => {
    const drag = ED.bnd.drag;
    if (!drag) return;
    const token = drag.aim ? Number(drag.aim.dataset.t) : null;
    document.body.classList.remove('bnd-dragging');
    doc.querySelectorAll('.bnd-gap').forEach(g => g.classList.remove('ok', 'aim'));
    ED.bnd.drag = null;
    if (token !== null) edLayoutOp('move', { boundary: drag.i, token });
  });

  $('bndChapter').addEventListener('change', e => edLoadBoundaries(Number(e.target.value)));
  $('bndReset').addEventListener('click', () => edLayoutOp('reset'));
  $('bndAccept').addEventListener('click', () => {
    ED.cut.ch = ED.bnd.ch;
    switchView('viewCuts');
  });
}

/* ==========================================================================
   Punkty cięcia
   ========================================================================== */

function edB64ToI8(s) {
  const bin = atob(s), u = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return new Int8Array(u.buffer);
}

async function edLoadCuts(chapterNum) {
  const C = ED.cut;
  try {
    const [cuts, peaks] = await Promise.all([
      api(`/api/cuts/${chapterNum}`),
      api(`/api/peaks/${chapterNum}`),
    ]);
    C.ch = chapterNum;
    C.data = cuts;
    C.times = cuts.cuts.map(c => c.time);
    C.peaks = { rate: peaks.rate, min: edB64ToI8(peaks.min), max: edB64ToI8(peaks.max) };

    let m = 1;
    for (let i = 0; i < C.peaks.max.length; i++) {
      if (C.peaks.max[i] > m) m = C.peaks.max[i];
      if (-C.peaks.min[i] > m) m = -C.peaks.min[i];
    }
    C.gain = 118 / m;

    if (!C.audio) { C.audio = new Audio(); C.audio.addEventListener('pause', () => { C.playing = false; edDraw(); }); }
    C.audio.src = `/api/audio/${chapterNum}`;

    const firstAtt = cuts.cuts.findIndex(c => c.needs_attention && !c.reviewed);
    C.sel = firstAtt > 0 ? firstAtt : 1;
    edRenderCuts();
  } catch (err) {
    $('cutList').innerHTML = `<div class="ed-empty">${edEsc(err.message)}</div>`;
  }
}

const edFirstCut = () => 1;
const edLastCut = () => ED.cut.data.cuts.length - 2;
const edClampSel = i => Math.min(Math.max(i, edFirstCut()), edLastCut());

function edSetupCanvas(cv) {
  const r = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = Math.round(w * r); cv.height = Math.round(h * r);
  const ctx = cv.getContext('2d');
  ctx.setTransform(r, 0, 0, r, 0, 0);
  return { ctx, w, h };
}

function edPeakColumns(t0, t1, w) {
  const C = ED.cut, rate = C.peaks.rate, cols = [];
  const i0 = Math.max(0, Math.floor(t0 * rate));
  const i1 = Math.min(C.peaks.min.length, Math.ceil(t1 * rate));
  const span = Math.max(1, i1 - i0);
  for (let x = 0; x < w; x++) {
    const a = i0 + Math.floor(span * x / w);
    const b = Math.max(a + 1, i0 + Math.floor(span * (x + 1) / w));
    let lo = 0, hi = 0;
    for (let i = a; i < Math.min(b, i1); i++) {
      if (C.peaks.min[i] < lo) lo = C.peaks.min[i];
      if (C.peaks.max[i] > hi) hi = C.peaks.max[i];
    }
    cols.push([lo, hi]);
  }
  return cols;
}

function edDrawWave() {
  const C = ED.cut, D = C.data;
  const cv = $('cutWave'), { ctx, w, h } = edSetupCanvas(cv);
  const t = C.times[C.sel], t0 = t - C.win / 2, t1 = t + C.win / 2;
  const X = s => (s - t0) / (t1 - t0) * w;
  const mid = h * 0.44, amp = h * 0.36;

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = edCssVar('--bg-card'); ctx.fillRect(0, 0, w, h);

  ctx.fillStyle = edCssVar('--bg-card-alt');
  D.silences.forEach(([a, b]) => {
    if (b < t0 || a > t1) return;
    ctx.fillRect(X(a), 4, Math.max(1, X(b) - X(a)), h - 30);
  });

  ctx.fillStyle = edCssVar('--text-secondary');
  edPeakColumns(t0, t1, w).forEach(([lo, hi], x) => {
    const y0 = mid - hi * C.gain / 127 * amp, y1 = mid - lo * C.gain / 127 * amp;
    ctx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
  });

  ctx.fillStyle = edCssVar('--border-color');
  D.words.forEach(([a]) => { if (a >= t0 && a <= t1) ctx.fillRect(X(a), mid + amp + 2, 1, 5); });

  const by = h - 20;
  D.segments.forEach(s => {
    if (s.e < t0 || s.s > t1) return;
    const hot = s.s + 0.05 < t && t < s.e - 0.05;
    ctx.fillStyle = hot ? edCssVar('--accent-rose') : edCssVar('--accent-emerald');
    ctx.globalAlpha = hot ? 0.9 : 0.45;
    ctx.fillRect(X(s.s), by, Math.max(2, X(s.e) - X(s.s)), 6);
    ctx.globalAlpha = 1;
  });

  ctx.fillStyle = edCssVar('--border-color');
  C.times.forEach((tt, i) => {
    if (i !== C.sel && tt >= t0 && tt <= t1) ctx.fillRect(X(tt) - 1, 4, 2, h - 30);
  });

  if (C.playing && C.audio.currentTime >= t0 && C.audio.currentTime <= t1) {
    ctx.fillStyle = edCssVar('--text-primary'); ctx.globalAlpha = 0.4;
    ctx.fillRect(X(C.audio.currentTime), 4, 1, h - 30); ctx.globalAlpha = 1;
  }

  const x = X(t);
  ctx.fillStyle = edCssVar('--accent-indigo');
  ctx.fillRect(x - 1.5, 0, 3, h - 12);
  ctx.beginPath(); ctx.moveTo(x - 7, 0); ctx.lineTo(x + 7, 0); ctx.lineTo(x, 11); ctx.closePath(); ctx.fill();

  ctx.fillStyle = edCssVar('--text-muted');
  ctx.font = '10px ui-monospace, Consolas, monospace';
  const step = C.win <= 2 ? 0.25 : C.win <= 6 ? 1 : C.win <= 16 ? 2 : 5;
  for (let s = Math.ceil(t0 / step) * step; s < t1; s += step) {
    ctx.fillText(edFmt(s).slice(0, 5), X(s) + 3, h - 3);
  }
}

function edDrawMini() {
  const C = ED.cut, D = C.data;
  const cv = $('cutMini'), { ctx, w, h } = edSetupCanvas(cv);
  const X = s => s / D.duration * w, mid = h / 2, amp = h * 0.4;

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = edCssVar('--bg-card'); ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = edCssVar('--border-color');
  edPeakColumns(0, D.duration, w).forEach(([lo, hi], x) => {
    const y0 = mid - hi * C.gain / 127 * amp, y1 = mid - lo * C.gain / 127 * amp;
    ctx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
  });

  C.times.forEach((tt, i) => {
    if (i < edFirstCut() || i > edLastCut()) return;
    const c = D.cuts[i];
    ctx.fillStyle = c.reviewed ? edCssVar('--accent-emerald')
      : (c.needs_attention ? edCssVar('--accent-rose') : edCssVar('--accent-indigo'));
    ctx.globalAlpha = i === C.sel ? 1 : 0.5;
    ctx.fillRect(X(tt), 0, i === C.sel ? 2 : 1, h);
    ctx.globalAlpha = 1;
  });

  ctx.strokeStyle = edCssVar('--accent-indigo'); ctx.lineWidth = 1;
  ctx.strokeRect(X(C.times[C.sel] - C.win / 2), 0.5, Math.max(3, X(C.win)), h - 1);
}

function edDraw() { if (ED.cut.data) { edDrawWave(); edDrawMini(); } }

function edRenderCuts() {
  const C = ED.cut, D = C.data;
  if (!D) return;
  const cut = D.cuts[C.sel], t = C.times[C.sel], delta = t - cut.time;
  const seg = D.segments.find(s => s.s + 0.05 < t && t < s.e - 0.05);

  $('cutTime').textContent = edFmt(t);
  const moved = Math.abs(t - cut.time) > 0.0005 || cut.source === 'manual';
  $('cutDelta').textContent = moved
    ? `${t - cut.time >= 0 ? '+' : '−'}${Math.abs(delta * 1000).toFixed(0)} ms wobec zapisanego`
    : 'zgodne z automatem';
  $('cutDelta').className = 'cut-delta' + (moved ? ' moved' : '');

  const badge = $('cutBadge');
  if (seg) { badge.textContent = 'w środku wypowiedzi'; badge.className = 'cut-badge alarm'; }
  else if (cut.source === 'manual') { badge.textContent = 'ustawione ręcznie'; badge.className = 'cut-badge manual'; }
  else if (cut.source === 'words') { badge.textContent = 'brak ciszy w pobliżu'; badge.className = 'cut-badge alarm'; }
  else { badge.textContent = 'dosunięte do ciszy'; badge.className = 'cut-badge silence'; }

  $('cutWarn').innerHTML = seg
    ? `<div class="cut-warn"><b>Lektor czyta przez ten punkt bez przerwy.</b> Nagranie to tu jedna wypowiedź: ` +
      `<q>${edEsc(seg.t)}</q> — przesuń cięcie do najbliższej pauzy albo wróć do granic i scal plansze.</div>`
    : '';

  $('cutLabA').textContent = C.sel;
  $('cutLabB').textContent = C.sel + 1;
  $('cutTxtA').innerHTML = '…' + edEsc(edWords(D.boards[C.sel - 1].text, 18, true));
  $('cutTxtB').innerHTML = `<em>${edEsc(edWords(D.boards[C.sel].text, 18, false))}</em>…`;

  const att = D.cuts.filter(c => c.needs_attention && !c.reviewed).length;
  const rev = D.cuts.filter(c => c.reviewed).length;
  $('cutStats').innerHTML =
    `<div class="ed-stat"><b>${D.summary.boards}</b><span>plansz</span></div>` +
    `<div class="ed-stat ${att ? 'warn' : ''}"><b>${att}</b><span>do sprawdzenia</span></div>` +
    `<div class="ed-stat good"><b>${rev}</b><span>przejrzanych</span></div>`;
  $('cutMeta').textContent = `${edFmt(D.duration)} · ${D.cuts.length} cięć`;

  edRenderCutList();
  edDraw();
}

function edRenderCutList() {
  const C = ED.cut, D = C.data, box = $('cutList');
  box.textContent = '';
  for (let i = edFirstCut(); i <= edLastCut(); i++) {
    const c = D.cuts[i];
    const att = c.needs_attention && !c.reviewed;
    if (C.onlyAtt && !att) continue;
    const [cls, lab] = c.reviewed ? ['st-reviewed', 'przejrzane']
      : att ? ['st-att', 'sporne']
      : c.source === 'manual' ? ['st-manual', 'ręczne'] : ['st-auto', 'automat'];
    const b = document.createElement('button');
    b.className = 'cut-row' + (att ? ' att' : '') + (i === C.sel ? ' active' : '');
    b.innerHTML = `<span class="n">${String(i).padStart(2, '0')}</span>` +
      `<span class="lab">${edEsc(edWords(D.boards[i].text, 5, false))}…</span>` +
      `<span class="st ${cls}">${lab}</span>`;
    b.addEventListener('click', () => { C.sel = i; edRenderCuts(); });
    box.appendChild(b);
  }
  if (!box.children.length) {
    box.innerHTML = '<div class="ed-empty">Nic spornego nie zostało.</div>';
  }
}

function edSetCutTime(t) {
  const C = ED.cut;
  const lo = C.times[C.sel - 1] + 0.2, hi = C.times[C.sel + 1] - 0.2;
  C.times[C.sel] = Math.round(Math.min(Math.max(t, lo), hi) * 1000) / 1000;
  $('cutTime').textContent = edFmt(C.times[C.sel]);
  edDraw();
}

async function edCommitCut(time) {
  const C = ED.cut, token = C.data.cuts[C.sel].token, keep = C.sel;
  try {
    C.data = await api(`/api/cuts/${C.ch}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, time }),
    });
    C.times = C.data.cuts.map(c => c.time);
    C.sel = keep;
    edRenderCuts();
  } catch (err) {
    notify(err.message, 'error');
  }
}

function edPlay(from, to) {
  const C = ED.cut;
  C.stopAt = to;
  C.audio.currentTime = Math.max(0, from);
  C.audio.play().then(() => { C.playing = true; edTick(); }).catch(() => {});
}

function edTick() {
  const C = ED.cut;
  if (!C.playing) return;
  if (C.stopAt !== null && C.audio.currentTime >= C.stopAt) { C.audio.pause(); C.playing = false; }
  edDraw();
  if (C.playing) requestAnimationFrame(edTick);
}

/* ------------------------------------------------------ zdarzenia cięcia */

function edInitCuts() {
  const C = ED.cut;

  $('cutWave').addEventListener('pointerdown', e => {
    if (!C.data) return;
    const cv = $('cutWave'), r = cv.getBoundingClientRect();
    const t0 = C.times[C.sel] - C.win / 2;
    const T = x => t0 + (x - r.left) / r.width * C.win;
    if (Math.abs(e.clientX - (r.left + (C.times[C.sel] - t0) / C.win * r.width)) > 14) return;
    cv.setPointerCapture(e.pointerId);
    const move = ev => {
      let t = T(ev.clientX);
      // Magnes na środek pobliskiej ciszy — jak dociąganie w Audacity.
      for (const [a, b] of C.data.silences) {
        if (b - a >= 0.1 && Math.abs((a + b) / 2 - t) < 0.05) { t = (a + b) / 2; break; }
      }
      edSetCutTime(t);
    };
    const up = () => {
      cv.removeEventListener('pointermove', move);
      cv.removeEventListener('pointerup', up);
      edCommitCut(C.times[C.sel]);
    };
    cv.addEventListener('pointermove', move);
    cv.addEventListener('pointerup', up);
  });

  $('cutMini').addEventListener('click', e => {
    if (!C.data) return;
    const r = e.currentTarget.getBoundingClientRect();
    const t = (e.clientX - r.left) / r.width * C.data.duration;
    let best = edFirstCut(), bd = Infinity;
    for (let i = edFirstCut(); i <= edLastCut(); i++) {
      const d = Math.abs(C.times[i] - t);
      if (d < bd) { bd = d; best = i; }
    }
    C.sel = best; edRenderCuts();
  });

  document.querySelectorAll('[data-nudge]').forEach(b =>
    b.addEventListener('click', () => {
      if (!C.data) return;
      edSetCutTime(C.times[C.sel] + parseFloat(b.dataset.nudge));
      edCommitCut(C.times[C.sel]);
    }));

  $('cutZoom').addEventListener('click', e => {
    const b = e.target.closest('[data-z]');
    if (!b) return;
    C.win = parseFloat(b.dataset.z);
    $('cutZoom').querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b));
    edDraw();
  });

  $('cutPlayJoint').addEventListener('click', () => C.data && edPlay(C.times[C.sel] - 1.6, C.times[C.sel] + 1.6));
  $('cutPlayA').addEventListener('click', () => C.data && edPlay(Math.max(C.times[C.sel] - 3.5, C.times[C.sel - 1]), C.times[C.sel]));
  $('cutPlayB').addEventListener('click', () => C.data && edPlay(C.times[C.sel], Math.min(C.times[C.sel] + 3.5, C.times[C.sel + 1])));
  $('cutReset').addEventListener('click', () => C.data && edCommitCut(null));

  $('cutVerify').addEventListener('click', async () => {
    if (!C.data) return;
    const token = C.data.cuts[C.sel].token;
    try {
      await api(`/api/layout/${C.ch}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ op: 'reviewed', token, flag: true }),
      });
      C.data.cuts[C.sel].reviewed = true;
      const next = C.data.cuts.findIndex((c, i) => i > C.sel && c.needs_attention && !c.reviewed);
      C.sel = next > 0 ? next : edClampSel(C.sel + 1);
      edRenderCuts();
    } catch (err) {
      notify(err.message, 'error');
    }
  });

  $('cutChapter').addEventListener('change', e => edLoadCuts(Number(e.target.value)));
  $('cutOnlyAtt').addEventListener('change', e => { C.onlyAtt = e.target.checked; edRenderCutList(); });

  document.addEventListener('keydown', e => {
    if (!$('viewCuts').classList.contains('active') || !C.data) return;
    if (e.target.matches('input, textarea, select')) return;
    const step = e.shiftKey ? 0.1 : 0.01;
    if (e.key === 'ArrowLeft') { e.preventDefault(); edSetCutTime(C.times[C.sel] - step); edCommitCut(C.times[C.sel]); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); edSetCutTime(C.times[C.sel] + step); edCommitCut(C.times[C.sel]); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); C.sel = edClampSel(C.sel - 1); edRenderCuts(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); C.sel = edClampSel(C.sel + 1); edRenderCuts(); }
    else if (e.key === ' ') { e.preventDefault(); C.playing ? C.audio.pause() : $('cutPlayJoint').click(); }
    else if (e.key === 'Enter') { e.preventDefault(); $('cutVerify').click(); }
  });

  window.addEventListener('resize', edDraw);
}

/* ------------------------------------------------------------ wejście */

async function edEnter(viewId) {
  const box = viewId === 'viewBoundaries' ? $('bndDoc') : $('cutList');
  let ready;
  try {
    ready = await edLoadOverview();
  } catch (err) {
    // Bez projektu albo bez wyników serwer odpowiada czytelnym 404 — pokazujemy go
    // zamiast zostawiać pusty ekran.
    box.innerHTML = `<div class="ed-empty">${edEsc(err.message)}</div>`;
    return;
  }
  if (!ready.length) {
    box.innerHTML = '<div class="ed-empty">Najpierw przetwórz rozdziały w kroku „Rozdziały”.</div>';
    return;
  }
  if (viewId === 'viewBoundaries') {
    const ch = ED.bnd.ch && ready.some(c => c.chapter_num === ED.bnd.ch) ? ED.bnd.ch : ready[0].chapter_num;
    $('bndChapter').value = String(ch);
    await edLoadBoundaries(ch);
  } else {
    const ch = ED.cut.ch && ready.some(c => c.chapter_num === ED.cut.ch) ? ED.cut.ch : ready[0].chapter_num;
    $('cutChapter').value = String(ch);
    await edLoadCuts(ch);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  edInitBoundaries();
  edInitCuts();
});
