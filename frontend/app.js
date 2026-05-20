/* ════════════════════════════════════════════════════════════
   JARVIS PWA — app.js
   ════════════════════════════════════════════════════════════ */

const API_BASE = '';   // same-origin (served by FastAPI)

// ──────────────────────────────────────────────────────────────────────
// STATE
// ──────────────────────────────────────────────────────────────────────
const state = {
  mode: 'talk',                  // 'talk' | 'ambient' | 'archive'
  // Talk mode
  talkRecorder: null,
  talkStream: null,
  talkChunks: [],
  talkPressing: false,
  // Ambient mode
  ambientRecorder: null,
  ambientStream: null,
  ambientChunks: [],
  ambientStartTs: 0,
  ambientTimerInterval: null,
  ambientAudioCtx: null,
  ambientAnalyser: null,
  ambientWaveformFrame: null,
};

// ──────────────────────────────────────────────────────────────────────
// SHORTHANDS
// ──────────────────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ──────────────────────────────────────────────────────────────────────
// MODE SWITCHING
// ──────────────────────────────────────────────────────────────────────
function switchMode(mode) {
  state.mode = mode;
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.mode === mode));
  $$('.screen').forEach(s => s.classList.toggle('active', s.dataset.screen === mode));
  if (mode === 'archive') loadMemos();
}

$$('.tab').forEach(tab => {
  tab.addEventListener('click', () => switchMode(tab.dataset.mode));
});

// ──────────────────────────────────────────────────────────────────────
// STATUS BAR
// ──────────────────────────────────────────────────────────────────────
function setStatus(text, isError = false) {
  $('#statusText').textContent = text;
  $('#status').classList.toggle('error', isError);
}

async function checkHealth() {
  try {
    const r = await fetch(`${API_BASE}/api/health`);
    if (!r.ok) throw new Error('bad status');
    const data = await r.json();
    if (!data.openai || !data.anthropic) setStatus('check keys', true);
    else if (!data.elevenlabs) setStatus('no voice', true);
    else setStatus('ready');
  } catch {
    setStatus('offline', true);
  }
}
checkHealth();
setInterval(checkHealth, 30000);

// ══════════════════════════════════════════════════════════════════════
// MODE 1 — TALK
// ══════════════════════════════════════════════════════════════════════

const talkBtn = $('#talkButton');
const talkCap = $('#talkCaption');
const talkOrb = $('#talkOrb');
const talkTranscript = $('#talkTranscript');

async function ensureTalkStream() {
  if (state.talkStream) return state.talkStream;
  try {
    state.talkStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    return state.talkStream;
  } catch (e) {
    talkCap.textContent = 'mic blocked';
    setStatus('mic denied', true);
    throw e;
  }
}

async function startTalk() {
  if (state.talkRecorder?.state === 'recording') return;
  const stream = await ensureTalkStream();
  state.talkChunks = [];
  const recorder = new MediaRecorder(stream, { mimeType: pickMime() });
  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) state.talkChunks.push(e.data);
  };
  recorder.onstop = handleTalkStop;
  recorder.start();
  state.talkRecorder = recorder;
  talkOrb.classList.add('listening');
  talkCap.textContent = 'listening…';
  talkBtn.classList.add('active');
}

function stopTalk() {
  if (state.talkRecorder?.state === 'recording') {
    state.talkRecorder.stop();
  }
  talkBtn.classList.remove('active');
  talkOrb.classList.remove('listening');
  talkOrb.classList.add('thinking');
  talkCap.textContent = 'thinking…';
}

