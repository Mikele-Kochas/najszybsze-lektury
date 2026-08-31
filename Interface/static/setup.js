/* ==========================================================================
   Warstwa produkcyjna: projekt, upload, kreator rozdziałów, zadania, eksport.
   Studio weryfikacji (app.js) pozostaje niezależne i korzysta z tych samych danych.
   ========================================================================== */

const NL = {
  project: null,
  proposal: null,
  mapRows: [],
  audioOptions: [],
  jobTimer: null,
  activeJobId: null,
  onJobDone: null,
};

const $ = (id) => document.getElementById(id);

function notify(message, type = 'info') {
  if (typeof showToast === 'function') showToast(message, type);
  else console.log(`[${type}] ${message}`);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let payload = null;
  try { payload = await res.json(); } catch { /* pusta odpowiedź */ }
  if (!res.ok) {
    throw new Error((payload && payload.detail) || `Błąd ${res.status}`);
  }
  return payload;
}

/* ---------------------------------------------------------------- widoki */

function switchView(viewId) {
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === viewId));
  document.querySelectorAll('.step-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.view === viewId));

  if (viewId === 'viewChapters') refreshChapterStatus();
  if ((viewId === 'viewBoundaries' || viewId === 'viewCuts') && typeof edEnter === 'function') {
    edEnter(viewId);
  }
  if (viewId === 'viewStudio' && typeof loadChapters === 'function') {
    loadChapters().then(() => {
      if (typeof loadSample === 'function' && !window.__studioLoaded) {
        window.__studioLoaded = true;
        loadSample();
      }
    });
  }
}

document.querySelectorAll('.step-btn').forEach(btn => {
  btn.addEventListener('click', () => switchView(btn.dataset.view));
});

/* ------------------------------------------------------------- zadania */

function renderJob(job) {
  const bar = $('jobBar');
  if (!job || ['done', 'error', 'cancelled'].includes(job.status)) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  $('jobLabel').textContent = job.label;
  $('jobMessage').textContent = job.message || '';
  const pct = Math.round((job.progress || 0) * 100);
  $('jobPercent').textContent = `${pct}%`;
  $('jobFill').style.width = `${pct}%`;
  $('jobLog').textContent = (job.log || []).join('\n');
}

function watchJob(jobId, onDone) {
  NL.activeJobId = jobId;
  NL.onJobDone = onDone;
  if (NL.jobTimer) clearInterval(NL.jobTimer);

  const poll = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      renderJob(job);

      if (['done', 'error', 'cancelled'].includes(job.status)) {
        clearInterval(NL.jobTimer);
        NL.jobTimer = null;
        NL.activeJobId = null;
        $('jobBar').hidden = true;

        if (job.status === 'done') {
          notify('Zadanie zakończone ✓', 'success');
          if (onDone) onDone(job);
        } else if (job.status === 'error') {
          notify(`Błąd: ${job.error}`, 'error');
          alert(`Zadanie nie powiodło się:\n\n${job.error}`);
        } else {
          notify('Zadanie anulowane', 'warning');
        }
        await loadProject();
      }
    } catch (err) {
      clearInterval(NL.jobTimer);
      NL.jobTimer = null;
      console.error('Job polling error:', err);
    }
  };

  poll();
  NL.jobTimer = setInterval(poll, 1000);
}

$('btnCancelJob').addEventListener('click', async () => {
  if (!NL.activeJobId) return;
  try { await api(`/api/jobs/${NL.activeJobId}/cancel`, { method: 'POST' }); }
  catch (err) { notify(err.message, 'error'); }
});

$('btnToggleLog').addEventListener('click', () => {
  const log = $('jobLog');
  log.hidden = !log.hidden;
});

/* ------------------------------------------------------------- projekt */

