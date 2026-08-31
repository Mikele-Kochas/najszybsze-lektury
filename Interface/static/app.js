// Application State
let currentMode = 'random'; // 'random' or 'continuous'
let currentChapterNum = 1;
let currentChapterData = null;
let allChapterChunks = [];
let activeChunkId = null;

let currentSample = null;
let isPlaying = false;
let startTime = 0;
let endTime = 0;
let audioDuration = 0;
let playbackSpeed = 1.0;

// DOM Elements
const btnModeRandom = document.getElementById('btnModeRandom');
const btnModeContinuous = document.getElementById('btnModeContinuous');

const chapterSelect = document.getElementById('chapterSelect');
const btnProcessChapter = document.getElementById('btnProcessChapter');
const btnNewSample = document.getElementById('btnNewSample');

const badgeChapter = document.getElementById('badgeChapter');
const badgeChunkInfo = document.getElementById('badgeChunkInfo');
const badgeType = document.getElementById('badgeType');
const badgeLines = document.getElementById('badgeLines');

const boardCard = document.getElementById('boardCard');
const boardContent = document.getElementById('boardContent');
const timeStart = document.getElementById('timeStart');
const timeEnd = document.getElementById('timeEnd');
const durationVal = document.getElementById('durationVal');

const audioElement = document.getElementById('audioElement');
const btnPlayPause = document.getElementById('btnPlayPause');
const playIcon = document.getElementById('playIcon');
const btnReplay = document.getElementById('btnReplay');
const progressBar = document.getElementById('progressBar');
const progressFill = document.getElementById('progressFill');
const progressScrubber = document.getElementById('progressScrubber');
const chkLoop = document.getElementById('chkLoop');

const btnOk = document.getElementById('btnOk');
const btnMismatch = document.getElementById('btnMismatch');
const btnMissing = document.getElementById('btnMissing');
const reviewComment = document.getElementById('reviewComment');
const btnSaveReview = document.getElementById('btnSaveReview');

const timelineSearch = document.getElementById('timelineSearch');
const timelineTotalCount = document.getElementById('timelineTotalCount');
const timelineList = document.getElementById('timelineList');

const whisperRawText = document.getElementById('whisperRawText');
const bookOriginalText = document.getElementById('bookOriginalText');
const prevChunkText = document.getElementById('prevChunkText');
const currChunkContextText = document.getElementById('currChunkContextText');
const nextChunkText = document.getElementById('nextChunkText');

const statAccuracy = document.getElementById('statAccuracy');
const statTotal = document.getElementById('statTotal');
const statCorrect = document.getElementById('statCorrect');
const statIncorrect = document.getElementById('statIncorrect');
const historyList = document.getElementById('historyList');
const toast = document.getElementById('toast');

// Format seconds to SRT format string (HH:MM:SS,mmm)
function formatSRTTime(seconds) {
  if (isNaN(seconds) || seconds < 0) seconds = 0;
  const totalMs = Math.round(seconds * 1000);
  const ms = totalMs % 1000;
  const totalSec = Math.floor(totalMs / 1000);
  const sec = totalSec % 60;
  const totalMin = Math.floor(totalSec / 60);
  const min = totalMin % 60;
  const hours = Math.floor(totalMin / 60);
  
  const pad = (n, z = 2) => String(n).padStart(z, '0');
  return `${pad(hours)}:${pad(min)}:${pad(sec)},${pad(ms, 3)}`;
}

// Toast helper
function showToast(message, type = 'info') {
  toast.textContent = message;
  toast.className = `toast show ${type}`;
  setTimeout(() => {
    toast.className = 'toast';
  }, 2400);
}

// Mode Switchers
btnModeRandom.addEventListener('click', () => {
  setMode('random');
});

btnModeContinuous.addEventListener('click', () => {
  setMode('continuous');
});