async function handleTalkStop() {
  const blob = new Blob(state.talkChunks, { type: state.talkRecorder.mimeType });
  state.talkChunks = [];
  if (blob.size < 500) {
    talkOrb.classList.remove('thinking');
    talkCap.textContent = 'too short — hold to talk';
    setTimeout(() => (talkCap.textContent = 'Hold to talk'), 1500);
    return;
  }

  try {
    const form = new FormData();
    form.append('audio', blob, 'talk.webm');
    const r = await fetch(`${API_BASE}/api/talk`, { method: 'POST', body: form });
    if (!r.ok) throw new Error(`server ${r.status}`);

    const userText = decodeHeader(r.headers.get('X-User-Text'));
    const jarvisText = decodeHeader(r.headers.get('X-Jarvis-Text'));
    appendTalkTurn('you', userText);
    appendTalkTurn('jarvis', jarvisText);

    const audioBlob = await r.blob();
    const audioUrl = URL.createObjectURL(audioBlob);
    const audio = new Audio(audioUrl);

    talkOrb.classList.remove('thinking');
    talkOrb.classList.add('speaking');
    talkCap.textContent = 'jarvis…';

    audio.onended = () => {
      URL.revokeObjectURL(audioUrl);
      talkOrb.classList.remove('speaking');
      talkCap.textContent = 'Hold to talk';
    };
    audio.onerror = audio.onended;
    await audio.play();
  } catch (err) {
    console.error(err);
    talkOrb.classList.remove('thinking', 'speaking');
    talkCap.textContent = 'error — try again';
    setStatus('talk failed', true);
    setTimeout(() => {
      talkCap.textContent = 'Hold to talk';
      checkHealth();
    }, 2200);
  }
}

function appendTalkTurn(role, text) {
  if (!text) return;
  talkTranscript.classList.add('visible');
  const div = document.createElement('div');
  div.className = `turn ${role === 'jarvis' ? 'jarvis' : 'you'}`;
  div.innerHTML = `<span class="role">${role}</span>${escapeHtml(text)}`;
  talkTranscript.appendChild(div);
  talkTranscript.scrollTop = talkTranscript.scrollHeight;
}

// Hold-to-talk on the big button
talkBtn.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  state.talkPressing = true;
  startTalk().catch(() => { state.talkPressing = false; });
});
const endPress = () => {
  if (state.talkPressing) {
    state.talkPressing = false;
    stopTalk();
  }
};
talkBtn.addEventListener('pointerup', endPress);
talkBtn.addEventListener('pointerleave', endPress);
talkBtn.addEventListener('pointercancel', endPress);

// ══════════════════════════════════════════════════════════════════════
// MODE 2 — AMBIENT RECORDING
// ══════════════════════════════════════════════════════════════════════

const recordBtn = $('#recordButton');
const recorderTime = $('#recorderTime');
const recorderState = $('#recorderState');
const waveformCanvas = $('#waveform');
const procPane = $('#processingPane');

async function ensureAmbientStream() {
  if (state.ambientStream) return state.ambientStream;
  state.ambientStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  return state.ambientStream;
}

async function startAmbient() {
  try {
    const stream = await ensureAmbientStream();
    state.ambientChunks = [];
    const recorder = new MediaRecorder(stream, { mimeType: pickMime() });
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) state.ambientChunks.push(e.data);
    };
    recorder.onstop = handleAmbientStop;
    recorder.start(1000); // 1s chunks
    state.ambientRecorder = recorder;
    state.ambientStartTs = Date.now();

    recordBtn.classList.add('recording');
    recorderState.textContent = 'recording';
    recorderState.classList.add('live');
    procPane.classList.remove('visible');
    resetProcessingSteps();

    startAmbientTimer();
    startWaveform(stream);
  } catch (e) {
    recorderState.textContent = 'mic denied';
    setStatus('mic denied', true);
  }
}

function stopAmbient() {
  if (state.ambientRecorder?.state === 'recording') {
    state.ambientRecorder.stop();
  }
  stopAmbientTimer();
  stopWaveform();
  recordBtn.classList.remove('recording');
  recorderState.textContent = 'processing';
  recorderState.classList.remove('live');
  recorderState.classList.add('processing');
}