async function loadProject() {
  try {
    NL.project = await api('/api/project');
  } catch (err) {
    console.error('Nie udało się wczytać projektu:', err);
    return;
  }
  const p = NL.project;

  $('projectSubtitle').textContent = p.title
    ? `${p.title}${p.author ? ' — ' + p.author : ''}`
    : 'Brak projektu — wgraj tekst i nagrania';

  const dev = p.device || {};
  const vramLabel = dev.vram_mb ? ` · ${(dev.vram_mb / 1024).toFixed(1)} GB` : '';
  $('deviceBadge').textContent = `${dev.device || '?'}${vramLabel} · ${dev.model_size || '?'}`;
  $('deviceBadge').title = dev.gpu_name || 'Urządzenie używane przez Whisper';
  $('deviceBadge').className = `device-badge ${dev.device === 'cuda' ? 'gpu' : 'cpu'}`;
  NL.vramMb = dev.vram_mb || 0;
  updateVramNote();

  $('stText').textContent = p.text_present ? `✅ ${p.text_file}` : '❌ brak';
  $('stAudio').textContent = p.audio_count ? `✅ ${p.audio_count} plików` : '❌ brak';
  $('stMap').textContent = p.chapter_map_count ? `✅ ${p.chapter_map_count} rozdziałów` : '⏳ nie zatwierdzona';
  $('stProcessed').textContent = p.chapter_map_count
    ? `${p.processed_count} / ${p.chapter_map_count}` : '—';

  const note = $('archiveNote');
  if (p.archived) {
    note.hidden = false;
    note.innerHTML = `🗄️ Poprzedni projekt <b>${escapeHtml(p.archived.title || '(bez tytułu)')}</b> ` +
      `(${p.archived.audio_count} nagrań) zachowano w <code>Data/Poprzedni_projekt/</code>.`;
  } else {
    note.hidden = true;
  }

  if (!$('inputTitle').value) $('inputTitle').value = p.title || '';
  if (!$('inputAuthor').value) $('inputAuthor').value = p.author || '';
  if (!$('exportName').value) $('exportName').value = p.title || '';

  const s = p.settings || {};
  $('setModel').value = s.model_size || 'small';
  $('setDevice').value = s.device || 'auto';
  $('setMaxLines').value = s.max_lines_per_board || 11;
  $('setMaxChars').value = s.max_chars_per_line || 45;

  if (p.active_job && !NL.activeJobId) watchJob(p.active_job.id, null);
}

/* -------------------------------------------------------------- upload */

function wireDropZone(zoneId, inputId, onFiles) {
  const zone = $(zoneId);
  const input = $(inputId);

  zone.addEventListener('click', () => input.click());
  input.addEventListener('change', () => onFiles([...input.files]));

  ['dragenter', 'dragover'].forEach(evt =>
    zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.add('dragging'); }));
  ['dragleave', 'drop'].forEach(evt =>
    zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.remove('dragging'); }));

  // Upuszczone pliki nie trafiają do input.files, więc przekazujemy je wprost
  // do callbacku, który zapamiętuje je w zmiennych używanych przy wysyłce.
  zone.addEventListener('drop', e => onFiles([...e.dataTransfer.files]));
}

let selectedTextFile = null;
let selectedAudioFiles = [];

wireDropZone('dropText', 'fileText', (files) => {
  const txt = files.find(f => f.name.toLowerCase().endsWith('.txt'));
  if (!txt) { notify('Wybierz plik z rozszerzeniem .txt', 'error'); return; }
  selectedTextFile = txt;
  $('dropTextLabel').innerHTML = `📄 <b>${txt.name}</b> — ${(txt.size / 1024).toFixed(0)} KB`;
  $('dropText').classList.add('filled');
  if (!$('inputTitle').value) $('inputTitle').value = txt.name.replace(/\.txt$/i, '');
});

wireDropZone('dropAudio', 'fileAudio', (files) => {
  const audio = files.filter(f => /\.(mp3|m4a|wav|flac|ogg|opus|aac|mp4)$/i.test(f.name));
  if (!audio.length) { notify('Nie wykryto plików audio', 'error'); return; }
  selectedAudioFiles = audio.sort((a, b) => a.name.localeCompare(b.name, 'pl', { numeric: true }));
  $('dropAudioLabel').innerHTML = `🎧 <b>${audio.length}</b> plików audio`;
  $('dropAudio').classList.add('filled');

  const totalMb = audio.reduce((sum, f) => sum + f.size, 0) / 1048576;
  $('audioFileList').innerHTML =
    selectedAudioFiles.slice(0, 40).map((f, i) =>
      `<span class="file-chip"><b>${i + 1}</b> ${f.name}</span>`).join('') +
    (audio.length > 40 ? `<span class="file-chip muted">…i ${audio.length - 40} więcej</span>` : '') +
    `<span class="file-chip total">Łącznie ${totalMb.toFixed(1)} MB</span>`;
});