function setMode(mode) {
  currentMode = mode;
  if (mode === 'random') {
    btnModeRandom.classList.add('active');
    btnModeContinuous.classList.remove('active');
    btnNewSample.style.display = 'inline-flex';
    chkLoop.parentElement.style.display = 'flex';
    btnReplay.style.display = 'inline-flex';
    showToast('Przełączono w tryb losowych próbek (Blind QA)');
    loadSample();
  } else {
    btnModeContinuous.classList.add('active');
    btnModeRandom.classList.remove('active');
    btnNewSample.style.display = 'none';
    chkLoop.parentElement.style.display = 'none';
    btnReplay.style.display = 'none';
    showToast('Przełączono w tryb ciągłego odtwarzania rozdziału');
    
    // Switch to Timeline tab automatically
    activateTab('tabTimeline');
    
    // Load full chapter continuous mode
    const ch = chapterSelect.value ? parseInt(chapterSelect.value) : 1;
    loadContinuousChapter(ch);
  }
}

function activateTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-tab') === tabId);
  });
  document.querySelectorAll('.tab-content').forEach(c => {
    c.classList.toggle('active', c.id === tabId);
  });
}

// Load Chapters
async function loadChapters() {
  try {
    const res = await fetch('/api/chapters');
    const data = await res.json();
    chapterSelect.innerHTML = '<option value="">Wszystkie / Losowy</option>';
    
    data.chapters.forEach(ch => {
      const opt = document.createElement('option');
      opt.value = ch.number;
      const statusIcon = ch.is_processed ? '✅' : '⏳';
      const matchRate = ch.match_rate ? ` (${ch.match_rate}%)` : '';
      opt.textContent = `${statusIcon} Rozdział ${ch.roman}: ${ch.title}${matchRate}`;
      chapterSelect.appendChild(opt);
    });
  } catch (err) {
    console.error("Failed loading chapters:", err);
  }
}

// Load Continuous Chapter
async function loadContinuousChapter(chapterNum = 1, startAtTime = 0.0) {
  try {
    currentChapterNum = chapterNum;
    boardContent.innerHTML = '<em>Ładowanie pełnego rozdziału...</em>';

    const res = await fetch(`/api/chapter/${chapterNum}`);
    if (!res.ok) {
      const err = await res.json();
      boardContent.innerHTML = `<span style="color:#f43f5e">Brak przetworzonego rozdziału ${chapterNum}. Kliknij "Przetwórz".</span>`;
      timelineList.innerHTML = '<div class="timeline-empty">Rozdział nie jest jeszcze przetworzony.</div>';
      return;
    }

    const data = await res.json();
    currentChapterData = data;
    allChapterChunks = data.chunks || [];
    currentSample = null;
    activeChunkId = null;

    badgeChapter.textContent = data.chapter_header;
    timelineTotalCount.textContent = `${allChapterChunks.length} plansz`;

    // Render Timeline List
    renderTimelineList(allChapterChunks);

    // Setup Continuous Audio
    setupAudioContinuous(chapterNum, startAtTime, data.duration);

    // Activate initial chunk at startAtTime
    updateActiveChunkForTime(startAtTime);

  } catch (err) {
    console.error("Error loading continuous chapter:", err);
    boardContent.innerHTML = `<span style="color:#f43f5e">Błąd ładowania rozdziału: ${err.message}</span>`;
  }
}

// Render Timeline List
function renderTimelineList(chunks, filterText = '') {
  const q = filterText.toLowerCase().trim();
  const filtered = q ? chunks.filter(c => c.text.toLowerCase().includes(q)) : chunks;

  if (filtered.length === 0) {
    timelineList.innerHTML = '<div class="timeline-empty">Brak plansz pasujących do filtra.</div>';
    return;
  }

  timelineList.innerHTML = filtered.map(c => {
    const isActive = c.chunk_id === activeChunkId ? 'active' : '';
    const typeLabel = c.chunk_type === 'dialogue' ? '💬 Dialog' : (c.chunk_type === 'intro_outro' ? '⚠️ Wtrącenie' : '📖 Narracja');
    return `
      <div class="timeline-item ${isActive}" data-id="${c.chunk_id}" data-start="${c.start_time}" data-end="${c.end_time}">
        <div class="timeline-item-meta">
          <span class="timeline-item-title">Plansza #${c.chunk_id} • ${typeLabel}</span>
          <span class="timeline-item-time">${formatSRTTime(c.start_time)} ➔ ${formatSRTTime(c.end_time)} (${c.duration}s)</span>
        </div>
        <div class="timeline-item-text">${c.text}</div>
      </div>
    `;
  }).join('');

  // Attach click listeners to timeline items
  document.querySelectorAll('.timeline-item').forEach(item => {
    item.addEventListener('click', () => {
      const start = parseFloat(item.getAttribute('data-start'));
      const chunkId = parseInt(item.getAttribute('data-id'));
      
      if (currentMode !== 'continuous') {
        setMode('continuous');
      }

      audioElement.currentTime = start;
      updateActiveChunkForTime(start);
      if (!isPlaying) {
        audioElement.play().then(() => {
          isPlaying = true;
          updatePlayButton(true);
        });
      }
    });
  });
}

