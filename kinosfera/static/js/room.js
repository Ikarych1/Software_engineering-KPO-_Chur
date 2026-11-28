/* Watch-party client: SSE events, clock sync, drift correction, chat, reactions, presence. */
(() => {
    'use strict';
    const page = document.querySelector('.room-page');
    if (!page || !window.KinoStream) return;
    const code = page.dataset.roomCode;
    const meId = Number(page.dataset.userId);
    const video = document.getElementById('room-video');
    const clipButton = document.querySelector('[data-open-clip]');
    function syncClipButton() {
        clipButton.disabled = !Number.isFinite(video.duration) || video.duration < 1;
    }
    video.addEventListener('durationchange', syncClipButton);
    video.addEventListener('emptied', syncClipButton);
    const screen = document.getElementById('cinema-screen');
    const joinBox = document.getElementById('room-join');
    const connectButton = document.getElementById('room-connect');
    const prep = document.getElementById('room-prep');
    const phase = document.getElementById('room-phase');
    const syncState = document.getElementById('sync-state');
    const controller = document.getElementById('room-controller');
    const notice = document.getElementById('screen-notice');
    const chat = document.getElementById('chat-messages');
    const chatForm = document.getElementById('chat-form');
    const chatInput = document.getElementById('chat-input');
    const memberList = document.getElementById('member-list');
    const statsBox = document.getElementById('stream-stats');
    const reactionBar = document.getElementById('reaction-bar');
    const stage = document.getElementById('reaction-stage');
    const hostPanel = document.getElementById('host-panel');
    const hostToggle = page.querySelector('[data-host-tools]');
    const fmt = window.KinoFormat;
    const csrf = document.querySelector('meta[name=csrf-token]').content;
    const toast = text => window.KinoDemo?.toast(text);

    let room = null;            // snapshot data
    let state = null;           // authoritative playback state
    let offset = 0;             // server clock - local clock, seconds
    let attached = false;
    let lastSeq = -1;
    let ended = false;
    let source = null;
    const seenMessages = new Set();

    const serverNow = () => Date.now() / 1000 + offset;
    const expected = () => !state ? 0 : state.playing
        ? state.position + Math.max(0, serverNow() - state.updated_at) * (state.rate || 1) : state.position;
    const isHost = () => room && room.host_id === meId;
    const canControl = () => room && (room.settings.control === 'all' || isHost());

    async function post(path, body) {
        const response = await fetch(`/api/rooms/${code}/${path}`, {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(body || {})});
        let data = {};
        try { data = await response.json(); } catch (_) { /* ignore */ }
        if (!response.ok) throw new Error(data.error || 'Не удалось выполнить действие.');
        return data;
    }

    async function syncClock() {
        const samples = [];
        for (let i = 0; i < 5; i += 1) {
            const t0 = Date.now() / 1000;
            try {
                const response = await fetch('/api/time', {cache: 'no-store'});
                const {t} = await response.json();
                const t1 = Date.now() / 1000;
                samples.push({rtt: t1 - t0, offset: t - (t0 + t1) / 2});
            } catch (_) { /* skip sample */ }
        }
        if (samples.length) {
            samples.sort((a, b) => a.rtt - b.rtt);
            offset = samples[0].offset;
        }
    }

    // ------------------------------------------------------------------ rendering
    function colorFor(id) { return ['peach', 'blue', 'purple', 'green', 'gold'][Number(id) % 5]; }
    function initials(name) { return String(name || '?').split(/\s+/).slice(0, 2).map(p => p[0] || '').join('').toUpperCase(); }
    function timeLabel(epoch) {
        return new Date(epoch * 1000).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
    }

    function addMessage(message, {ephemeral = false} = {}) {
        if (message.id && seenMessages.has(message.id)) return;
        if (message.id) seenMessages.add(message.id);
        const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
        const row = document.createElement('div');
        if (message.kind === 'system' || ephemeral) {
            row.className = `chat-system${ephemeral ? ' is-ephemeral' : ''}`;
            row.textContent = message.text;
        } else {
            row.className = `chat-message${message.user_id === meId ? ' is-mine' : ''}`;
            const avatar = document.createElement('span');
            avatar.className = `avatar ${colorFor(message.user_id)} tiny`;
            avatar.textContent = initials(message.name);
            const body = document.createElement('div');
            const head = document.createElement('strong');
            head.append(document.createTextNode(`${message.name} `));
            const time = document.createElement('time');
            time.textContent = timeLabel(message.created_at);
            head.append(time);
            const text = document.createElement('p');
            text.textContent = message.text;
            body.append(head, text);
            row.append(avatar, body);
        }
        chat.append(row);
        while (chat.children.length > 400) chat.firstElementChild.remove();
        if (atBottom || message.user_id === meId) chat.scrollTop = chat.scrollHeight;
    }

    const STATUS = {playing: 'смотрит', paused: 'на паузе', buffering: 'догружает видео…', ready: 'готов', idle: 'в комнате', error: 'ошибка плеера', offline: 'не в сети'};
    function renderMembers(members) {
        room.members = members;
        memberList.replaceChildren();
        const online = members.filter(m => m.online).length;
        document.getElementById('member-count').textContent = String(online);
        const transfer = document.getElementById('host-transfer');
        if (transfer) {
            transfer.replaceChildren(new Option('— выберите участника —', ''));
            members.filter(m => !m.host).forEach(m => transfer.append(new Option(m.name, String(m.id))));
        }
        members.forEach(member => {
            const item = document.createElement('li');
            item.className = `member status-${member.status}${member.online ? ' is-online' : ''}`;
            const avatar = document.createElement('span');
            avatar.className = `avatar ${member.color} small`;
            avatar.textContent = initials(member.name);
            const text = document.createElement('div');
            const name = document.createElement('strong');
            name.textContent = member.name + (member.id === meId ? ' (вы)' : '');
            const status = document.createElement('span');
            status.className = 'member-status';
            let label = STATUS[member.status] || member.status;
            if (member.online && ['playing', 'paused'].includes(member.status) && state && attached) {
                const drift = member.position - expected();
                if (Math.abs(drift) > 3) label += drift < 0 ? ` · отстаёт на ${Math.round(-drift)} с` : ` · впереди на ${Math.round(drift)} с`;
            }
            status.textContent = label;
            text.append(name, status);
            item.append(avatar, text);
            if (member.host) {
                const crown = document.createElement('span');
                crown.className = 'member-host';
                crown.title = 'Хост комнаты';
                crown.textContent = 'хост';
                item.append(crown);
            }
            memberList.append(item);
        });
        const host = members.find(m => m.host);
        document.getElementById('room-host').textContent = host ? host.name : '—';
    }

    function renderSettings() {
        const s = room.settings;
        controller.textContent = s.control === 'all' ? 'Управлять могут все участники' : `Плеером управляет ${isHost() ? 'вы (хост)' : 'хост'}`;
        hostToggle.hidden = !isHost();
        if (!isHost()) hostPanel.hidden = true;
        const set = (id, value) => { const el = document.getElementById(id); if (el) el.checked = Boolean(value); };
        set('host-wait', s.wait_for_all); set('host-chat', s.chat); set('host-reactions', s.reactions); set('host-locked', s.locked);
        const control = document.getElementById('host-control');
        if (control) control.value = s.control;
        chatInput.disabled = !s.chat && !isHost();
        chatInput.placeholder = chatInput.disabled ? 'Хост отключил сообщения' : 'Поделиться впечатлением…';
        reactionBar.querySelectorAll('button').forEach(b => { b.disabled = !s.reactions && !isHost(); });
        page.querySelector('[data-end-room] span').textContent = isHost() ? 'Завершить' : 'Выйти';
    }

    function showNotice(text) {
        notice.textContent = text;
        notice.classList.add('is-visible');
        clearTimeout(showNotice.timer);
        showNotice.timer = setTimeout(() => notice.classList.remove('is-visible'), 3200);
    }

    function floatReaction(emoji, name) {
        const item = document.createElement('span');
        item.className = 'floating-reaction';
        item.textContent = emoji;
        const label = document.createElement('small');
        label.textContent = name;
        item.append(label);
        item.style.left = `${10 + Math.random() * 75}%`;
        stage.append(item);
        item.animate?.([
            {transform: 'translateY(0) scale(.6)', opacity: 0},
            {transform: 'translateY(-40px) scale(1.15)', opacity: 1, offset: .15},
            {transform: 'translateY(-260px) scale(1)', opacity: 0}], {duration: 2600, easing: 'ease-out'});
        setTimeout(() => item.remove(), 2600);
    }

    function setSync(kind, text) {
        syncState.className = `sync-state is-${kind}`;
        syncState.lastChild.textContent = ` ${text}`;
    }

    // ------------------------------------------------------------------ playback sync
    // Programmatic changes are announced first, so their media events are not
    // mistaken for user actions (which would be sent back to the room).
    let expectPlay = 0, expectPause = 0, expectSeek = null, expectSeekAt = 0;
    function doPlay() {
        if (!video.paused) return;
        expectPlay += 1;
        video.play().catch(() => { expectPlay = Math.max(0, expectPlay - 1); setSync('warn', 'Нажмите ▶, чтобы присоединиться'); });
    }
    function doPause() {
        if (video.paused) return;
        expectPause += 1;
        video.pause();
    }
    function doSeek(target) {
        if (!Number.isFinite(target) || !video.duration) return;
        expectSeek = Math.max(0, Math.min(target, video.duration - 0.5));
        expectSeekAt = Date.now();
        video.currentTime = expectSeek;
    }

    function applyState(force = false) {
        if (!attached || !state || !video.duration) return;
        const target = expected();
        if (force || Math.abs(video.currentTime - target) > 1.2) doSeek(target);
        if (state.playing) doPlay(); else doPause();
        video.playbackRate = state.rate || 1;
    }

    function correctDrift() {
        if (expectSeek !== null && Date.now() - expectSeekAt > 8000 && !video.seeking) expectSeek = null;
        if (!attached || !state || video.seeking || video.readyState < 2 || expectSeek !== null) return;
        const target = expected();
        const drift = video.currentTime - target;
        const base = state.rate || 1;
        if (!state.playing) {
            if (Math.abs(drift) > 0.6) doSeek(target);
            doPause();
            setSync('ok', `Пауза · ${fmt.time(target)}`);
            return;
        }
        if (video.paused) {
            if (!stream.buffering) setSync('warn', 'Нажмите ▶, чтобы присоединиться к просмотру');
            return;
        }
        if (Math.abs(drift) > 1.5) {
            doSeek(target + 0.3);
            setSync('warn', 'Догоняем остальных…');
        } else if (Math.abs(drift) > 0.2) {
            video.playbackRate = base * (1 - Math.max(-0.08, Math.min(0.08, drift * 0.12)));
            setSync('warn', drift > 0 ? 'Чуть притормаживаем для синхронизации' : 'Чуть ускоряемся для синхронизации');
        } else {
            video.playbackRate = base;
            setSync('ok', 'Синхронно со всеми');
        }
    }

    function myStatus() {
        if (!attached) return 'idle';
        if (stream.buffering) return 'buffering';
        return video.paused ? 'paused' : 'playing';
    }
    let lastPresence = 0;
    function sendPresence(force = false) {
        if (ended) return;
        const now = Date.now();
        if (!force && now - lastPresence < 1800) return;
        lastPresence = now;
        post('presence', {status: myStatus(), position: video.currentTime || 0}).catch(() => {});
    }

    async function requestControl(action) {
        if (!attached || ended) return;
        if (!canControl()) {
            toast('Сейчас плеером управляет хост комнаты.');
            applyState(true);
            return;
        }
        try {
            const data = await post('control', {action, position: video.currentTime || 0});
            state = data.state;
        } catch (error) {
            toast(error.message);
            applyState(true);
        }
    }

    // ------------------------------------------------------------------ stream
    const stream = new KinoStream({video, movieId: Number(screen.dataset.movieId), room: code,
        episode: screen.dataset.episode != null ? Number(screen.dataset.episode) : null});
    window.KinoTracks?.mount(screen, stream);
    stream.addEventListener('phase', event => { phase.textContent = event.detail.message; });
    stream.addEventListener('stats', event => {
        const s = event.detail || {};
        statsBox.hidden = false;
        statsBox.querySelector('[data-stat=rate]').textContent = fmt.rate(s.download_rate);
        statsBox.querySelector('[data-stat=peers]').textContent = String(s.peers ?? 0);
        statsBox.querySelector('[data-stat=buffer]').textContent = s.buffer_seconds != null ? `${Math.min(999, Math.round(s.buffer_seconds))} с` : '—';
    });
    stream.addEventListener('buffering', () => sendPresence(true));
    stream.addEventListener('attached', () => {
        attached = true;
        joinBox.hidden = true;
        video.addEventListener('loadedmetadata', () => applyState(true), {once: true});
        if (video.readyState >= 1) applyState(true);
        sendPresence(true);
    });
    stream.addEventListener('autoplay-blocked', () => setSync('warn', 'Нажмите ▶ в плеере, чтобы присоединиться'));
    stream.addEventListener('error', event => { setSync('bad', event.detail.message); post('presence', {status: 'error'}).catch(() => {}); });

    async function connect() {
        connectButton.disabled = true;
        connectButton.closest('.prep-actions').hidden = true;
        prep.hidden = false;
        try {
            await stream.start({resumeAt: expected(), autoplay: Boolean(state?.playing)});
        } catch (error) {
            phase.textContent = error.message;
            connectButton.disabled = false;
            connectButton.closest('.prep-actions').hidden = false;
            prep.hidden = true;
        }
    }
    connectButton?.addEventListener('click', connect);

    video.addEventListener('play', () => {
        if (stream.switching) return;
        if (expectPlay > 0) { expectPlay -= 1; } else if (state && !state.playing) requestControl('play');
        sendPresence(true);
    });
    video.addEventListener('pause', () => {
        if (stream.switching) return;
        if (expectPause > 0) { expectPause -= 1; }
        else if (state && state.playing && !video.ended && document.visibilityState === 'visible') requestControl('pause');
        sendPresence(true);
    });
    video.addEventListener('seeked', () => {
        if (stream.switching) return;
        if (expectSeek !== null && Math.abs(video.currentTime - expectSeek) < 1.5) { expectSeek = null; return; }
        expectSeek = null;
        if (state && Math.abs(video.currentTime - expected()) > 2) requestControl('seek');
    });

    // ------------------------------------------------------------------ events
    function handle(kind, data) {
        if (data.server_time) {
            // Keep the offset honest if the computer clock jumps (sleep, NTP adjustment).
            const estimate = data.server_time - Date.now() / 1000;
            if (Math.abs(estimate - offset) > 2) offset = estimate;
        }
        switch (kind) {
        case 'snapshot':
            room = data;
            state = data.state;
            chat.replaceChildren();
            seenMessages.clear();
            data.messages.forEach(m => addMessage(m));
            renderMembers(data.members);
            renderSettings();
            document.getElementById('room-title').textContent = data.title;
            if (data.ended) return showEnded();
            if (!reactionBar.children.length) data.reactions.forEach(emoji => {
                const button = document.createElement('button');
                button.type = 'button';
                button.textContent = emoji;
                button.setAttribute('aria-label', `Реакция ${emoji}`);
                button.addEventListener('click', () => post('react', {emoji}).catch(error => toast(error.message)));
                reactionBar.append(button);
            });
            renderSettings();
            if (!attached) setSync('idle', state.playing ? `Идёт просмотр · ${fmt.time(expected())}` : 'Ожидаем начала');
            applyState();
            break;
        case 'state':
            state = data;
            applyState(data.action === 'seek');
            break;
        case 'members': renderMembers(data.items || []); break;
        case 'chat': addMessage(data); break;
        case 'notice': showNotice(data.text); addMessage({text: data.text}, {ephemeral: true}); break;
        case 'reaction': floatReaction(data.emoji, data.name); break;
        case 'settings':
            room.settings = data.settings;
            room.title = data.title;
            document.getElementById('room-title').textContent = data.title;
            renderSettings();
            break;
        case 'host':
            room.host_id = data.host_id;
            renderSettings();
            if (isHost()) toast('Теперь вы хост этой комнаты.');
            break;
        case 'movie':
            toast(`Хост выбрал «${data.title}». Перезагружаем плеер…`);
            setTimeout(() => location.reload(), 1200);
            break;
        case 'ended': showEnded(); break;
        default: break;
        }
    }

    function listen() {
        source = new EventSource(`/api/rooms/${code}/events`);
        ['snapshot', 'state', 'members', 'chat', 'notice', 'reaction', 'settings', 'host', 'movie', 'ended'].forEach(kind => {
            source.addEventListener(kind, event => {
                lastSeq = Number(event.lastEventId) || lastSeq;
                try { handle(kind, JSON.parse(event.data)); } catch (error) { console.error(error); }
            });
        });
        source.onerror = () => { if (!ended) setSync('bad', 'Связь с комнатой потеряна, переподключаемся…'); };
        source.onopen = () => { if (attached) setSync('ok', 'Связь восстановлена'); };
    }

    function showEnded() {
        if (ended) return;
        ended = true;
        source?.close();
        stream.destroy();
        video.pause();
        joinBox.hidden = false;
        joinBox.querySelector('h2').textContent = 'Киновечер завершён';
        const text = joinBox.querySelector('#room-join-text') || joinBox.querySelector('p');
        if (text) text.textContent = 'Спасибо, что смотрели вместе! Чат сохранён в истории комнат.';
        const actions = joinBox.querySelector('.prep-actions');
        if (actions) {
            actions.hidden = false;
            actions.replaceChildren();
            const link = document.createElement('a');
            link.className = 'btn';
            link.href = `/rooms/${code}/archive`;
            link.textContent = 'Открыть сохранённый чат';
            actions.append(link);
        }
        prep.hidden = true;
        chatInput.disabled = true;
        setSync('idle', 'Киновечер завершён');
    }

    // ------------------------------------------------------------------ controls
    chatForm.addEventListener('submit', async event => {
        event.preventDefault();
        const text = chatInput.value.trim();
        if (!text) return;
        chatInput.value = '';
        try { await post('chat', {text}); } catch (error) { toast(error.message); chatInput.value = text; }
        chatInput.focus();
    });
    page.querySelectorAll('[data-pane]').forEach(tab => tab.addEventListener('click', () => {
        page.querySelectorAll('[data-pane]').forEach(t => t.setAttribute('aria-selected', String(t === tab)));
        page.querySelectorAll('[data-pane-body]').forEach(body => { body.hidden = body.dataset.paneBody !== tab.dataset.pane; });
    }));
    hostToggle.addEventListener('click', () => {
        const open = hostToggle.getAttribute('aria-expanded') !== 'true';
        hostToggle.setAttribute('aria-expanded', String(open));
        hostPanel.hidden = !open;
    });
    const saveSettings = values => post('settings', values).catch(error => toast(error.message));
    document.getElementById('host-control')?.addEventListener('change', e => saveSettings({control: e.target.value}));
    [['host-wait', 'wait_for_all'], ['host-chat', 'chat'], ['host-reactions', 'reactions'], ['host-locked', 'locked']].forEach(([id, key]) => {
        document.getElementById(id)?.addEventListener('change', e => saveSettings({[key]: e.target.checked}));
    });
    document.getElementById('host-title')?.addEventListener('change', e => saveSettings({title: e.target.value}));
    document.getElementById('host-movie')?.addEventListener('change', e => post('movie', {movie_id: Number(e.target.value)}).catch(error => toast(error.message)));
    document.getElementById('host-episode')?.addEventListener('change', e => post('movie', {movie_id: Number(screen.dataset.movieId), episode: Number(e.target.value)}).catch(error => toast(error.message)));
    document.getElementById('host-transfer')?.addEventListener('change', e => {
        if (e.target.value) post('host', {user_id: Number(e.target.value)}).catch(error => toast(error.message));
    });
    const endButton = page.querySelector('[data-end-room]');
    endButton.addEventListener('click', async () => {
        if (!isHost()) { location.href = '/rooms'; return; }
        if (endButton.dataset.confirm !== '1') {
            endButton.dataset.confirm = '1';
            endButton.querySelector('span').textContent = 'Точно завершить?';
            setTimeout(() => { endButton.dataset.confirm = ''; renderSettings(); }, 4000);
            return;
        }
        try { await post('end', {}); } catch (error) { toast(error.message); }
    });
    const inviteUrl = `${location.origin}/room/${code}`;
    page.querySelector('[data-invite]').addEventListener('click', () => window.KinoDemo?.share(inviteUrl));
    page.querySelector('[data-copy-code]').addEventListener('click', () => window.KinoDemo?.share(code));
    page.querySelector('[data-fullscreen]').addEventListener('click', async () => {
        const layout = document.getElementById('room-layout');
        try { document.fullscreenElement ? await document.exitFullscreen() : await layout.requestFullscreen(); } catch (_) { toast('Браузер не разрешил полноэкранный режим.'); }
    });

    window.KinoPlayer = {getState: () => ({movieId: Number(screen.dataset.movieId), currentTime: video.currentTime || 0,
        duration: Number.isFinite(video.duration) ? video.duration : 0, playing: !video.paused}),
        pause() {}, play() {}, seekTo() {}};

    setInterval(correctDrift, 500);
    setInterval(() => sendPresence(), 2000);
    syncClock().finally(listen);
    window.addEventListener('pagehide', () => source?.close());
})();