$('uploadForm').addEventListener('submit', async (e) => {
  e.preventDefault();

  if (!selectedTextFile) { notify('Wybierz plik .txt z tekstem książki', 'error'); return; }
  if (!selectedAudioFiles.length) { notify('Wybierz pliki audio', 'error'); return; }

  if (NL.project && NL.project.text_present &&
      !confirm('Wgranie nowych materiałów usunie obecny projekt wraz z wynikami przetwarzania.\n\nKontynuować?')) {
    return;
  }

  const form = new FormData();
  form.append('text_file', selectedTextFile);
  selectedAudioFiles.forEach(f => form.append('audio_files', f));
  form.append('title', $('inputTitle').value.trim());
  form.append('author', $('inputAuthor').value.trim());

  const btn = $('btnUpload');
  btn.disabled = true;
  btn.textContent = '⏳ Wysyłanie...';
  $('uploadProgress').hidden = false;

  // XHR zamiast fetch — daje postęp wysyłki, co przy setkach MB audio jest niezbędne.
  await new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');

    xhr.upload.addEventListener('progress', (evt) => {
      if (!evt.lengthComputable) return;
      const pct = Math.round((evt.loaded / evt.total) * 100);
      $('uploadFill').style.width = `${pct}%`;
      $('uploadPercent').textContent = `${pct}%`;
    });

    xhr.addEventListener('load', async () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        notify('Materiały wgrane ✓', 'success');
        await loadProject();
        switchView('viewChapters');
        setTimeout(() => $('btnDetectChapters').click(), 300);
      } else {
        let detail = `Błąd ${xhr.status}`;
        try { detail = JSON.parse(xhr.responseText).detail || detail; } catch {}
        alert(`Nie udało się wgrać materiałów:\n\n${detail}`);
      }
      resolve();
    });

    xhr.addEventListener('error', () => { alert('Błąd połączenia podczas wysyłki.'); resolve(); });
    xhr.send(form);
  });

  btn.disabled = false;
  btn.textContent = '📥 Wgraj i utwórz projekt';
  $('uploadProgress').hidden = true;
  $('uploadFill').style.width = '0%';
});

$('btnResetProject').addEventListener('click', async () => {
  if (!confirm('Usunąć wszystkie pliki źródłowe i wyniki tego projektu?')) return;
  try {
    await api('/api/project/reset', { method: 'POST' });
    selectedTextFile = null;
    selectedAudioFiles = [];
    $('dropTextLabel').innerHTML = 'Przeciągnij plik <b>.txt</b> lub kliknij, aby wybrać';
    $('dropAudioLabel').innerHTML = 'Przeciągnij pliki <b>audio</b> (wiele naraz) lub kliknij';
    $('dropText').classList.remove('filled');
    $('dropAudio').classList.remove('filled');
    $('audioFileList').innerHTML = '';
    $('mapTableBody').innerHTML = '<tr><td colspan="6" class="table-empty">Kliknij „Wykryj rozdziały”.</td></tr>';
    notify('Projekt wyczyszczony', 'success');
    await loadProject();
  } catch (err) { alert(err.message); }
});

// Zapotrzebowanie modeli na VRAM (float16), zgodne z MODEL_VRAM_GB w transcriber.py.
const MODEL_VRAM_GB = {
  tiny: 0.5, base: 0.7, small: 1.0, medium: 2.5, 'large-v3': 4.7,
};

function updateVramNote() {
  const note = $('vramNote');
  if (!note) return;

  const needed = MODEL_VRAM_GB[$('setModel').value];
  const available = NL.vramMb ? NL.vramMb / 1024 : 0;

  if (!available || !needed) { note.textContent = ''; note.className = 'field-note'; return; }

  // Margines na kontekst CUDA i bufory dekodera - sam ciężar modelu to nie wszystko.
  if (needed > available - 0.6) {
    note.className = 'field-note warn';
    note.textContent = `⚠️ Model potrzebuje ok. ${needed} GB, a karta ma ${available.toFixed(1)} GB. ` +
      `Ładowanie prawdopodobnie się nie powiedzie i przetwarzanie zejdzie na CPU (kilkanaście razy wolniej).`;
  } else {
    note.className = 'field-note ok';
    note.textContent = `✓ Mieści się w pamięci karty (${needed} GB z ${available.toFixed(1)} GB).`;
  }
}