// Timeline Search Filter
if (timelineSearch) {
  timelineSearch.addEventListener('input', (e) => {
    if (allChapterChunks.length > 0) {
      renderTimelineList(allChapterChunks, e.target.value);
    }
  });
}

// Update Active Chunk For Current Audio Time (Continuous Mode)
function updateActiveChunkForTime(curTime) {
  if (!allChapterChunks || allChapterChunks.length === 0) return;

  // Find chunk containing curTime
  let chunk = allChapterChunks.find(c => curTime >= c.start_time && curTime <= c.end_time);
  
  // If in a small pause between chunks, find nearest upcoming chunk
  if (!chunk) {
    chunk = allChapterChunks.find(c => c.start_time > curTime);
    if (!chunk) chunk = allChapterChunks[allChapterChunks.length - 1];
  }

  if (chunk && chunk.chunk_id !== activeChunkId) {
    activeChunkId = chunk.chunk_id;
    renderActiveBoard(chunk);

    // Highlight in timeline list
    document.querySelectorAll('.timeline-item').forEach(el => {
      const id = parseInt(el.getAttribute('data-id'));
      if (id === activeChunkId) {
        el.classList.add('active');
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } else {
        el.classList.remove('active');
      }
    });

    // Update Context Tab
    const idx = allChapterChunks.findIndex(c => c.chunk_id === chunk.chunk_id);
    const prevC = idx > 0 ? allChapterChunks[idx - 1] : null;
    const nextC = idx < allChapterChunks.length - 1 ? allChapterChunks[idx + 1] : null;
    prevChunkText.textContent = prevC ? `#${prevC.chunk_id}: ${prevC.text}` : '(początek rozdziału)';
    currChunkContextText.textContent = `#${chunk.chunk_id}: ${chunk.text}`;
    nextChunkText.textContent = nextC ? `#${nextC.chunk_id}: ${nextC.text}` : '(koniec rozdziału)';

    // Update Diff Tab with Whisper segments overlapping this chunk
    if (currentChapterData && currentChapterData.whisper_segments) {
      const wTexts = currentChapterData.whisper_segments
        .filter(w => !(w.end < chunk.start_time || w.start > chunk.end_time))
        .map(w => w.text);
      whisperRawText.textContent = wTexts.join(' ') || '(brak transkrypcji w tym oknie)';
    }
    bookOriginalText.textContent = chunk.text;
  }
}

// Render Board Card
function renderActiveBoard(chunk) {
  // Flash animation on board transition
  boardCard.style.transform = 'scale(0.99)';
  setTimeout(() => { boardCard.style.transform = 'scale(1)'; }, 100);

  badgeChunkInfo.textContent = `Plansza #${chunk.chunk_id} / ${allChapterChunks.length}`;
  badgeType.textContent = chunk.chunk_type === 'dialogue' ? '💬 Dialog' : (chunk.chunk_type === 'intro_outro' ? '⚠️ Wtrącenie' : '📖 Narracja');
  badgeLines.textContent = `${chunk.lines_count} / 11 linii`;

  boardContent.textContent = chunk.text;

  timeStart.textContent = formatSRTTime(chunk.start_time);
  timeEnd.textContent = formatSRTTime(chunk.end_time);
  durationVal.textContent = `${chunk.duration || (chunk.end_time - chunk.start_time).toFixed(1)}s`;
}

