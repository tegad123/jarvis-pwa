/* ════════════════════════════════════════════════════════════
   JARVIS PWA — app.js
   ════════════════════════════════════════════════════════════ */

const API_BASE = '';   // same-origin (served by FastAPI)
const PLAYBACK_SPEED_KEY = 'jarvis_playback_speed';

const state = {
  authenticated: false,
};

const $ = (sel) => document.querySelector(sel);

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
    },
    audio: {
      current: null,
      bubble: null,
      queued: null,
      queuedByChat: {},
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
    if (this.isRecording()) {
      this.stopRecording();
      return;
    }
    this.pauseCurrentAudio({ clearQueue: true });
    try {
      await this.startRecording(e);
    } catch (err) {
      console.error(err);
      this.micIdle();
      this.showMicStatus('mic unavailable');
    }
  },

  async ensureMicStream() {
    if (this.state.mic.stream) return this.state.mic.stream;
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('microphone unsupported');
    }
    this.state.mic.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
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
      if (e.data?.size) this.state.mic.chunks.push(e.data);
    };
    recorder.onstop = () => this.handleMicStop(recorder, Date.now() - this.state.mic.startedAt);
    recorder.start();
    this.state.mic.recorder = recorder;
    this.el.micButton.classList.add('recording');
    this.el.micButton.classList.remove('processing');
    this.el.micButton.setAttribute('aria-label', 'Tap to stop recording');
    this.el.sendButton.disabled = true;
    this.showListening();
  },

  stopRecording() {
    const recorder = this.state.mic.recorder;
    this.state.mic.pressing = false;
    if (recorder?.state === 'recording') {
      recorder.stop();
    }
  },

  async handleMicStop(recorder, durationMs) {
    const chunks = this.state.mic.chunks;
    const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
    this.state.mic.chunks = [];
    this.state.mic.recorder = null;
    this.el.micButton.classList.remove('recording');
    this.clearMicStatus();
    this.playQueuedAudio();

    if (durationMs < 1000 || blob.size < 1000) {
      this.micIdle();
      return;
    }

    this.state.mic.sending = true;
    this.state.mic.pending = (this.state.mic.pending || 0) + 1;
    this.setMicProcessing();
    this.el.sendButton.disabled = true;

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
      const r = await fetch(`/chat/api/chats/${chatId}/voice-message`, {
        method: 'POST',
        body: form,
      });
      if (r.status === 401) { shellAuth.logout(); return; }
      if (!r.ok) throw new Error(`server ${r.status}`);

      const data = await r.json();
      let assistantEl = null;
      if (this.state.currentChatId === chatId) {
        this.appendMessage(data.user_message);
        assistantEl = this.appendMessage(data.assistant_message);
      }
      if (this.state.currentChatId === chatId && data.assistant_message?.audio_url) {
        this.playAssistantAudio(data.assistant_message.audio_url, chatId, assistantEl);
      } else if (data.assistant_message?.audio_url) {
        this.state.audio.queuedByChat[chatId] = {
          url: data.assistant_message.audio_url,
          chatId,
          bubble: null,
        };
      }
      await this.loadChats();
      const updated = this.state.chats.find(c => c.chat_id === chatId);
      if (updated?.title && this.state.currentChatId === chatId) this.el.title.textContent = updated.title;
    } catch (err) {
      console.error(err);
      this.showMicStatus('voice failed');
    } finally {
      this.state.mic.pending = Math.max(0, (this.state.mic.pending || 1) - 1);
      this.micIdle();
    }
  },

  micIdle() {
    this.state.mic.pressing = false;
    this.state.mic.sending = (this.state.mic.pending || 0) > 0;
    if (!this.isRecording()) this.el.micButton.classList.remove('recording');
    this.setMicProcessing();
    this.el.micButton.disabled = false;
    this.el.micButton.setAttribute('aria-label', this.isRecording() ? 'Tap to stop recording' : 'Tap to record');
    this.el.sendButton.disabled = this.state.sending || this.isRecording();
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

  stopMicStream() {
    if (this.state.mic.recorder?.state === 'recording') {
      this.state.mic.recorder.stop();
    }
    this.state.mic.stream?.getTracks().forEach(track => track.stop());
    this.state.mic.stream = null;
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

  playAssistantAudio(url, chatId, bubble) {
    if (!url || chatId !== this.state.currentChatId) return;
    const item = { url, chatId, bubble };
    if (this.isRecording()) {
      this.state.audio.queued = item;
      return;
    }
    this.playAudioItem(item);
  },

  playAudioItem(item) {
    if (!item || item.chatId !== this.state.currentChatId) return;
    if (this.isRecording()) {
      this.state.audio.queued = item;
      return;
    }
    this.pauseCurrentAudio();
    this.clearMicStatus();

    const audio = new Audio(item.url);
    audio.playbackRate = this.state.audio.speed;
    this.state.audio.current = audio;
    this.state.audio.bubble = item.bubble || null;
    item.bubble?.classList.add('chat-bubble-speaking');

    const clear = () => this.clearCurrentAudio(audio);
    audio.addEventListener('ended', clear, { once: true });
    audio.addEventListener('error', clear, { once: true });
    audio.play().catch(() => clear());
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
    this.playAssistantAudio(item.url, chatId, item.bubble);
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