$('setModel').addEventListener('change', updateVramNote);

$('btnSaveSettings').addEventListener('click', async () => {
  try {
    await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model_size: $('setModel').value,
        device: $('setDevice').value,
        max_lines_per_board: parseInt($('setMaxLines').value, 10),
        max_chars_per_line: parseInt($('setMaxChars').value, 10),
      }),
    });
    notify('Ustawienia zapisane ✓', 'success');
    await loadProject();
  } catch (err) { alert(err.message); }
});

/* -------------------------------------------------- kreator rozdziałów */

$('btnDetectChapters').addEventListener('click', async () => {
  try {
    const { job } = await api('/api/chapters/detect', { method: 'POST' });
    watchJob(job.id, async () => {
      NL.proposal = await api('/api/chapters/proposal');
      renderProposal(NL.proposal);
    });
  } catch (err) { alert(err.message); }
});

function renderProposal(proposal) {
  NL.mapRows = proposal.chapters.map(c => ({ ...c }));
  NL.audioOptions = (proposal.audio_files || []).map(a => a.file);

  $('mapMethodHint').innerHTML =
    `Metoda: <code>${proposal.method}</code> — znaleziono <b>${proposal.chapters.length}</b> rozdziałów. ` +
    `Sprawdź przypisanie nagrań i w razie potrzeby popraw je przed zatwierdzeniem.`;

  $('mapWarnings').innerHTML = (proposal.warnings || [])
    .map(w => `<div class="warning-item">⚠️ ${w}</div>`).join('');

  renderMapTable();
  $('btnSaveMap').disabled = false;
}

function renderMapTable() {
  const body = $('mapTableBody');
  if (!NL.mapRows.length) {
    body.innerHTML = '<tr><td colspan="6" class="table-empty">Brak rozdziałów.</td></tr>';
    return;
  }

  body.innerHTML = NL.mapRows.map((row, idx) => {
    const conf = row.confidence != null ? row.confidence : 1;
    const confClass = conf >= 0.75 ? 'conf-high' : conf >= 0.45 ? 'conf-mid' : 'conf-low';
    const options = NL.audioOptions.map(f =>
      `<option value="${f}" ${f === row.audio_file ? 'selected' : ''}>${f}</option>`).join('');

    return `
      <tr data-idx="${idx}">
        <td class="cell-num">${row.chapter_num}</td>
        <td>
          <input class="cell-input map-header" value="${escapeAttr(row.header || '')}" data-field="header">
          <div class="cell-snippet" title="Początek rozdziału w tekście">${escapeHtml((row.snippet || '').slice(0, 120))}…</div>
        </td>
        <td>
          <select class="cell-input map-audio" data-field="audio_file">
            <option value="">— brak —</option>${options}
          </select>
        </td>
        <td class="cell-range">
          <input class="cell-input cell-tiny" type="number" value="${row.text_start}" data-field="text_start">
          <input class="cell-input cell-tiny" type="number" value="${row.text_end}" data-field="text_end">
        </td>
        <td><span class="conf-pill ${confClass}">${Math.round(conf * 100)}%</span></td>
        <td><button class="btn-micro btn-preview" data-idx="${idx}">👁️ Podgląd</button></td>
      </tr>`;
  }).join('');

  body.querySelectorAll('input, select').forEach(el => {
    el.addEventListener('change', (e) => {
      const idx = parseInt(e.target.closest('tr').dataset.idx, 10);
      const field = e.target.dataset.field;
      let value = e.target.value;
      if (field === 'text_start' || field === 'text_end') value = parseInt(value, 10) || 0;
      NL.mapRows[idx][field] = value;
      if (field === 'audio_file') {
        NL.mapRows[idx].audio_path = null;   // serwer odtworzy ścieżkę po nazwie pliku
      }
      NL.mapRows[idx].source = 'manual';
    });
  });

  body.querySelectorAll('.btn-preview').forEach(btn => {
    btn.addEventListener('click', async () => {
      const row = NL.mapRows[parseInt(btn.dataset.idx, 10)];
      try {
        const data = await api(`/api/text/slice?start=${row.text_start}&end=${Math.min(row.text_end, row.text_start + 1500)}`);
        alert(`Fragment tekstu [${data.start}–${data.end}] z ${data.total} znaków:\n\n${data.text}`);
      } catch (err) { alert(err.message); }
    });
  });
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function escapeAttr(str) { return escapeHtml(str); }

$('btnSaveMap').addEventListener('click', async () => {
  const missing = NL.mapRows.filter(r => !r.audio_file);
  if (missing.length && !confirm(
      `${missing.length} rozdział(ów) nie ma przypisanego nagrania i nie zostanie przetworzonych.\n\nZapisać mimo to?`)) {
    return;
  }
  try {
    await api('/api/chapters/map', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chapters: NL.mapRows }),
    });
    notify('Mapa rozdziałów zatwierdzona ✓', 'success');
    await loadProject();
    await refreshChapterStatus();
    if (typeof loadChapters === 'function') loadChapters();
  } catch (err) { alert(err.message); }
});