// Setup Audio for Continuous Mode
function setupAudioContinuous(chapterNum, startTime = 0.0, duration = 0.0) {
  audioElement.pause();
  isPlaying = false;
  updatePlayButton(false);

  const audioSrc = `/api/audio/${chapterNum}`;
  if (audioElement.getAttribute('data-src') !== audioSrc) {
    audioElement.setAttribute('data-src', audioSrc);
    audioElement.src = audioSrc;
    audioElement.load();
  }

  audioElement.playbackRate = playbackSpeed;
  audioElement.currentTime = startTime;
  updateProgressBar(0);
}

// Setup Audio for Snippet Random Mode
function setupAudioSnippet(chapterNum, start, end) {
  audioElement.pause();
  isPlaying = false;
  updatePlayButton(false);

  const audioSrc = `/api/audio/${chapterNum}`;
  if (audioElement.getAttribute('data-src') !== audioSrc) {
    audioElement.setAttribute('data-src', audioSrc);
    audioElement.src = audioSrc;
    audioElement.load();
  }

  startTime = start;
  endTime = end;
  audioDuration = Math.max(0.1, endTime - startTime);

  audioElement.playbackRate = playbackSpeed;
  audioElement.currentTime = start;
  updateProgressBar(0);
}

// Load Random Sample (QA Mode)
async function loadSample(targetChapter = null) {
  try {
    const chVal = targetChapter || chapterSelect.value;
    const url = chVal ? `/api/sample?chapter_num=${chVal}` : '/api/sample';
    
    boardContent.innerHTML = '<em>Ładowanie losowej planszy...</em>';
    const res = await fetch(url);
    if (!res.ok) {
      const err = await res.json();
      boardContent.innerHTML = `<span style="color:#f43f5e">Brak danych: ${err.detail}</span>`;
      return;
    }

    const data = await res.json();
    currentSample = data;
    currentChapterNum = data.chapter_num;

    const chunk = data.chunk;
    startTime = chunk.start_time;
    endTime = chunk.end_time;
    audioDuration = Math.max(0.1, endTime - startTime);

    badgeChapter.textContent = data.chapter_header;
    renderActiveBoard(chunk);

    whisperRawText.textContent = data.whisper_text || '(brak transkrypcji w tym oknie)';
    bookOriginalText.textContent = chunk.text;

    prevChunkText.textContent = data.prev_chunk ? `#${data.prev_chunk.chunk_id}: ${data.prev_chunk.text}` : '(początek rozdziału)';
    currChunkContextText.textContent = `#${chunk.chunk_id}: ${chunk.text}`;
    nextChunkText.textContent = data.next_chunk ? `#${data.next_chunk.chunk_id}: ${data.next_chunk.text}` : '(koniec rozdziału)';

    setupAudioSnippet(data.chapter_num, startTime, endTime);

    // Also populate timeline in background for current chapter if available
    fetch(`/api/chapter/${data.chapter_num}`).then(r => r.json()).then(chData => {
      currentChapterData = chData;
      allChapterChunks = chData.chunks || [];
      timelineTotalCount.textContent = `${allChapterChunks.length} plansz`;
      renderTimelineList(allChapterChunks);
    }).catch(() => {});

    updateStats();
  } catch (err) {
    console.error("Error loading sample:", err);
    boardContent.innerHTML = `<span style="color:#f43f5e">Błąd połączenia z serwerem.</span>`;
  }
}

// Audio Controls
function playAudio() {
  if (currentMode === 'random') {
    if (audioElement.currentTime < startTime || audioElement.currentTime >= endTime - 0.05) {
      audioElement.currentTime = startTime;
    }
  }
  audioElement.play().then(() => {
    isPlaying = true;
    updatePlayButton(true);
  }).catch(err => {
    console.error("Play error:", err);
  });
}

function pauseAudio() {
  audioElement.pause();
  isPlaying = false;
  updatePlayButton(false);
}

function togglePlayPause() {
  if (isPlaying) {
    pauseAudio();
  } else {
    playAudio();
  }
}

