/* ════════════════════════════════════════════════════════════
   JARVIS PWA — app.js
   ════════════════════════════════════════════════════════════ */

const API_BASE = '';   // same-origin (served by FastAPI)
const PLAYBACK_SPEED_KEY = 'jarvis_playback_speed';
const SILENT_WAV_DATA_URI = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQAAAAA=';

const state = {
  authenticated: false,
};

const $ = (sel) => document.querySelector(sel);
const cssEscape = (value) => window.CSS?.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, '\\$&');

const shellAuth = {
  async init() {
    document.body.classList.add('auth-pending');
    try {
      const r = await fetch('/chat/api/auth-status');
      const data = await r.json();
      state.authenticated = !!data.authenticated;
    } catch {
      state.authenticated = false;
    }
    this.applyAuthState();

    $('#shellLoginForm').addEventListener('submit', (e) => {
      e.preventDefault();
      this.login();
    });
    $('#topLogoutButton').addEventListener('click', () => this.logout());
  },

  applyAuthState() {
    document.body.classList.remove('auth-pending', 'authenticated', 'unauthenticated');
    document.body.classList.add(state.authenticated ? 'authenticated' : 'unauthenticated');
    if (state.authenticated) {
      chat.activate();
    } else {
      chat.reset();
      setTimeout(() => $('#shellPasswordInput')?.focus(), 50);
    }
  },

  async login() {
    const password = $('#shellPasswordInput').value;
    const errEl = $('#shellLoginError');
    errEl.textContent = '';
    try {
      const r = await fetch('/chat/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (!r.ok) {
        errEl.textContent = 'Wrong password — try again.';
        $('#shellPasswordInput').select();
        return;
      }
      $('#shellPasswordInput').value = '';
      state.authenticated = true;
      this.applyAuthState();
    } catch {
      errEl.textContent = 'Network error.';
    }
  },

  async logout() {
    try { await fetch('/chat/api/logout', { method: 'POST' }); } catch {}
    state.authenticated = false;
    this.applyAuthState();
  },
};