async function handleAmbientStop() {
  const blob = new Blob(state.ambientChunks, { type: state.ambientRecorder.mimeType });
  state.ambientChunks = [];
  const duration = Math.round((Date.now() - state.ambientStartTs) / 1000);

  if (blob.size < 1000) {
    recorderState.textContent = 'too short';
    recorderState.classList.remove('processing');
    setTimeout(() => { recorderState.textContent = 'idle'; recorderTime.textContent = '00:00'; }, 1500);
    return;
  }

  procPane.classList.add('visible');
  setProcStep('upload', 'active');

  try {
    const form = new FormData();
    form.append('audio', blob, 'ambient.webm');
    form.append('duration_seconds', String(duration));

    setProcStep('upload', 'done');
    setProcStep('transcribe', 'active');

    const r = await fetch(`${API_BASE}/api/ambient`, { method: 'POST', body: form });
    if (!r.ok) throw new Error(`server ${r.status}`);
    const data = await r.json();

    setProcStep('transcribe', 'done');
    setProcStep('extract', 'active');

    // The extract + post step runs async on the server,
    // we just show the user a friendly arc here
    setTimeout(() => {
      setProcStep('extract', 'done');
      setProcStep('post', 'active');
      setTimeout(() => {
        setProcStep('post', 'done');
        recorderState.textContent = `memo #${data.memo_id} saved`;
        recorderState.classList.remove('processing');
        setTimeout(() => {
          recorderTime.textContent = '00:00';
          recorderState.textContent = 'idle';
          procPane.classList.remove('visible');
        }, 3500);
      }, 800);
    }, 800);
  } catch (err) {
    console.error(err);
    recorderState.textContent = 'error';
    setStatus('upload failed', true);
    setTimeout(() => { recorderState.textContent = 'idle'; recorderTime.textContent = '00:00'; }, 2500);
  }
}

function resetProcessingSteps() {
  $$('.proc-step').forEach(s => s.classList.remove('active', 'done'));
}
function setProcStep(step, cls) {
  const el = document.querySelector(`.proc-step[data-step="${step}"]`);
  if (el) {
    el.classList.remove('active', 'done');
    el.classList.add(cls);
  }
}

recordBtn.addEventListener('click', () => {
  if (state.ambientRecorder?.state === 'recording') {
    stopAmbient();
  } else {
    startAmbient();
  }
});

// ── Timer ────────────────────────────────────────────────────────────
function startAmbientTimer() {
  const tick = () => {
    const sec = Math.floor((Date.now() - state.ambientStartTs) / 1000);
    const mm = String(Math.floor(sec / 60)).padStart(2, '0');
    const ss = String(sec % 60).padStart(2, '0');
    recorderTime.textContent = `${mm}:${ss}`;
  };
  tick();
  state.ambientTimerInterval = setInterval(tick, 500);
}
function stopAmbientTimer() {
  if (state.ambientTimerInterval) clearInterval(state.ambientTimerInterval);
  state.ambientTimerInterval = null;
}

// ── Waveform visualization ───────────────────────────────────────────
function startWaveform(stream) {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  state.ambientAudioCtx = new AudioCtx();
  const source = state.ambientAudioCtx.createMediaStreamSource(stream);
  state.ambientAnalyser = state.ambientAudioCtx.createAnalyser();
  state.ambientAnalyser.fftSize = 256;
  source.connect(state.ambientAnalyser);
  drawWaveform();
}
function drawWaveform() {
  const canvas = waveformCanvas;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  canvas.width = canvas.offsetWidth * dpr;
  canvas.height = canvas.offsetHeight * dpr;
  ctx.scale(dpr, dpr);

  const buf = new Uint8Array(state.ambientAnalyser.frequencyBinCount);
  const w = canvas.offsetWidth;
  const h = canvas.offsetHeight;
  const barCount = 48;
  const gap = 2;
  const barW = (w - gap * (barCount - 1)) / barCount;

  const loop = () => {
    if (!state.ambientAnalyser) return;
    state.ambientAnalyser.getByteFrequencyData(buf);
    ctx.clearRect(0, 0, w, h);
    const step = Math.floor(buf.length / barCount);
    for (let i = 0; i < barCount; i++) {
      let v = 0;
      for (let j = 0; j < step; j++) v += buf[i * step + j];
      v = v / step / 255;
      const barH = Math.max(2, v * h * 1.2);
      const x = i * (barW + gap);
      const y = (h - barH) / 2;
      ctx.fillStyle = `rgba(255, 87, 87, ${0.3 + v * 0.7})`;
      ctx.fillRect(x, y, barW, barH);
    }
    state.ambientWaveformFrame = requestAnimationFrame(loop);
  };
  loop();
}
function stopWaveform() {
  if (state.ambientWaveformFrame) cancelAnimationFrame(state.ambientWaveformFrame);
  state.ambientWaveformFrame = null;
  if (state.ambientAudioCtx) {
    state.ambientAudioCtx.close().catch(() => {});
    state.ambientAudioCtx = null;
    state.ambientAnalyser = null;
  }
  const ctx = waveformCanvas.getContext('2d');
  ctx.clearRect(0, 0, waveformCanvas.width, waveformCanvas.height);
}