function replaySnippet() {
  if (currentMode === 'random') {
    audioElement.currentTime = startTime;
  } else if (activeChunkId && allChapterChunks.length > 0) {
    const chunk = allChapterChunks.find(c => c.chunk_id === activeChunkId);
    if (chunk) audioElement.currentTime = chunk.start_time;
  }
  playAudio();
}

function updatePlayButton(playing) {
  playIcon.textContent = playing ? '❚❚' : '▶';
}

function updateProgressBar(fraction) {
  const pct = Math.max(0, Math.min(100, fraction * 100));
  progressFill.style.width = `${pct}%`;
  progressScrubber.style.left = `${pct}%`;
}

// Audio Time Update listener
audioElement.addEventListener('timeupdate', () => {
  const cur = audioElement.currentTime;

  if (currentMode === 'continuous') {
    // Continuous chapter mode
    updateActiveChunkForTime(cur);

    const totalDur = audioElement.duration || (currentChapterData ? currentChapterData.duration : 1.0);
    const progress = totalDur > 0 ? (cur / totalDur) : 0;
    updateProgressBar(progress);

  } else {
    // Random sample snippet mode
    if (!currentSample) return;
    
    if (cur >= endTime) {
      if (chkLoop.checked) {
        audioElement.currentTime = startTime;
        audioElement.play();
      } else {
        pauseAudio();
        audioElement.currentTime = startTime;
        updateProgressBar(0);
      }
      return;
    }

    const progress = (cur - startTime) / audioDuration;
    updateProgressBar(progress);
  }
});

// Click on progress bar to seek
progressBar.addEventListener('click', (e) => {
  const rect = progressBar.getBoundingClientRect();
  const clickX = e.clientX - rect.left;
  const fraction = Math.max(0, Math.min(1, clickX / rect.width));

  if (currentMode === 'continuous') {
    const totalDur = audioElement.duration || (currentChapterData ? currentChapterData.duration : 1.0);
    audioElement.currentTime = fraction * totalDur;
    updateActiveChunkForTime(audioElement.currentTime);
  } else {
    audioElement.currentTime = startTime + fraction * audioDuration;
  }
  updateProgressBar(fraction);
});

// Review Submission
async function submitReview(isCorrect, status) {
  const chunk = currentMode === 'continuous' 
    ? (allChapterChunks.find(c => c.chunk_id === activeChunkId) || allChapterChunks[0])
    : (currentSample ? currentSample.chunk : null);

  if (!chunk) return;

  const payload = {
    chapter_num: currentChapterNum,
    chunk_id: chunk.chunk_id,
    is_correct: isCorrect,
    status: status,
    comment: reviewComment.value.trim(),
    chunk_text: chunk.text,
    start_time: chunk.start_time,
    end_time: chunk.end_time
  };

  try {
    const res = await fetch('/api/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      const msg = isCorrect ? `Zapisano Planszę #${chunk.chunk_id}: Zgodny (OK) ✓` : `Zapisano uwagę (${status}) dla Planszy #${chunk.chunk_id} ✕`;
      showToast(msg, isCorrect ? 'success' : 'error');
      
      if (currentMode === 'random') {
        setTimeout(() => loadSample(), 250);
      }
    }
  } catch (err) {
    console.error("Failed saving review:", err);
  }
}

// Update Stats
async function updateStats() {
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();

    statAccuracy.textContent = `${data.accuracy_pct}%`;
    statTotal.textContent = data.total;
    statCorrect.textContent = data.correct;
    statIncorrect.textContent = data.incorrect;

    if (data.recent_reviews && data.recent_reviews.length > 0) {
      historyList.innerHTML = data.recent_reviews.map(r => {
        const icon = r.is_correct ? '✅' : '❌';
        const tag = r.status === 'ok' ? 'OK' : (r.status === 'mismatch' ? 'Błąd' : 'Wtrącenie');
        const preview = (r.chunk_text || '').substring(0, 45) + '...';
        return `
          <div class="history-item">
            <span>${icon} [Ch ${r.chapter_num} #${r.chunk_id}] ${preview}</span>
            <span class="badge badge-neutral">${tag}</span>
          </div>
        `;
      }).join('');
    }
  } catch (err) {
    console.error("Stats update failed:", err);
  }
}