const chat = {
  state: {
    chats: [],
    currentChatId: null,
    sending: false,
    loaded: false,
    mic: {
      stream: null,
      recorder: null,
      chunks: [],
      pressing: false,
      sending: false,
      pending: 0,
      startedAt: 0,
      statusTimer: null,
      emptyCount: 0,
    },
    audio: {
      current: null,
      bubble: null,
      queued: null,
      queuedByChat: {},
      tapToPlay: {},
      unlock: {
        attempted: false,
        audioContext: null,
        silentAudio: null,
        mediaSources: new WeakMap(),
      },
      speed: readPlaybackSpeed(),
    },
    settingsOpen: false,
  },
  el: {},

  init() {
    this.el = {
      app:             $('#chatApp'),
      newButton:       $('#chatNewButton'),
      list:            $('#chatList'),
      title:           $('#chatTitle'),
      messages:        $('#chatMessages'),
      input:           $('#chatInput'),
      sendButton:      $('#chatSendButton'),
      micButton:       $('#chatMicButton'),
      micStatus:       $('#chatMicStatus'),
      settingsButton:  $('#chatSettingsButton'),
      settingsPanel:   $('#chatSettingsPanel'),
      speedSlider:     $('#voiceSpeedSlider'),
      speedValue:      $('#voiceSpeedValue'),
      settingsLogoutButton: $('#chatSettingsLogoutButton'),
      hamburger:       $('#chatHamburger'),
      sidebar:         $('#chatSidebar'),
      sidebarBackdrop: $('#chatSidebarBackdrop'),
      logoutButton:    $('#chatLogoutButton'),
    };

    this.el.newButton.addEventListener('click', () => this.createChat());
    this.el.logoutButton.addEventListener('click', () => shellAuth.logout());
    this.el.settingsLogoutButton.addEventListener('click', () => shellAuth.logout());
    this.el.sendButton.addEventListener('click', () => this.sendMessage());
    this.el.micButton.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      this.handleMicTap(e);
    });
    this.el.settingsButton.addEventListener('click', (e) => {
      e.stopPropagation();
      this.toggleSettings();
    });
    this.el.speedSlider.addEventListener('input', () => this.setPlaybackSpeed(this.el.speedSlider.value));
    document.addEventListener('pointerdown', (e) => {
      if (!this.state.settingsOpen) return;
      if (this.el.settingsPanel.contains(e.target) || this.el.settingsButton.contains(e.target)) return;
      this.closeSettings();
    });
    window.addEventListener('pagehide', () => this.pauseCurrentAudio({ clearQueue: true }));
    window.addEventListener('beforeunload', () => this.pauseCurrentAudio({ clearQueue: true }));
    this.el.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.sendMessage();
      }
    });
    this.el.input.addEventListener('input', () => this.autoResize());
    this.el.hamburger.addEventListener('click', () => this.toggleSidebar());
    this.el.sidebarBackdrop.addEventListener('click', () => this.closeSidebar());
    this.setPlaybackSpeed(this.state.audio.speed, { persist: false });

    this.el.input.addEventListener('focus', () => {
      setTimeout(() => {
        this.el.input.scrollIntoView({ block: 'end', behavior: 'smooth' });
      }, 300);
    });
  },

  async activate() {
    if (!state.authenticated) return;
    if (!this.state.loaded) {
      await this.loadChats();
      this.state.loaded = true;
    } else {
      await this.loadChats();
    }
  },

  reset() {
    this.state.chats = [];
    this.state.currentChatId = null;
    this.state.sending = false;
    this.state.loaded = false;
    this.pauseCurrentAudio({ clearQueue: true });
    this.closeSettings();
    this.stopMicStream();
    if (!this.el.messages) return;
    this.el.list.innerHTML = '';
    this.el.title.textContent = 'No chat selected';
    this.el.messages.innerHTML = '<div class="chat-empty">Pick a chat or start a new one.</div>';
    this.el.input.value = '';
    this.el.sendButton.disabled = false;
    this.closeSidebar();
  },

  async loadChats() {
    try {
      const r = await fetch('/chat/api/chats');
      if (r.status === 401) { shellAuth.logout(); return; }
      if (!r.ok) return;
      this.state.chats = await r.json();
      this.renderChatList();
    } catch {}
  },

  renderChatList() {
    if (!this.state.chats.length) {
      this.el.list.innerHTML = '<div class="chat-list-empty">No chats yet.</div>';
      return;
    }
    this.el.list.innerHTML = '';
    this.state.chats.forEach(c => {
      const div = document.createElement('div');
      div.className = 'chat-list-item';
      if (c.chat_id === this.state.currentChatId) div.classList.add('active');
      const isVoice = c.origin === 'talk' || c.has_audio === true;
      const titleEmpty = !c.title;
      const titleText = c.title || 'New chat';
      const titleClasses = [
        'chat-list-item-title',
        titleEmpty ? 'empty' : '',
        isVoice ? 'is-voice' : '',
      ].filter(Boolean).join(' ');
      const prefix = isVoice ? '🎙️ ' : '';
      const ts = c.last_message_at || c.created_at;
      div.innerHTML = `
        <div class="${titleClasses}">${prefix}${escapeHtml(titleText)}</div>
        <div class="chat-list-item-time">${this.relativeTime(ts)}</div>
      `;
      div.addEventListener('click', () => this.openChat(c.chat_id));
      this.el.list.appendChild(div);
    });
  },

  async createChat() {
    try {
      const r = await fetch('/chat/api/chats', { method: 'POST' });
      if (r.status === 401) { shellAuth.logout(); return; }
      if (!r.ok) return;
      const data = await r.json();
      this.pauseCurrentAudio();
      const newChat = {
        chat_id: data.chat_id,
        title: null,
        created_at: new Date().toISOString(),
        last_message_at: null,
      };
      this.state.chats.unshift(newChat);
      this.state.currentChatId = data.chat_id;
      this.renderChatList();
      this.el.messages.innerHTML = '<div class="chat-empty">Send your first message.</div>';
      this.el.title.textContent = 'New chat';
      this.closeSidebar();
      setTimeout(() => this.el.input.focus(), 80);
    } catch {}
  },

  async openChat(chatId) {
    if (chatId !== this.state.currentChatId) {
      this.pauseCurrentAudio();
    }
    this.state.currentChatId = chatId;
    this.renderChatList();
    const item = this.state.chats.find(c => c.chat_id === chatId);
    this.el.title.textContent = item?.title || 'New chat';
    this.closeSidebar();
    try {
      const r = await fetch(`/chat/api/chats/${chatId}/messages`);
      if (r.status === 401) { shellAuth.logout(); return; }
      if (!r.ok) return;
      const msgs = await r.json();
      this.renderMessages(msgs);
      this.playQueuedChatAudio(chatId);
    } catch {}
  },

  renderMessages(msgs) {
    if (!msgs.length) {
      this.el.messages.innerHTML = '<div class="chat-empty">Send your first message.</div>';
      return;
    }
    this.el.messages.innerHTML = '';
    msgs.forEach(m => this.appendMessage(m));
    this.scrollToBottom();
  },

  appendMessage(m) {
    const empty = this.el.messages.querySelector('.chat-empty');
    if (empty) empty.remove();
    const div = document.createElement('div');
    div.className = `chat-bubble chat-bubble-${m.role}`;
    if (m.pending) div.classList.add('chat-bubble-pending');

    const text = document.createElement('span');
    text.className = 'chat-bubble-text';
    text.textContent = m.content;
    div.appendChild(text);

    if (m.role === 'assistant') {
      if (m.audio_url && this.state.audio.tapToPlay[this.audioItemKey({
        messageId: m.message_id,
        chatId: m.chat_id || this.state.currentChatId,
        url: m.audio_url,
      })]) {
        this.renderTapToPlayButton(div, {
          url: m.audio_url,
          chatId: m.chat_id || this.state.currentChatId,
          messageId: m.message_id,
          bubble: div,
        });
      }
      const speaking = document.createElement('div');
      speaking.className = 'chat-speaking-indicator';
      speaking.textContent = '🔊 speaking...';
      div.appendChild(speaking);
    }

    this.el.messages.appendChild(div);
    this.scrollToBottom();
    return div;
  },

  scrollToBottom() {
    this.el.messages.scrollTop = this.el.messages.scrollHeight;
  },

  async sendMessage() {
    if (this.state.sending || this.isRecording()) return;
    const content = this.el.input.value.trim();
    if (!content) return;
    if (!this.state.currentChatId) {
      await this.createChat();
      if (!this.state.currentChatId) return;
    }

    const chatId = this.state.currentChatId;
    this.state.sending = true;
    this.el.sendButton.disabled = true;
    this.el.input.value = '';
    this.autoResize();

    this.appendMessage({ role: 'user', content });
    const pendingEl = this.appendMessage({
      role: 'assistant',
      content: 'thinking…',
      pending: true,
    });

    try {
      const r = await fetch(`/chat/api/chats/${chatId}/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      if (r.status === 401) { shellAuth.logout(); return; }
      if (!r.ok) throw new Error(`server ${r.status}`);
      const reply = await r.json();
      pendingEl.classList.remove('chat-bubble-pending');
      pendingEl.querySelector('.chat-bubble-text').textContent = reply.content;
      this.scrollToBottom();
      await this.loadChats();
      const updated = this.state.chats.find(c => c.chat_id === chatId);
      if (updated?.title) this.el.title.textContent = updated.title;
    } catch {
      pendingEl.classList.remove('chat-bubble-pending');
      pendingEl.querySelector('.chat-bubble-text').textContent = '(error — please try again)';
    } finally {
      this.state.sending = false;
      this.el.sendButton.disabled = false;
    }
  },

  async handleMicTap(e) {
    this.logMicState('tap', {
      recorderState: this.state.mic.recorder?.state || 'none',
      pending: this.state.mic.pending,
      hasStream: !!this.state.mic.stream,
      tracks: this.describeMicTracks(this.state.mic.stream),
    });
    if (this.isRecording()) {
      this.stopRecording();
      return;
    }
    this.unlockAudioPlayback();
    this.cancelVisibleTapToPlay();
    this.pauseCurrentAudio({ clearQueue: true });
    try {
      await this.startRecording(e);
    } catch (err) {
      console.error(err);
      this.releaseMicStream('start-failed');
      this.micIdle();
      this.showMicStatus('mic unavailable');
    }
  },

  async ensureMicStream() {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('microphone unsupported');
    }
    this.releaseMicStream('before-new-stream');
    this.logMicState('requesting-stream');
    this.state.mic.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.logMicState('stream-ready', {
      tracks: this.describeMicTracks(this.state.mic.stream),
    });
    return this.state.mic.stream;
  },

  async startRecording(e) {
    if (this.state.sending || this.isRecording()) return;
    if (!window.MediaRecorder) throw new Error('MediaRecorder unsupported');
    const stream = await this.ensureMicStream();
    const mimeType = pickMime();
    const options = mimeType ? { mimeType } : undefined;
    const recorder = new MediaRecorder(stream, options);

    this.state.mic.chunks = [];
    this.state.mic.startedAt = Date.now();
    this.state.mic.pressing = true;
    this.el.micButton.setPointerCapture?.(e.pointerId);

    recorder.ondataavailable = (e) => {
      this.logMicState('dataavailable', {
        size: e.data?.size || 0,
        type: e.data?.type || '',
        recorderState: recorder.state,
        chunks: this.state.mic.chunks.length,
        tracks: this.describeMicTracks(stream),
      });
      if (e.data?.size) this.state.mic.chunks.push(e.data);
    };
    recorder.onerror = (event) => {
      this.logMicState('recorder-error', {
        error: event.error?.message || event.error?.name || 'unknown',
        recorderState: recorder.state,
        tracks: this.describeMicTracks(stream),
      });
    };
    recorder.onstart = () => {
      this.logMicState('recording-started', {
        mimeType: recorder.mimeType,
        recorderState: recorder.state,
        tracks: this.describeMicTracks(stream),
      });
    };
    recorder.onstop = () => {
      const durationMs = Date.now() - this.state.mic.startedAt;
      this.logMicState('recorder-stop-event', {
        durationMs,
        mimeType: recorder.mimeType,
        recorderState: recorder.state,
        chunks: this.state.mic.chunks.length,
        tracks: this.describeMicTracks(stream),
      });
      this.handleMicStop(recorder, durationMs);
    };
    this.state.mic.recorder = recorder;
    recorder.start();
    this.el.micButton.classList.add('recording');
    this.el.micButton.classList.remove('processing');
    this.el.micButton.setAttribute('aria-label', 'Tap to stop recording');
    this.el.sendButton.disabled = true;
    this.showListening();
    this.logMicTransition('idle', 'recording', {
      mimeType: recorder.mimeType,
      tracks: this.describeMicTracks(stream),
    });
  },

  stopRecording() {
    const recorder = this.state.mic.recorder;
    this.state.mic.pressing = false;
    this.logMicState('stop-requested', {
      recorderState: recorder?.state || 'none',
      tracks: this.describeMicTracks(this.state.mic.stream),
    });
    if (recorder?.state === 'recording') {
      recorder.stop();
      return;
    }
    if (recorder) {
      this.logMicState('stop-ignored', { reason: `recorder-${recorder.state}` });
    }
  },

  async handleMicStop(recorder, durationMs) {
    const chunks = this.state.mic.chunks;
    const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
    const tracks = this.describeMicTracks(this.state.mic.stream);
    this.logMicState('recording-stopped', {
      durationMs,
      blobSize: blob.size,
      mimeType: blob.type,
      chunks: chunks.length,
      tracks,
    });
    this.releaseMicStream('recording-stop');
    this.state.mic.chunks = [];
    this.state.mic.recorder = null;
    this.el.micButton.classList.remove('recording');
    this.clearMicStatus();
    this.playQueuedAudio();

    if (durationMs < 1000 || blob.size < 1000) {
      const reason = durationMs < 1000 ? 'too-short' : 'empty-blob';
      this.state.mic.emptyCount += 1;
      this.logMicTransition('recording', 'idle', {
        discard: true,
        reason,
        durationMs,
        blobSize: blob.size,
        emptyCount: this.state.mic.emptyCount,
      });
      const msg = this.state.mic.emptyCount > 1
        ? 'Recording was empty again — try again'
        : 'Recording was empty — try again';
      this.micIdle();
      this.showMicStatus(msg);
      return;
    }
    this.state.mic.emptyCount = 0;

    this.state.mic.sending = true;
    this.state.mic.pending = (this.state.mic.pending || 0) + 1;
    this.setMicProcessing();
    this.el.sendButton.disabled = true;
    this.logMicTransition('recording', 'processing', {
      durationMs,
      blobSize: blob.size,
      mimeType: blob.type,
    });

    if (!this.state.currentChatId) {
      await this.createChat();
      if (!this.state.currentChatId) {
        this.state.mic.pending = Math.max(0, (this.state.mic.pending || 1) - 1);
        this.micIdle();
        return;
      }
    }

    const chatId = this.state.currentChatId;
    try {
      const form = new FormData();
      form.append('audio', blob, 'voice.webm');
      this.logMicState('upload-start', {
        chatId,
        durationMs,
        blobSize: blob.size,
        mimeType: blob.type,
      });
      const r = await fetch(`/chat/api/chats/${chatId}/voice-message`, {
        method: 'POST',
        body: form,
      });
      if (r.status === 401) { shellAuth.logout(); return; }
      if (!r.ok) throw new Error(`server ${r.status}`);

      const data = await r.json();
      console.log('[voice-debug] voice-message response:', data);
      this.logMicState('upload-success', {
        chatId,
        hasAssistantAudio: !!data.assistant_message?.audio_url,
      });
      let assistantEl = null;
      if (this.state.currentChatId === chatId) {
        console.log('[voice-debug] appending voice messages:', {
          chatId,
          userAudioUrl: data.user_message?.audio_url,
          assistantAudioUrl: data.assistant_message?.audio_url,
        });
        this.appendMessage(data.user_message);
        assistantEl = this.appendMessage(data.assistant_message);
      }
      if (this.state.currentChatId === chatId && data.assistant_message?.audio_url) {
        console.log('[voice-debug] starting assistant audio playback:', {
          chatId,
          audioUrl: data.assistant_message.audio_url,
          hasBubble: !!assistantEl,
        });
        this.playAssistantAudio(
          data.assistant_message.audio_url,
          chatId,
          assistantEl,
          data.assistant_message.message_id
        );
      } else if (data.assistant_message?.audio_url) {
        console.log('[voice-debug] queueing assistant audio for inactive chat:', {
          chatId,
          currentChatId: this.state.currentChatId,
          audioUrl: data.assistant_message.audio_url,
        });
        this.state.audio.queuedByChat[chatId] = {
          url: data.assistant_message.audio_url,
          chatId,
          messageId: data.assistant_message.message_id,
          bubble: null,
        };
      }
      await this.loadChats();
      const updated = this.state.chats.find(c => c.chat_id === chatId);
      if (updated?.title && this.state.currentChatId === chatId) this.el.title.textContent = updated.title;
    } catch (err) {
      console.error(err);
      this.logMicState('upload-failed', {
        chatId,
        error: err.message || String(err),
      });
      this.showMicStatus('voice failed');
    } finally {
      this.state.mic.pending = Math.max(0, (this.state.mic.pending || 1) - 1);
      this.micIdle();
    }
  },

  micIdle() {
    const wasProcessing = this.state.mic.sending;
    this.state.mic.pressing = false;
    this.state.mic.sending = (this.state.mic.pending || 0) > 0;
    if (!this.isRecording()) this.el.micButton.classList.remove('recording');
    this.setMicProcessing();
    this.el.micButton.disabled = false;
    this.el.micButton.setAttribute('aria-label', this.isRecording() ? 'Tap to stop recording' : 'Tap to record');
    this.el.sendButton.disabled = this.state.sending || this.isRecording();
    if (wasProcessing && !this.state.mic.sending && !this.isRecording()) {
      this.logMicTransition('processing', 'idle', {
        pending: this.state.mic.pending,
      });
    }
  },

  isRecording() {
    return this.state.mic.recorder?.state === 'recording';
  },

  setMicProcessing() {
    if ((this.state.mic.pending || 0) > 0 && !this.isRecording()) {
      this.el.micButton.classList.add('processing');
      return;
    }
    this.el.micButton.classList.remove('processing');
  },

  releaseMicStream(reason) {
    const stream = this.state.mic.stream;
    if (!stream) return;
    const before = this.describeMicTracks(stream);
    stream.getTracks().forEach(track => {
      try { track.stop(); } catch {}
    });
    this.state.mic.stream = null;
    this.logMicState('stream-released', { reason, tracks: before });
  },

  describeMicTracks(stream) {
    if (!stream) return [];
    return stream.getTracks().map(track => ({
      kind: track.kind,
      enabled: track.enabled,
      muted: track.muted,
      readyState: track.readyState,
      label: track.label || '',
    }));
  },

  logMicState(event, detail = {}) {
    console.log('[mic-state]', event, {
      ...detail,
      recorderState: detail.recorderState || this.state.mic.recorder?.state || 'none',
      pending: this.state.mic.pending,
      sending: this.state.mic.sending,
      isRecording: this.isRecording(),
    });
  },

  logMicTransition(from, to, detail = {}) {
    this.logMicState(`${from} -> ${to}`, detail);
  },

  stopMicStream() {
    if (this.state.mic.recorder?.state === 'recording') {
      this.state.mic.recorder.stop();
    }
    this.releaseMicStream('stop-mic-stream');
    this.state.mic.recorder = null;
    this.state.mic.chunks = [];
    this.state.mic.pending = 0;
    this.micIdle();
  },

  showMicStatus(text, autoClear = true) {
    clearTimeout(this.state.mic.statusTimer);
    this.el.micStatus.textContent = text;
    this.el.micStatus.classList.remove('listening');
    if (autoClear) {
      this.state.mic.statusTimer = setTimeout(() => this.clearMicStatus(), 1800);
    }
  },

  showListening() {
    clearTimeout(this.state.mic.statusTimer);
    this.el.micStatus.textContent = '🎙️ listening...';
    this.el.micStatus.classList.add('listening');
  },

  clearMicStatus() {
    clearTimeout(this.state.mic.statusTimer);
    this.el.micStatus.textContent = '';
    this.el.micStatus.classList.remove('listening');
  },

  unlockAudioPlayback() {
    const unlock = this.state.audio.unlock;
    unlock.attempted = true;
    console.log('[voice-debug] audio unlock attempted');

    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass && !unlock.audioContext) {
      try {
        const ctx = new AudioContextClass();
        unlock.audioContext = ctx;
        const source = ctx.createBufferSource();
        source.buffer = ctx.createBuffer(1, 1, 22050);
        source.connect(ctx.destination);
        source.start(0);
        ctx.resume?.()
          .then(() => console.log('[voice-debug] audio context unlocked', { state: ctx.state }))
          .catch((err) => console.log(`[voice-debug] audio context unlock rejected: ${err?.name || 'Error'}: ${err?.message || ''}`));
      } catch (err) {
        console.log(`[voice-debug] audio context unlock failed: ${err?.name || 'Error'}: ${err?.message || ''}`);
      }
    } else if (unlock.audioContext?.state === 'suspended') {
      unlock.audioContext.resume?.()
        .then(() => console.log('[voice-debug] audio context resumed', { state: unlock.audioContext.state }))
        .catch((err) => console.log(`[voice-debug] audio context resume rejected: ${err?.name || 'Error'}: ${err?.message || ''}`));
    }

    if (!unlock.silentAudio) {
      try {
        const silentAudio = new Audio(SILENT_WAV_DATA_URI);
        silentAudio.preload = 'auto';
        silentAudio.setAttribute('playsinline', '');
        unlock.silentAudio = silentAudio;
      } catch (err) {
        console.log(`[voice-debug] silent audio create failed: ${err?.name || 'Error'}: ${err?.message || ''}`);
      }
    }

    if (unlock.silentAudio) {
      try {
        unlock.silentAudio.currentTime = 0;
      } catch {}
      unlock.silentAudio.play()
        .then(() => console.log('[voice-debug] silent audio unlocked'))
        .catch((err) => console.log(`[voice-debug] silent audio unlock rejected: ${err?.name || 'Error'}: ${err?.message || ''}`));
    }
  },

  prepareUnlockedAudioElement(audio) {
    audio.setAttribute('playsinline', '');
    audio.preload = 'auto';

    const unlock = this.state.audio.unlock;
    const ctx = unlock.audioContext;
    if (!ctx) return;
    if (ctx.state === 'suspended') {
      ctx.resume?.().catch((err) => {
        console.log(`[voice-debug] audio context resume before playback rejected: ${err?.name || 'Error'}: ${err?.message || ''}`);
      });
    }
    if (!unlock.mediaSources.has(audio)) {
      try {
        const source = ctx.createMediaElementSource(audio);
        source.connect(ctx.destination);
        unlock.mediaSources.set(audio, source);
        console.log('[voice-debug] response audio connected to unlocked context');
      } catch (err) {
        console.log(`[voice-debug] response audio context connect failed: ${err?.name || 'Error'}: ${err?.message || ''}`);
      }
    }
  },

  audioItemKey(item) {
    return item?.messageId || `${item?.chatId || 'unknown'}:${item?.url || 'unknown'}`;
  },

  playAssistantAudio(url, chatId, bubble, messageId = null) {
    console.log('[voice-debug] playAssistantAudio called:', {
      url,
      chatId,
      currentChatId: this.state.currentChatId,
      isRecording: this.isRecording(),
      hasBubble: !!bubble,
      messageId,
    });
    if (!url || chatId !== this.state.currentChatId) return;
    const item = { url, chatId, bubble, messageId };
    if (this.isRecording()) {
      console.log('[voice-debug] playback queued because recorder is active:', item);
      this.state.audio.queued = item;
      return;
    }
    this.playAudioItem(item, { source: 'autoplay' });
  },

  playAudioItem(item, { source = 'autoplay' } = {}) {
    console.log('[voice-debug] playAudioItem called:', {
      item,
      currentChatId: this.state.currentChatId,
      isRecording: this.isRecording(),
      playbackSpeed: this.state.audio.speed,
      source,
    });
    if (!item || item.chatId !== this.state.currentChatId) return;
    if (this.isRecording()) {
      console.log('[voice-debug] playAudioItem re-queued because recorder is active:', item);
      this.state.audio.queued = item;
      return;
    }
    this.pauseCurrentAudio();
    this.clearMicStatus();
    if (source !== 'user tap') this.removeTapToPlayButton(item);

    const audio = new Audio(item.url);
    this.prepareUnlockedAudioElement(audio);
    audio.playbackRate = this.state.audio.speed;
    this.state.audio.current = audio;
    this.state.audio.bubble = item.bubble || null;
    item.bubble?.classList.add('chat-bubble-speaking');

    const clear = () => this.clearCurrentAudio(audio);
    audio.addEventListener('ended', () => {
      console.log('[voice-debug] audio ended', { url: item.url, source });
      clear();
    }, { once: true });
    audio.addEventListener('error', () => {
      console.log('[voice-debug] audio element error:', {
        url: item.url,
        error: audio.error,
        networkState: audio.networkState,
        readyState: audio.readyState,
      });
      clear();
    }, { once: true });
    console.log(`[voice-debug] play() attempted (${source})`, {
      url: item.url,
      networkState: audio.networkState,
      readyState: audio.readyState,
    });
    audio.play()
      .then(() => {
        console.log('[voice-debug] play() resolved', {
          url: item.url,
          source,
          duration: audio.duration,
          readyState: audio.readyState,
        });
        this.removePendingTapToPlay(item);
      })
      .catch((err) => {
        console.log(`[voice-debug] play() rejected: ${err?.name || 'Error'}: ${err?.message || ''}`, {
          url: item.url,
          source,
          name: err?.name,
          message: err?.message,
          networkState: audio.networkState,
          readyState: audio.readyState,
        });
        this.clearCurrentAudio(audio);
        this.renderTapToPlayButton(item.bubble, item);
      });
  },

  playQueuedAudio() {
    if (this.isRecording() || !this.state.audio.queued) return;
    const item = this.state.audio.queued;
    this.state.audio.queued = null;
    this.playAudioItem(item);
  },

  playQueuedChatAudio(chatId) {
    const item = this.state.audio.queuedByChat[chatId];
    if (!item || chatId !== this.state.currentChatId) return;
    delete this.state.audio.queuedByChat[chatId];
    const bubbles = this.el.messages.querySelectorAll('.chat-bubble-assistant');
    item.bubble = bubbles[bubbles.length - 1] || null;
    this.playAssistantAudio(item.url, chatId, item.bubble, item.messageId);
  },

  renderTapToPlayButton(bubble, item) {
    if (!bubble || !item?.url) return;
    const key = this.audioItemKey(item);
    const persisted = {
      url: item.url,
      chatId: item.chatId,
      messageId: item.messageId,
    };
    this.state.audio.tapToPlay[key] = persisted;
    bubble.dataset.audioKey = key;
    bubble.dataset.audioUrl = item.url;
    bubble.dataset.chatId = item.chatId || '';
    if (item.messageId) bubble.dataset.messageId = item.messageId;
    if (bubble.querySelector('.chat-tap-play-button')) return;

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chat-tap-play-button';
    button.title = 'Tap to play response';
    button.setAttribute('aria-label', 'Tap to play response');
    button.innerHTML = `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M8 5v14l11-7z"></path>
      </svg>
    `;
    button.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      this.playAudioItem({ ...persisted, bubble }, { source: 'user tap' });
    });
    bubble.querySelector('.chat-bubble-text')?.after(button);
    console.log('[voice-debug] tap-to-play button rendered', persisted);
  },

  removeTapToPlayButton(item) {
    const key = this.audioItemKey(item);
    const safeKey = cssEscape(key);
    const bubble = item?.bubble || this.el.messages?.querySelector(`[data-audio-key="${safeKey}"]`);
    bubble?.querySelector('.chat-tap-play-button')?.remove();
  },

  removePendingTapToPlay(item) {
    const key = this.audioItemKey(item);
    delete this.state.audio.tapToPlay[key];
    this.removeTapToPlayButton(item);
  },

  cancelVisibleTapToPlay() {
    this.el.messages?.querySelectorAll('.chat-tap-play-button').forEach((button) => {
      const bubble = button.closest('.chat-bubble');
      const key = bubble?.dataset.audioKey;
      if (key) delete this.state.audio.tapToPlay[key];
      button.remove();
    });
  },

  pauseCurrentAudio({ clearQueue = false } = {}) {
    const audio = this.state.audio.current;
    if (audio) {
      try { audio.pause(); } catch {}
      try {
        audio.currentTime = Number.isFinite(audio.duration) ? audio.duration : 0;
      } catch {}
    }
    this.state.audio.bubble?.classList.remove('chat-bubble-speaking');
    this.state.audio.current = null;
    this.state.audio.bubble = null;
    if (clearQueue) {
      this.state.audio.queued = null;
      this.state.audio.queuedByChat = {};
    }
  },

  clearCurrentAudio(audio) {
    if (this.state.audio.current !== audio) return;
    this.state.audio.bubble?.classList.remove('chat-bubble-speaking');
    this.state.audio.current = null;
    this.state.audio.bubble = null;
  },

  toggleSettings() {
    this.state.settingsOpen ? this.closeSettings() : this.openSettings();
  },

  openSettings() {
    this.state.settingsOpen = true;
    this.el.settingsPanel.hidden = false;
    this.el.settingsButton.setAttribute('aria-expanded', 'true');
  },

  closeSettings() {
    this.state.settingsOpen = false;
    if (this.el.settingsPanel) this.el.settingsPanel.hidden = true;
    this.el.settingsButton?.setAttribute('aria-expanded', 'false');
  },

  setPlaybackSpeed(value, { persist = true } = {}) {
    const speed = clampPlaybackSpeed(value);
    this.state.audio.speed = speed;
    if (this.el.speedSlider) this.el.speedSlider.value = String(speed);
    if (this.el.speedValue) this.el.speedValue.textContent = formatPlaybackSpeed(speed);
    if (persist) {
      try { localStorage.setItem(PLAYBACK_SPEED_KEY, String(speed)); } catch {}
    }
    if (this.state.audio.current) this.state.audio.current.playbackRate = speed;
  },

  autoResize() {
    const el = this.el.input;
    el.style.height = 'auto';
    el.style.height = Math.min(140, el.scrollHeight) + 'px';
  },

  toggleSidebar() { this.el.app.classList.toggle('sidebar-open'); },
  closeSidebar()  { this.el.app.classList.remove('sidebar-open'); },

  relativeTime(iso) {
    if (!iso) return '';
    const ts = iso.endsWith('Z') ? iso : iso + 'Z';
    const t = new Date(ts);
    const diff = (Date.now() - t.getTime()) / 1000;
    if (diff < 60)         return 'just now';
    if (diff < 3600)       return `${Math.floor(diff / 60)} min ago`;
    if (diff < 86400)      return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 86400 * 2)  return 'yesterday';
    if (diff < 86400 * 7)  return `${Math.floor(diff / 86400)}d ago`;
    return t.toLocaleDateString([], { month: 'short', day: 'numeric' });
  },
};

function escapeHtml(s) {
  if (!s) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function clampPlaybackSpeed(value) {
  const n = parseFloat(value);
  if (!Number.isFinite(n)) return 1;
  return Math.min(2, Math.max(0.75, Math.round(n * 20) / 20));
}

function readPlaybackSpeed() {
  try {
    return clampPlaybackSpeed(localStorage.getItem(PLAYBACK_SPEED_KEY) || '1');
  } catch {
    return 1;
  }
}

function formatPlaybackSpeed(speed) {
  return `${Number(speed).toFixed(2).replace(/0$/, '').replace(/\.0$/, '.0')}x`;
}

function pickMime() {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'];
  for (const c of candidates) {
    if (MediaRecorder.isTypeSupported(c)) return c;
  }
  return '';
}

document.addEventListener('gesturestart', e => e.preventDefault());

chat.init();
shellAuth.init();
