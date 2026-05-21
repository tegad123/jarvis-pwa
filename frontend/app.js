/* ════════════════════════════════════════════════════════════
   JARVIS PWA — app.js
   ════════════════════════════════════════════════════════════ */

const API_BASE = '';   // same-origin (served by FastAPI)

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
      startedAt: 0,
      statusTimer: null,
    },
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
      hamburger:       $('#chatHamburger'),
      sidebar:         $('#chatSidebar'),
      sidebarBackdrop: $('#chatSidebarBackdrop'),
      logoutButton:    $('#chatLogoutButton'),
    };

    this.el.newButton.addEventListener('click', () => this.createChat());
    this.el.logoutButton.addEventListener('click', () => shellAuth.logout());
    this.el.sendButton.addEventListener('click', () => this.sendMessage());
    this.el.micButton.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      this.startRecording(e).catch((err) => {
        console.error(err);
        this.micIdle();
        this.showMicStatus('mic unavailable');
      });
    });
    const endPress = (e) => {
      e.preventDefault();
      if (this.state.mic.pressing) this.stopRecording();
    };
    this.el.micButton.addEventListener('pointerup', endPress);
    this.el.micButton.addEventListener('pointerleave', endPress);
    this.el.micButton.addEventListener('pointercancel', endPress);
    this.el.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.sendMessage();
      }
    });
    this.el.input.addEventListener('input', () => this.autoResize());
    this.el.hamburger.addEventListener('click', () => this.toggleSidebar());
    this.el.sidebarBackdrop.addEventListener('click', () => this.closeSidebar());

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
    div.textContent = m.content;
    this.el.messages.appendChild(div);
    this.scrollToBottom();
    return div;
  },

  scrollToBottom() {
    this.el.messages.scrollTop = this.el.messages.scrollHeight;
  },

  async sendMessage() {
    if (this.state.sending || this.state.mic.sending) return;
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
      pendingEl.textContent = reply.content;
      this.scrollToBottom();
      await this.loadChats();
      const updated = this.state.chats.find(c => c.chat_id === chatId);
      if (updated?.title) this.el.title.textContent = updated.title;
    } catch {
      pendingEl.classList.remove('chat-bubble-pending');
      pendingEl.textContent = '(error — please try again)';
    } finally {
      this.state.sending = false;
      this.el.sendButton.disabled = false;
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
    if (this.state.sending || this.state.mic.sending || this.state.mic.recorder?.state === 'recording') return;
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
    this.clearMicStatus();
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

    if (durationMs < 1000 || blob.size < 1000) {
      this.micIdle();
      return;
    }

    this.state.mic.sending = true;
    this.el.micButton.classList.add('processing');
    this.el.micButton.disabled = true;
    this.el.sendButton.disabled = true;

    if (!this.state.currentChatId) {
      await this.createChat();
      if (!this.state.currentChatId) {
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
      this.appendMessage(data.user_message);
      this.appendMessage(data.assistant_message);
      if (data.assistant_message?.audio_url) {
        const audio = new Audio(data.assistant_message.audio_url);
        audio.play().catch(() => {});
      }
      await this.loadChats();
      const updated = this.state.chats.find(c => c.chat_id === chatId);
      if (updated?.title) this.el.title.textContent = updated.title;
    } catch (err) {
      console.error(err);
      this.showMicStatus('voice failed');
    } finally {
      this.micIdle();
    }
  },

  micIdle() {
    this.state.mic.sending = false;
    this.state.mic.pressing = false;
    this.el.micButton.classList.remove('recording', 'processing');
    this.el.micButton.disabled = false;
    this.el.sendButton.disabled = this.state.sending;
  },

  stopMicStream() {
    if (this.state.mic.recorder?.state === 'recording') {
      this.state.mic.recorder.stop();
    }
    this.state.mic.stream?.getTracks().forEach(track => track.stop());
    this.state.mic.stream = null;
    this.state.mic.recorder = null;
    this.state.mic.chunks = [];
    this.micIdle();
  },

  showMicStatus(text) {
    clearTimeout(this.state.mic.statusTimer);
    this.el.micStatus.textContent = text;
    this.state.mic.statusTimer = setTimeout(() => this.clearMicStatus(), 1800);
  },

  clearMicStatus() {
    clearTimeout(this.state.mic.statusTimer);
    this.el.micStatus.textContent = '';
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