// Process Chapter Endpoint Call
async function processSelectedChapter() {
  const chNum = chapterSelect.value;
  if (!chNum) {
    alert("Wybierz konkretny rozdział z listy, aby go przetworzyć.");
    return;
  }
  btnProcessChapter.disabled = true;
  btnProcessChapter.textContent = "⏳ Przetwarzanie...";

  try {
    // Przetwarzanie jest zadaniem w tle — pasek postępu prowadzi setup.js,
    // a po jego zakończeniu odświeżamy widok bieżącego rozdziału.
    const res = await fetch('/api/process', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chapters: [parseInt(chNum, 10)], use_cache: true })
    });
    const data = await res.json();
    if (!res.ok) {
      alert(`Błąd przetwarzania: ${data.detail}`);
      return;
    }

    watchJob(data.job.id, async () => {
      await loadChapters();
      chapterSelect.value = chNum;
      if (currentMode === 'continuous') {
        loadContinuousChapter(parseInt(chNum, 10));
      } else {
        loadSample(parseInt(chNum, 10));
      }
    });
  } catch (err) {
    alert(`Błąd: ${err.message}`);
  } finally {
    btnProcessChapter.disabled = false;
    btnProcessChapter.textContent = "⚙️ Przetwórz";
  }
}

// New DOM Elements for Edit & Export
const btnExportZip = document.getElementById('btnExportZip');
const btnEditBoard = document.getElementById('btnEditBoard');
const unalignedBanner = document.getElementById('unalignedBanner');
const btnAcceptWhisper = document.getElementById('btnAcceptWhisper');
const boardEditor = document.getElementById('boardEditor');
const boardEditControls = document.getElementById('boardEditControls');
const btnSaveBoardEdit = document.getElementById('btnSaveBoardEdit');
const btnCancelBoardEdit = document.getElementById('btnCancelBoardEdit');

// Update renderActiveBoard to check for unaligned banner
const originalRenderActiveBoard = renderActiveBoard;
renderActiveBoard = function(chunk) {
  originalRenderActiveBoard(chunk);

  // Close editor if open
  closeBoardEditor();

  // Check if chunk is unaligned / intro-outro
  const isUnaligned = chunk.chunk_type === 'intro_outro' || chunk.text.includes('(brak tekstu w pliku źródłowym)');
  if (unalignedBanner) {
    unalignedBanner.style.display = isUnaligned ? 'flex' : 'none';
  }
};

// Inline Board Editor Functions
function openBoardEditor() {
  const currentChunk = currentMode === 'continuous'
    ? (allChapterChunks.find(c => c.chunk_id === activeChunkId) || allChapterChunks[0])
    : (currentSample ? currentSample.chunk : null);

  if (!currentChunk) return;

  boardContent.style.display = 'none';
  boardEditor.style.display = 'block';
  boardEditControls.style.display = 'flex';
  boardEditor.value = currentChunk.text;
  boardEditor.focus();
}

function closeBoardEditor() {
  if (boardContent && boardEditor && boardEditControls) {
    boardContent.style.display = 'block';
    boardEditor.style.display = 'none';
    boardEditControls.style.display = 'none';
  }
}

async function saveBoardEdit() {
  const newText = boardEditor.value.trim();
  if (!newText) {
    alert("Tekst planszy nie może być pusty.");
    return;
  }

  const currentChunk = currentMode === 'continuous'
    ? (allChapterChunks.find(c => c.chunk_id === activeChunkId) || allChapterChunks[0])
    : (currentSample ? currentSample.chunk : null);

  if (!currentChunk) return;

  try {
    const res = await fetch('/api/chunk/edit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chapter_num: currentChapterNum,
        chunk_id: currentChunk.chunk_id,
        text: newText
      })
    });

    if (res.ok) {
      const data = await res.json();
      currentChunk.text = newText;
      currentChunk.chunk_type = 'edited';
      boardContent.textContent = newText;
      closeBoardEditor();
      showToast(`Zapisano zmiany w Planszy #${currentChunk.chunk_id} ✓`, 'success');

      // Refresh timeline list
      if (allChapterChunks.length > 0) {
        renderTimelineList(allChapterChunks);
      }
    } else {
      alert("Błąd zapisu zmian.");
    }
  } catch (err) {
    console.error("Save edit error:", err);
  }
}

