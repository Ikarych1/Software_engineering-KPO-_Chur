/* Local room interactions and a browser-only chat archive. No network calls. */
(() => {
    'use strict';
    let volatileState = { rooms: {} };
    function read() {
        if (window.KinoDemo) return window.KinoDemo.read();
        try {
            const parsed = JSON.parse(localStorage.getItem('kinosfera-demo') || '{}');
            return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : volatileState;
        } catch (_) { return volatileState; }
    }
    function update(mutator) {
        if (window.KinoDemo) return window.KinoDemo.update(mutator);
        const state = read();
        mutator(state);
        volatileState = state;
        try { localStorage.setItem('kinosfera-demo', JSON.stringify(state)); } catch (_) { /* Memory-only demo. */ }
        return state;
    }
    function toast(message) {
        if (window.KinoDemo) window.KinoDemo.toast(message);
    }
    function roomEntries() {
        const rooms = read().rooms;
        if (!rooms || typeof rooms !== 'object' || Array.isArray(rooms)) return [];
        return Object.entries(rooms).filter(([, room]) => room && typeof room === 'object');
    }
    function safeImage(image) {
        try {
            const url = new URL(String(image || ''), location.origin);
            if (url.origin === location.origin && url.pathname.startsWith('/static/images/')) return url.pathname;
        } catch (_) { /* Use the fixture cover below. */ }
        return '/static/images/inception.jpg';
    }
    function messageNode(message) {
        if (!message || typeof message !== 'object') message = {};
        const row = document.createElement('div');
        row.className = 'chat-message';
        const avatar = document.createElement('span');
        const sender = String(message.sender || 'Вы').slice(0, 80);
        avatar.className = `avatar ${sender === 'Соня' ? 'purple' : sender === 'Михаил' ? 'blue' : 'peach'} tiny`;
        avatar.textContent = sender.slice(0, 1);
        const content = document.createElement('div');
        const heading = document.createElement('strong');
        heading.append(document.createTextNode(`${sender} `));
        const time = document.createElement('time');
        time.textContent = String(message.time || '').slice(0, 8);
        if (message.timestamp) time.dateTime = String(message.timestamp);
        heading.append(time);
        const text = document.createElement('p');
        text.textContent = String(message.text || '').slice(0, 1000);
        content.append(heading, text);
        row.append(avatar, content);
        return row;
    }
    function archiveUrl(key, room) {
        const params = new URLSearchParams({ room: room.code, movie: String(room.movieId), session: room.archiveKey || key });
        return `/rooms/archive/local?${params}`;
    }
    function dateLabel(value) {
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? 'Сохранён в браузере'
            : date.toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' });
    }
    function renderHistory() {
        const grid = document.querySelector('[data-local-room-history]') || document.querySelector('.rooms-page .archive-grid');
        if (!grid) return;
        grid.querySelectorAll('[data-local-archive]').forEach(node => node.remove());
        const allRooms = roomEntries();
        const completed = allRooms.filter(([key, room]) => room.endedAt &&
            (!room.archiveKey || room.archiveKey === key || !allRooms.some(([entryKey]) => entryKey === room.archiveKey)))
            .sort((a, b) => String(b[1].endedAt).localeCompare(String(a[1].endedAt)));
        const fragment = document.createDocumentFragment();
        completed.forEach(([key, room]) => {
            const card = document.createElement('a');
            card.className = 'archive-card panel';
            card.dataset.localArchive = '';
            card.href = archiveUrl(key, room);
            const image = document.createElement('img');
            image.src = safeImage(room.image);
            image.alt = String(room.title || 'Фильм');
            const content = document.createElement('div');
            const badge = document.createElement('span');
            badge.className = 'badge';
            badge.textContent = `Завершён · ${dateLabel(room.endedAt)}`;
            const heading = document.createElement('h3');
            heading.textContent = String(room.heading || 'Вечер с друзьями');
            const subtitle = document.createElement('p');
            subtitle.className = 'muted';
            subtitle.textContent = `${room.title} · ${Array.isArray(room.messages) ? room.messages.length : 0} сообщений`;
            const link = document.createElement('span');
            link.className = 'text-link';
            link.textContent = 'Сохранённый чат →';
            content.append(badge, heading, subtitle, link);
            card.append(image, content);
            fragment.append(card);
        });
        grid.prepend(fragment);
    }
    function renderArchive() {
        const page = document.querySelector('.archive-page');
        if (!page || (page.dataset.archiveCode !== 'local' && !location.pathname.endsWith('/archive/local'))) return;
        const params = new URLSearchParams(location.search);
        const requestedKey = params.get('session') || `${params.get('room')}:${params.get('movie')}`;
        const found = roomEntries().find(([key]) => key === requestedKey);
        const room = found && found[1].endedAt ? found[1] : null;
        const heading = page.querySelector('h1');
        const archive = page.querySelector('.archive-chat');
        if (!archive) return;
        archive.replaceChildren();
        const title = document.createElement('h3');
        title.textContent = room ? 'Сохранённый разговор' : 'Сеанс ещё не сохранён';
        const notice = document.createElement('p');
        notice.className = 'muted small-text';
        notice.textContent = room ? 'Сообщения этой демо-комнаты сохранены только в вашем браузере.'
            : 'Завершите демо-комнату, чтобы её чат появился здесь. Архив доступен в том браузере, где прошёл сеанс.';
        archive.append(title, notice);
        if (heading) {
            heading.textContent = room ? String(room.heading || 'Вечер с друзьями') : 'Сохранённый чат';
            if (heading.nextElementSibling) heading.nextElementSibling.textContent = room
                ? `${room.title} · завершён ${dateLabel(room.endedAt)} · вы, Михаил и Соня (демо)` : 'Локальный архив демо-комнат';
        }
        const movieLink = page.querySelector('a.btn');
        if (movieLink) {
            movieLink.hidden = !room;
            if (room) movieLink.href = `/movie/${Number(room.movieId) || 1}`;
        }
        if (room) {
            const messages = Array.isArray(room.messages) ? room.messages : [];
            messages.forEach(message => archive.append(messageNode(message)));
            if (!messages.length) {
                const empty = document.createElement('p');
                empty.className = 'muted';
                empty.textContent = 'В этом киновечере сообщений пока не было.';
                archive.append(empty);
            }
        }
    }
    renderHistory();
    renderArchive();
    window.addEventListener('storage', event => {
        if (event.key === 'kinosfera-demo') { renderHistory(); renderArchive(); }
    });

    const page = document.querySelector('.room-page');
    if (!page) return;
    const player = page.querySelector('[data-player]');
    const code = page.dataset.roomCode || decodeURIComponent(location.pathname.split('/').pop() || 'ROOM-359');
    const movieId = Number(player && player.dataset.movieId) || 2;
    const key = `${code}:${movieId}`;
    const isHost = !['false', 'False', '0'].includes(page.dataset.isHost);
    const chat = page.querySelector('#chat-messages');
    const form = page.querySelector('#chat-form');
    const input = page.querySelector('#chat-input');
    const notice = page.querySelector('.chat-notice');
    const hostPanel = page.querySelector('#host-panel');
    const reactionPicker = page.querySelector('#reaction-picker');
    const reactionToggle = page.querySelector('[data-toggle-reactions]');
    const stage = page.querySelector('#reaction-stage');
    let room = roomEntries().find(([entryKey]) => entryKey === key)?.[1];
    let lastSavedProgress = 0;
    if (!room) {
        const initialMessages = chat ? [...chat.querySelectorAll('.chat-message')].map(node => ({
            sender: node.querySelector('strong')?.firstChild?.textContent.trim() || 'Демо-участник',
            text: node.querySelector('p')?.textContent || '',
            time: node.querySelector('time')?.textContent || '', timestamp: new Date().toISOString()
        })) : [];
        room = {
            code, movieId, title: player?.dataset.title || 'Киновечер',
            image: player?.dataset.image || '/static/images/inception.jpg',
            heading: 'Вечер с друзьями', messages: initialMessages,
            settings: { reactions: true, chat: true }, startedAt: new Date().toISOString(), endedAt: null, progress: 0
        };
        save();
    }
    if (!Array.isArray(room.messages)) room.messages = [];
    room.settings = { reactions: true, chat: true, ...room.settings };
    function save() {
        update(state => {
            if (!state.rooms || typeof state.rooms !== 'object' || Array.isArray(state.rooms)) state.rooms = {};
            state.rooms[key] = room;
            if (room.endedAt && room.archiveKey) state.rooms[room.archiveKey] = room;
        });
    }
    function renderChat() {
        if (!chat) return;
        const day = document.createElement('div');
        day.className = 'chat-day';
        day.textContent = room.endedAt ? 'Сохранённый разговор' : 'Киновечер';
        chat.replaceChildren(day);
        room.messages.forEach(message => chat.append(messageNode(message)));
        chat.scrollTop = chat.scrollHeight;
    }
    function setVisible(element, visible) {
        if (!element) return;
        element.hidden = !visible;
        element.style.display = visible ? '' : 'none';
    }
    function applySettings() {
        const canChat = Boolean(room.settings.chat && !room.endedAt);
        if (form) form.querySelectorAll('input, button').forEach(element => { element.disabled = !canChat; });
        if (input) input.placeholder = room.endedAt ? 'Сеанс завершён · чат сохранён'
            : canChat ? 'Поделиться впечатлением…' : 'Хост отключил сообщения';
        if (reactionToggle) reactionToggle.disabled = !room.settings.reactions || Boolean(room.endedAt);
        if (!room.settings.reactions || room.endedAt) {
            setVisible(reactionPicker, false);
            reactionToggle?.setAttribute('aria-expanded', 'false');
        }
        if (hostPanel) {
            const options = hostPanel.querySelectorAll('input[type="checkbox"]');
            if (options[0]) options[0].checked = Boolean(room.settings.reactions);
            if (options[1]) options[1].checked = Boolean(room.settings.chat);
            options.forEach(option => { option.disabled = !isHost || Boolean(room.endedAt); });
        }
    }
    function showEnded() {
        if (!room.endedAt) return;
        window.KinoPlayer?.setLocked(true, 'Киновечер завершён · разговор сохранён в этом браузере');
        if (notice) notice.textContent = 'Киновечер завершён. Сообщения доступны в сохранённом чате.';
        if (!page.querySelector('[data-room-ended]')) {
            const banner = document.createElement('div');
            banner.className = 'panel room-ended-notice';
            banner.dataset.roomEnded = '';
            const title = document.createElement('h3');
            title.textContent = 'Киновечер сохранён';
            const description = document.createElement('p');
            description.className = 'muted';
            description.textContent = 'Вернитесь к разговору после титров. Чат хранится в этом браузере.';
            const link = document.createElement('a');
            link.className = 'btn btn-secondary';
            link.href = archiveUrl(key, room);
            link.textContent = 'Открыть сохранённый чат';
            const restart = document.createElement('button');
            restart.className = 'btn btn-secondary';
            restart.type = 'button';
            restart.textContent = 'Начать новый киновечер';
            restart.addEventListener('click', () => {
                if (!room.archiveKey) {
                    room.archiveKey = `${key}:${room.endedAt}`;
                    save();
                }
                room = { code, movieId, title: room.title, image: room.image, heading: room.heading,
                    messages: [], settings: { reactions: true, chat: true }, startedAt: new Date().toISOString(),
                    endedAt: null, progress: 0 };
                save();
                window.KinoPlayer?.setLocked(false);
                window.KinoPlayer?.seekTo(0);
                banner.remove();
                page.querySelectorAll('[data-device], [data-host-tools]').forEach(button => { button.disabled = false; });
                page.querySelectorAll('[data-device]').forEach(button => {
                    button.setAttribute('aria-pressed', 'false');
                    const label = button.querySelector('span');
                    if (label) label.textContent = `${button.dataset.device === 'mic' ? 'Микрофон' : 'Камера'} выкл.`;
                });
                const end = page.querySelector('[data-end-room]');
                end?.classList.add('leave-button');
                if (end?.querySelector('span')) end.querySelector('span').textContent = 'Завершить';
                if (notice) notice.textContent = 'Сообщения нового демо-сеанса сохраняются в этом браузере.';
                renderChat();
                applySettings();
                toast('Новый демо-киновечер начат. Предыдущий разговор остался в архиве.');
            });
            banner.append(title, description, link, restart);
            page.querySelector('.room-heading')?.after(banner);
        }
        page.querySelectorAll('[data-device], [data-host-tools]').forEach(button => { button.disabled = true; });
        setVisible(hostPanel, false);
        const endButton = page.querySelector('[data-end-room]');
        if (endButton) {
            const label = endButton.querySelector('span');
            if (label) label.textContent = 'Сохранённый чат';
            endButton.classList.remove('leave-button');
        }
        applySettings();
    }
    renderChat();
    if (Number.isFinite(Number(room.progress))) window.KinoPlayer?.seekTo(room.progress);
    applySettings();
    showEnded();
    form?.addEventListener('submit', event => {
        event.preventDefault();
        if (!input || !room.settings.chat || room.endedAt) return;
        const text = input.value.trim().slice(0, 1000);
        if (!text) return;
        const now = new Date();
        room.messages.push({ sender: 'Вы', text, timestamp: now.toISOString(),
            time: now.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' }) });
        input.value = '';
        save();
        renderChat();
        input.focus();
        if (notice) notice.textContent = 'Сообщение сохранено в этом браузере. После завершения весь чат появится в архиве.';
    });
    page.querySelectorAll('[data-device]').forEach(button => button.addEventListener('click', () => {
        if (room.endedAt) return;
        const enabled = button.getAttribute('aria-pressed') !== 'true';
        const label = button.dataset.device === 'mic' ? 'Микрофон' : 'Камера';
        button.setAttribute('aria-pressed', String(enabled));
        const text = button.querySelector('span');
        if (text) text.textContent = `${label} ${enabled ? 'вкл.' : 'выкл.'}`;
        button.title = 'Демонстрация состояния: устройство не подключено';
        toast(`${label}: ${enabled ? 'вкл.' : 'выкл.'} в макете. Устройство не подключается.`);
    }));
    const participantsToggle = page.querySelector('[data-toggle-participants]');
    participantsToggle?.addEventListener('click', () => {
        const visible = participantsToggle.getAttribute('aria-pressed') !== 'true';
        participantsToggle.setAttribute('aria-pressed', String(visible));
        setVisible(page.querySelector('#participant-strip'), visible);
    });
    const chatToggle = page.querySelector('[data-toggle-chat]');
    chatToggle?.addEventListener('click', () => {
        const visible = chatToggle.getAttribute('aria-pressed') !== 'true';
        chatToggle.setAttribute('aria-pressed', String(visible));
        setVisible(page.querySelector('#chat-pane'), visible);
        const layout = page.querySelector('#room-layout');
        if (layout) layout.style.gridTemplateColumns = visible ? '' : 'minmax(0, 1fr)';
    });
    reactionToggle?.addEventListener('click', () => {
        if (!room.settings.reactions || room.endedAt) return;
        const visible = reactionToggle.getAttribute('aria-expanded') !== 'true';
        reactionToggle.setAttribute('aria-expanded', String(visible));
        setVisible(reactionPicker, visible);
    });
    reactionPicker?.querySelectorAll('[data-reaction]').forEach(button => button.addEventListener('click', () => {
        if (!room.settings.reactions || room.endedAt || !stage) return;
        const reaction = document.createElement('span');
        reaction.className = 'floating-reaction';
        reaction.textContent = button.dataset.reaction;
        reaction.setAttribute('aria-hidden', 'true');
        reaction.style.left = `${20 + Math.random() * 60}%`;
        stage.append(reaction);
        if (reaction.animate) reaction.animate([
            { transform: 'translateY(0) scale(.7)', opacity: 0 },
            { transform: 'translateY(-30px) scale(1.2)', opacity: 1, offset: .2 },
            { transform: 'translateY(-180px) scale(1)', opacity: 0 }
        ], { duration: 1800, easing: 'ease-out' });
        window.setTimeout(() => reaction.remove(), 1800);
    }));
    const hostToggle = page.querySelector('[data-host-tools]');
    hostToggle?.setAttribute('aria-expanded', 'false');
    hostToggle?.addEventListener('click', () => {
        if (room.endedAt) return;
        if (!isHost) { toast('Управление настройками доступно хосту демо-комнаты.'); return; }
        const visible = hostToggle.getAttribute('aria-expanded') !== 'true';
        hostToggle.setAttribute('aria-expanded', String(visible));
        setVisible(hostPanel, visible);
    });
    if (hostPanel) hostPanel.querySelectorAll('input[type="checkbox"]').forEach((option, index) => {
        option.addEventListener('change', () => {
            if (!isHost || room.endedAt) return;
            room.settings[index === 0 ? 'reactions' : 'chat'] = option.checked;
            save();
            applySettings();
            toast(`${index === 0 ? 'Реакции' : 'Сообщения в чате'} ${option.checked ? 'разрешены' : 'отключены'} в демо-комнате.`);
        });
    });
    page.querySelector('[data-end-room]')?.addEventListener('click', () => {
        if (room.endedAt) { location.href = archiveUrl(key, room); return; }
        window.KinoPlayer?.pause();
        room.progress = window.KinoPlayer?.getState().currentTime || 0;
        room.endedAt = new Date().toISOString();
        room.archiveKey = `${key}:${room.endedAt}`;
        save();
        renderChat();
        showEnded();
        toast('Киновечер завершён. Чат сохранён в локальном архиве.');
    });
    player?.addEventListener('kinoplayer:change', event => {
        if (room.endedAt) return;
        const position = Number(event.detail.currentTime) || 0;
        room.progress = position;
        if (!event.detail.playing || Math.abs(position - lastSavedProgress) >= 10) {
            lastSavedProgress = position;
            save();
        }
    });
    window.addEventListener('pagehide', () => {
        if (!room.endedAt) {
            room.progress = window.KinoPlayer?.getState().currentTime || 0;
            save();
        }
    });
})();