/* --------------------------------------------------------- przetwarzanie */

async function refreshChapterStatus() {
  let data;
  try { data = await api('/api/chapters'); }
  catch { return; }

  const body = $('chapterStatusBody');
  if (!data.chapters || !data.chapters.length) {
    body.innerHTML = '<tr><td colspan="7" class="table-empty">Brak rozdziałów — zatwierdź najpierw mapę.</td></tr>';
    return;
  }

  body.innerHTML = data.chapters.map(ch => {
    const status = ch.is_processed
      ? '<span class="pill pill-ok">gotowy</span>'
      : '<span class="pill pill-wait">oczekuje</span>';
    const rate = ch.match_rate != null ? `${ch.match_rate}%` : '—';
    const rateClass = ch.match_rate == null ? '' :
      ch.match_rate >= 90 ? 'rate-good' : ch.match_rate >= 70 ? 'rate-mid' : 'rate-bad';
    return `
      <tr>
        <td class="cell-num">${ch.number}</td>
        <td>${escapeHtml(ch.header || '')}</td>
        <td class="cell-file">${ch.audio_file ? escapeHtml(ch.audio_file) : '<em>brak</em>'}</td>
        <td>${ch.chunks_count || '—'}</td>
        <td class="${rateClass}">${rate}</td>
        <td>${status}</td>
        <td><button class="btn-micro btn-process-one" data-ch="${ch.number}">▶️ Przetwórz</button></td>
      </tr>`;
  }).join('');

  body.querySelectorAll('.btn-process-one').forEach(btn => {
    btn.addEventListener('click', () => startProcessing([parseInt(btn.dataset.ch, 10)]));
  });
}

async function startProcessing(chapters = null) {
  try {
    const { job } = await api('/api/process', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chapters, use_cache: $('chkUseCache').checked }),
    });
    watchJob(job.id, async (finished) => {
      const r = finished.result || {};
      if (r.failed && r.failed.length) {
        alert(`Przetworzono ${r.processed.length}/${r.total}.\n\nBłędy:\n` +
              r.failed.map(f => `• Rozdział ${f.chapter_num}: ${f.error}`).join('\n'));
      }
      await refreshChapterStatus();
      if (typeof loadChapters === 'function') loadChapters();
    });
  } catch (err) { alert(err.message); }
}

$('btnProcessAll').addEventListener('click', () => startProcessing(null));

/* ---------------------------------------------------------------- eksport */

$('btnBuildExport').addEventListener('click', async () => {
  try {
    const { job } = await api('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        book_name: $('exportName').value.trim() || null,
        slice_audio: $('chkSliceAudio').checked,
      }),
    });
    watchJob(job.id, (finished) => {
      const r = finished.result || {};
      $('exportSummary').hidden = false;
      $('exportSummary').innerHTML =
        `✅ Paczka <b>${escapeHtml(r.zip_name || '')}</b> — ` +
        `${r.chapters} rozdziałów, ${(r.size_bytes / 1048576).toFixed(1)} MB.`;
      $('btnDownloadZip').disabled = false;
    });
  } catch (err) { alert(err.message); }
});

$('btnDownloadZip').addEventListener('click', () => {
  const name = $('exportName').value.trim();
  window.location.href = `/api/export/download${name ? `?book_name=${encodeURIComponent(name)}` : ''}`;
});

$('btnExportZip').addEventListener('click', () => switchView('viewExport'));

/* ------------------------------------------------------------------ start */

window.addEventListener('DOMContentLoaded', async () => {
  await loadProject();
  if (NL.project && NL.project.chapter_map_count) {
    switchView('viewChapters');
  }
});