// Accept Whisper Transcription for unaligned chunks
if (btnAcceptWhisper) {
  btnAcceptWhisper.addEventListener('click', async () => {
    const rawWhisper = whisperRawText.textContent.trim();
    if (!rawWhisper || rawWhisper.startsWith('(')) {
      alert("Brak transkrypcji Whisper dla tej planszy.");
      return;
    }
    boardEditor.value = rawWhisper;
    await saveBoardEdit();
  });
}

if (btnEditBoard) btnEditBoard.addEventListener('click', openBoardEditor);
if (btnSaveBoardEdit) btnSaveBoardEdit.addEventListener('click', saveBoardEdit);
if (btnCancelBoardEdit) btnCancelBoardEdit.addEventListener('click', closeBoardEditor);

// Eksport obsługuje widok „Eksport" w setup.js — tam nazwa paczki pochodzi z projektu,
// a budowanie ZIP-a idzie przez zadanie w tle z paskiem postępu.

// Speed Buttons
document.querySelectorAll('.btn-speed').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.btn-speed').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    playbackSpeed = parseFloat(btn.getAttribute('data-speed'));
    audioElement.playbackRate = playbackSpeed;
  });
});

// Tab Switching
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    const tabId = btn.getAttribute('data-tab');
    document.getElementById(tabId).classList.add('active');
  });
});

// Event Listeners
btnPlayPause.addEventListener('click', togglePlayPause);
btnReplay.addEventListener('click', replaySnippet);
btnNewSample.addEventListener('click', () => loadSample());

chapterSelect.addEventListener('change', () => {
  const ch = chapterSelect.value ? parseInt(chapterSelect.value) : 1;
  if (currentMode === 'continuous') {
    loadContinuousChapter(ch);
  } else {
    loadSample(ch);
  }
});

btnProcessChapter.addEventListener('click', processSelectedChapter);

btnOk.addEventListener('click', () => submitReview(true, 'ok'));
btnMismatch.addEventListener('click', () => submitReview(false, 'mismatch'));
btnMissing.addEventListener('click', () => submitReview(false, 'missing_source'));
btnSaveReview.addEventListener('click', () => {
  if (reviewComment.value.trim()) {
    submitReview(true, 'comment_only');
  }
});

// Global Keyboard Shortcuts
window.addEventListener('keydown', (e) => {
  // Skróty działają wyłącznie w widoku weryfikacji — w kreatorze cyfry i spacja
  // trafiałyby w akcje oceny zamiast do pól formularza.
  const studio = document.getElementById('viewStudio');
  if (!studio || !studio.classList.contains('active')) return;

  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;

  if (e.code === 'Space') {
    e.preventDefault();
    togglePlayPause();
  } else if (e.code === 'KeyR') {
    e.preventDefault();
    replaySnippet();
  } else if (e.key === '1') {
    e.preventDefault();
    submitReview(true, 'ok');
  } else if (e.key === '2') {
    e.preventDefault();
    submitReview(false, 'mismatch');
  } else if (e.key === '3') {
    e.preventDefault();
    submitReview(false, 'missing_source');
  } else if (e.code === 'KeyN') {
    e.preventDefault();
    if (currentMode === 'random') loadSample();
  } else if (e.code === 'ArrowRight') {
    // Seek +5s
    audioElement.currentTime = Math.min(audioElement.duration || 99999, audioElement.currentTime + 5);
  } else if (e.code === 'ArrowLeft') {
    // Seek -5s
    audioElement.currentTime = Math.max(0, audioElement.currentTime - 5);
  }
});

// Studio startuje dopiero przy wejściu w widok „Weryfikacja” (patrz switchView w setup.js) —
// przy pustym projekcie nie ma czego losować.
window.addEventListener('DOMContentLoaded', () => {
  loadChapters();
});