// ══════════════════════════════════════════════════════════════════════
// MODE 3 — ARCHIVE
// ══════════════════════════════════════════════════════════════════════

async function loadMemos() {
  const list = $('#memoList');
  list.innerHTML = '<div class="memo-empty">Loading…</div>';
  try {
    const r = await fetch(`${API_BASE}/api/memos`);
    const memos = await r.json();
    $('#memoCount').textContent = `${memos.length} memo${memos.length === 1 ? '' : 's'}`;
    if (!memos.length) {
      list.innerHTML = '<div class="memo-empty">No memos yet. Record something.</div>';
      return;
    }
    list.innerHTML = '';
    memos.forEach(m => list.appendChild(renderMemoCard(m)));
  } catch {
    list.innerHTML = '<div class="memo-empty">Couldn\'t load memos.</div>';
  }
}

function renderMemoCard(m) {
  const card = document.createElement('div');
  card.className = 'memo-card';
  const date = new Date(m.recorded_at + 'Z');
  const dateStr = date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const dur = m.duration_seconds ? formatDuration(m.duration_seconds) : '';
  const tags = (m.tags || []).slice(0, 4).map(t => `<span class="memo-tag">${escapeHtml(t)}</span>`).join('');
  card.innerHTML = `
    <div class="memo-card-header">
      <span class="memo-time">${dateStr}</span>
      <span class="memo-duration">${dur}</span>
    </div>
    <div class="memo-summary">${escapeHtml(m.summary || '(processing…)')}</div>
    <div class="memo-tags">${tags}</div>
  `;
  card.addEventListener('click', () => openMemoDetail(m.id));
  return card;
}

async function openMemoDetail(id) {
  const modal = $('#memoModal');
  const body = $('#modalBody');
  body.innerHTML = '<div class="memo-empty">Loading…</div>';
  modal.classList.add('visible');
  try {
    const r = await fetch(`${API_BASE}/api/memos/${id}`);
    const m = await r.json();
    const date = new Date(m.recorded_at + 'Z').toLocaleString();
    let html = `<h3>${date} · ${formatDuration(m.duration_seconds || 0)}</h3>`;
    if (m.summary) html += `<p class="transcript">${escapeHtml(m.summary)}</p>`;
    if (m.key_ideas?.length) {
      html += `<h3>Key Ideas</h3><ul>${m.key_ideas.map(k => `<li>${escapeHtml(k)}</li>`).join('')}</ul>`;
    }
    if (m.actions?.length) {
      html += `<h3>Actions</h3><ul>${m.actions.map(a => {
        const proj = a.project_tag ? ` <em>(${escapeHtml(a.project_tag)})</em>` : '';
        const prio = a.priority ? ` [${escapeHtml(a.priority)}]` : '';
        return `<li>${escapeHtml(a.task)}${prio}${proj}</li>`;
      }).join('')}</ul>`;
    }
    if (m.tags?.length) {
      html += `<h3>Tags</h3><div class="memo-tags">${m.tags.map(t => `<span class="memo-tag">${escapeHtml(t)}</span>`).join('')}</div>`;
    }
    html += `<h3>Transcript</h3><p class="transcript">${escapeHtml(m.raw_transcript)}</p>`;
    body.innerHTML = html;
  } catch {
    body.innerHTML = '<div class="memo-empty">Couldn\'t load.</div>';
  }
}
$('#modalClose').addEventListener('click', () => $('#memoModal').classList.remove('visible'));
$('#memoModal').addEventListener('click', (e) => {
  if (e.target.id === 'memoModal') $('#memoModal').classList.remove('visible');
});
$('#refreshMemos').addEventListener('click', loadMemos);

// ══════════════════════════════════════════════════════════════════════
// UTILITIES
// ══════════════════════════════════════════════════════════════════════
function pickMime() {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'];
  for (const c of candidates) {
    if (MediaRecorder.isTypeSupported(c)) return c;
  }
  return '';
}
function escapeHtml(s) {
  if (!s) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function decodeHeader(b64) {
  if (!b64) return '';
  try { return atob(b64); } catch { return ''; }
}
function formatDuration(sec) {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

// Prevent accidental zoom on double-tap
document.addEventListener('gesturestart', e => e.preventDefault());
