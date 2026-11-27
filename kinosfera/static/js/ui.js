(() => {
    'use strict';

    const userId = document.body.dataset.userId;
    const STORAGE_KEY = `kinosfera-user-${userId || 'guest'}`;
    const defaults = {watched: [], ratings: {}, watchlist: [], clips: [], settings: {}, rooms: {}, friends: []};
    const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
    const copy = value => JSON.parse(JSON.stringify(value));
    let memoryState = copy(defaults);
    let storageAvailable = true;
    let toastTimer;
    let activeClip = null;
    let lastClipsSignature = '';
    const cardTemplates = new Map();
    const catalog = new Map();
    let pendingSave, saving = false;
    async function persist() {
        if (!userId || saving) return;
        saving = true;
        try {
            while (pendingSave) {
                const data = pendingSave;
                pendingSave = null;
                const response = await fetch('/api/profile/state', {method: 'POST', keepalive: true,
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content}, body: data});
                if (!response.ok) throw new Error();
                window.dispatchEvent(new Event('kinosfera:state-saved'));
            }
        } catch (_) { toast('Не удалось сохранить изменения в аккаунте. Обновите страницу и войдите снова.'); }
        finally { saving = false; }
    }

    const movieIds = value => [...new Set((Array.isArray(value) ? value : []).map(Number).filter(id => Number.isInteger(id) && id > 0))];
    const text = (value, limit = 80) => typeof value === 'string' ? value.slice(0, limit) : '';

    function normalize(value) {
        const state = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
        const ratings = {};
        Object.entries(state.ratings && typeof state.ratings === 'object' ? state.ratings : defaults.ratings).forEach(([id, score]) => {
            if (/^[1-9]\d*$/.test(id) && Number.isInteger(Number(score)) && Number(score) >= 1 && Number(score) <= 10) ratings[id] = Number(score);
        });
        const watched = movieIds(own(state, 'watched') ? state.watched : defaults.watched);
        return {
            ...state,
            watched: movieIds([...watched, ...Object.keys(ratings)]),
            ratings,
            watchlist: movieIds(state.watchlist),
            favorites: movieIds(state.favorites),
            clips: (Array.isArray(state.clips) ? state.clips : []).filter(clip => clip && Number.isInteger(Number(clip.movieId)) && Number(clip.movieId) > 0 && Number.isFinite(Number(clip.start)) && Number(clip.start) >= 0 && Number.isFinite(Number(clip.end)) && Number(clip.end) > Number(clip.start) && Number(clip.end) - Number(clip.start) <= 60).map(clip => ({
                id: text(String(clip.id || ''), 100), movieId: Number(clip.movieId), movieTitle: text(clip.movieTitle, 150), title: text(clip.title, 80), image: text(clip.image, 500),
                start: Number(clip.start), end: Number(clip.end), format: clip.format === 'portrait' ? 'portrait' : 'landscape', pinned: Boolean(clip.pinned), createdAt: text(clip.createdAt, 40)
            })).filter(clip => clip.id),
            settings: state.settings && typeof state.settings === 'object' && !Array.isArray(state.settings) ? state.settings : {},
            rooms: state.rooms && typeof state.rooms === 'object' && !Array.isArray(state.rooms) ? state.rooms : {},
            friends: (Array.isArray(state.friends) ? state.friends : []).filter(name => typeof name === 'string').map(name => name.slice(0, 100))
        };
    }

    function read() {
        if (storageAvailable) {
            try {
                const saved = localStorage.getItem(STORAGE_KEY);
                if (saved) memoryState = normalize(JSON.parse(saved));
            } catch (error) {
                storageAvailable = false;
            }
        }
        return copy(memoryState);
    }

    function update(mutator) {
        if (!userId) {
            location.href = '/auth/login?next=' + encodeURIComponent(location.pathname + location.search);
            return read();
        }
        const draft = read();
        const result = mutator(draft);
        const replacement = result && typeof result === 'object' && !Array.isArray(result) ? result : draft;
        memoryState = normalize(replacement);
        pendingSave = JSON.stringify(memoryState);
        persist();
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(memoryState));
        } catch (error) {
            storageAvailable = false;
            toast('Браузер не разрешает сохранение. Изменения доступны до закрытия страницы.');
        }
        refresh();
        document.dispatchEvent(new CustomEvent('kinodemo:change', {detail: copy(memoryState)}));
        return copy(memoryState);
    }

    function toast(message) {
        const element = document.getElementById('toast');
        if (!element) return;
        clearTimeout(toastTimer);
        element.textContent = String(message);
        element.classList.add('is-visible');
        toastTimer = setTimeout(() => element.classList.remove('is-visible'), 3800);
    }

    function openDialog(dialog) {
        if (!dialog) return;
        if (typeof dialog.showModal === 'function') {
            if (!dialog.open) dialog.showModal();
        } else {
            dialog.setAttribute('open', '');
        }
    }

    function closeDialog(dialog) {
        if (!dialog) return;
        if (typeof dialog.close === 'function') dialog.close();
        else dialog.removeAttribute('open');
    }

    function safeImage(value) {
        try {
            const url = new URL(value, location.origin);
            return url.origin === location.origin && url.pathname.startsWith('/static/images/') ? url.href : '';
        } catch (error) {
            return '';
        }
    }

    function clipLink(clip) {
        const url = new URL(`/watch/${clip.movieId}`, location.origin);
        url.searchParams.set('start', String(clip.start));
        url.searchParams.set('end', String(clip.end));
        url.searchParams.set('clip', clip.title);
        return url;
    }

    async function share(url = location.href) {
        let copied = false;
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(String(url));
                copied = true;
            }
        } catch (error) {
            // A denied clipboard permission still allows the local selection fallback.
        }
        if (!copied) {
            const field = document.createElement('textarea');
            field.value = String(url);
            field.setAttribute('aria-label', 'Ссылка для копирования');
            field.style.position = 'fixed';
            field.style.opacity = '0';
            field.style.pointerEvents = 'none';
            const previous = document.activeElement;
            document.body.append(field);
            field.select();
            try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
            field.remove();
            if (previous && typeof previous.focus === 'function') previous.focus();
        }
        toast(copied ? 'Ссылка скопирована. Её можно отправить друзьям.' : 'Не удалось скопировать ссылку. Откройте фрагмент и скопируйте адрес страницы.');
        return copied;
    }

    function syncBookmarks(state) {
        document.querySelectorAll('[data-favorite]').forEach(button => {
            const saved = state.favorites.includes(Number(button.dataset.favorite));
            button.setAttribute('aria-pressed', String(saved));
            button.querySelector('span:last-child').textContent = saved ? 'Убрать из избранного' : 'В избранное';
        });
        document.querySelectorAll('[data-bookmark]').forEach(button => {
            const selected = state.watchlist.includes(Number(button.dataset.bookmark));
            const title = button.closest('.movie-card')?.querySelector('.card-title')?.textContent.trim();
            button.classList.toggle('is-saved', selected);
            button.setAttribute('aria-pressed', String(selected));
            button.setAttribute('aria-label', `${selected ? 'Убрать из' : 'Добавить в'} «Буду смотреть»${title ? `: ${title}` : ''}`);
            const label = button.querySelector('[data-bookmark-label]');
            if (label) label.textContent = selected ? 'В закладках' : 'Буду смотреть';
        });
    }

    function syncRatings(state) {
        document.querySelectorAll('[data-rating-panel]').forEach(panel => {
            const score = Number(state.ratings[panel.dataset.ratingPanel]) || 0;
            panel.querySelectorAll('[data-rate]').forEach(button => {
                const selected = Number(button.dataset.rate) === score;
                button.classList.toggle('is-selected', selected);
                button.setAttribute('aria-pressed', String(selected));
            });
            panel.querySelector('[data-remove-score]').hidden = !score;
            const feedback = panel.querySelector('.score-feedback');
            if (feedback) feedback.textContent = score ? `Вы оценили на ${score} из 10` : 'Ещё не оценён';
        });
    }

    function syncCollections(state) {
        document.querySelectorAll('[data-profile-collection]').forEach(collection => {
            const kind = collection.dataset.profileCollection;
            const ids = kind === 'ratings' ? Object.keys(state.ratings).map(Number) : state[kind] || [];
            if (kind === 'watched' && collection.classList.contains('profile-mini-rail')) {
                ids.forEach(id => {
                    if (!collection.querySelector(`[data-movie-id="${id}"]`) && cardTemplates.has(id)) {
                        const card = cardTemplates.get(id).cloneNode(true);
                        const addCard = collection.querySelector('.add-film-card');
                        collection.insertBefore(card, addCard);
                    }
                });
            }
            let count = 0;
            collection.querySelectorAll('.movie-card').forEach(card => {
                const id = Number(card.dataset.movieId);
                const visible = ids.includes(id);
                card.hidden = !visible;
                if (visible) count += 1;
                let badge = card.querySelector('.personal-score');
                const score = state.ratings[id];
                if ((kind === 'ratings' || kind === 'watched') && score) {
                    if (!badge) {
                        badge = document.createElement('span');
                        badge.className = 'personal-score';
                        card.querySelector('.poster-wrap')?.append(badge);
                    }
                    badge.textContent = `Ваша оценка ${score}`;
                } else if (badge) badge.remove();
            });
            const emptyId = collection.classList.contains('profile-mini-rail') ? (kind === 'watchlist' ? 'overview-watchlist-empty' : '') : 'collection-empty';
            const empty = emptyId && document.getElementById(emptyId);
            if (empty) empty.hidden = count > 0;
        });
        document.querySelectorAll('[data-stat]').forEach(element => {
            const value = element.dataset.stat;
            element.textContent = String(value === 'ratings' ? Object.keys(state.ratings).length : (state[value] || []).length);
        });
    }

    function syncSettings(state) {
        const name = text(state.settings.name, 60).trim();
        if (name) {
            document.querySelectorAll('.profile-identity h1, .profile-menu .dropdown > strong').forEach(element => { element.textContent = name; });
            const initials = name.split(/\s+/).slice(0, 2).map(part => part[0]).join('').toUpperCase();
            document.querySelectorAll('.profile-menu summary .avatar, .profile-identity .profile-avatar').forEach(element => { element.textContent = initials; });
        }
        document.querySelectorAll('[data-follow]').forEach(button => {
            const name = button.closest('.person-card')?.querySelector('h3')?.textContent.trim() || '';
            const selected = state.friends.includes(name);
            button.setAttribute('aria-pressed', String(selected));
            button.classList.toggle('is-selected', selected);
            button.textContent = selected ? 'Добавлен в друзья' : 'Добавить в друзья';
        });
    }

    const node = (tag, className, content) => {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (content !== undefined) element.textContent = String(content);
        return element;
    };
    const time = seconds => `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;

    function renderClips(state) {
        const container = document.getElementById('profile-clips');
        if (!container) return;
        const pinnedOnly = container.hasAttribute('data-pinned-only');
        const clips = state.clips.filter(clip => !pinnedOnly || clip.pinned).sort((first, second) => Number(second.pinned) - Number(first.pinned) || String(second.createdAt).localeCompare(String(first.createdAt)));
        const signature = JSON.stringify(clips) + String(pinnedOnly);
        const empty = document.getElementById('clips-empty');
        if (empty) empty.hidden = clips.length > 0;
        if (signature === lastClipsSignature) return;
        lastClipsSignature = signature;
        container.replaceChildren();
        clips.forEach(clip => {
            const film = catalog.get(clip.movieId);
            const article = node('article', `clip-card panel${clip.format === 'portrait' ? ' clip-portrait' : ''}`);
            article.dataset.clipId = clip.id;
            const art = node('a', 'clip-art');
            art.href = clipLink(clip).pathname + clipLink(clip).search;
            art.target = '_blank';
            art.rel = 'noopener';
            art.setAttribute('aria-label', `Открыть фрагмент: ${clip.title}`);
            const source = safeImage(clip.image || film?.image || '');
            if (source) {
                const poster = node('img');
                poster.src = source;
                poster.alt = clip.movieTitle || film?.title || 'Обложка фильма';
                poster.loading = 'lazy';
                art.append(poster);
            }
            art.append(node('span', 'clip-duration', `${time(clip.start)}–${time(clip.end)}`));
            const body = node('div', 'clip-body');
            body.append(node('h3', '', clip.title || 'Любимый момент'));
            body.append(node('p', 'muted small-text', clip.movieTitle || film?.title || 'Фильм'));
            const meta = node('div', 'clip-meta');
            meta.append(node('span', 'muted small-text', `${Math.round(clip.end - clip.start)} сек · ${clip.format === 'portrait' ? '9:16' : '16:9'}`));
            if (clip.pinned) meta.append(node('span', 'badge clip-pinned', 'Закреплён'));
            body.append(meta);
            const actions = node('div', 'clip-actions');
            [['pin', clip.pinned ? 'Открепить' : 'Закрепить'], ['share', 'Поделиться'], ['delete', 'Удалить']].forEach(([action, label]) => {
                const button = node('button', `btn btn-secondary btn-small${action === 'delete' ? ' clip-delete' : ''}`, label);
                button.type = 'button';
                button.dataset.clipAction = action;
                button.dataset.clipId = clip.id;
                if (action === 'pin') button.setAttribute('aria-pressed', String(clip.pinned));
                button.setAttribute('aria-label', `${label} клип «${clip.title}»`);
                actions.append(button);
            });
            body.append(actions);
            article.append(art, body);
            container.append(article);
        });
    }

    function refresh() {
        const state = read();
        syncCollections(state);
        syncBookmarks(state);
        syncRatings(state);
        syncSettings(state);
        renderClips(state);
    }

    function openClip(context) {
        const dialog = document.getElementById('clip-dialog');
        const duration = Number(context.duration);
        const movieId = Number(context.movieId);
        if (!dialog || !Number.isInteger(movieId) || movieId < 1 || !Number.isFinite(duration) || duration < 1) {
            toast('Откройте плеер фильма, чтобы сохранить момент.');
            return;
        }
        const requestedStart = Number(context.start || 0);
        const start = Math.min(Math.max(Number.isFinite(requestedStart) ? Math.floor(requestedStart) : 0, 0), Math.floor(duration) - 1);
        const requestedEnd = Number(context.end || start + 30);
        const end = Math.min(Math.max(Number.isFinite(requestedEnd) ? Math.floor(requestedEnd) : start + 30, start + 1), start + 60, Math.floor(duration));
        activeClip = {movieId, duration, movieTitle: text(context.title, 150), image: safeImage(context.image || '')};
        const form = document.getElementById('clip-form');
        form.reset();
        document.getElementById('clip-title').value = text(context.clipTitle || `${activeClip.movieTitle} — любимый момент`, 80);
        const startInput = document.getElementById('clip-start');
        const endInput = document.getElementById('clip-end');
        startInput.value = String(start);
        startInput.max = String(Math.floor(duration) - 1);
        endInput.value = String(end);
        endInput.max = String(Math.floor(duration));
        document.getElementById('clip-error').textContent = '';
        const preview = document.getElementById('clip-preview');
        if (preview) {
            preview.classList.remove('portrait');
            preview.style.backgroundImage = activeClip.image ? `linear-gradient(0deg, rgba(0,0,0,.55), rgba(0,0,0,.1)), url("${activeClip.image}")` : '';
        }
        openDialog(dialog);
        document.getElementById('clip-title').focus();
    }

    window.KinoDemo = {read, update, toast, refresh, openClip, share};

    document.querySelectorAll('.movie-card').forEach(card => {
        const id = Number(card.dataset.movieId);
        if (!cardTemplates.has(id)) cardTemplates.set(id, card.cloneNode(true));
    });
    const catalogElement = document.getElementById('catalog-json');
    if (catalogElement) {
        try { JSON.parse(catalogElement.textContent).forEach(film => catalog.set(Number(film.id), film)); } catch (error) { /* Other controls remain usable. */ }
    }

    // Account state from SQLite is authoritative on each navigation.
    try {
        const initial = JSON.parse(document.getElementById('account-state').textContent);
        memoryState = normalize(initial || defaults);
        localStorage.setItem(STORAGE_KEY, JSON.stringify(memoryState));
    } catch (_) { storageAvailable = false; }

    document.querySelectorAll('dialog').forEach(dialog => {
        dialog.addEventListener('click', event => {
            if (event.target !== dialog) return;
            const bounds = dialog.getBoundingClientRect();
            if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeDialog(dialog);
        });
    });

    document.addEventListener('click', event => {
        const target = event.target instanceof Element ? event.target.closest('button, [data-share]') : null;
        if (!target) return;
        if (target.hasAttribute('data-open-search')) {
            openDialog(document.getElementById('search-dialog'));
            document.getElementById('global-search')?.focus();
        } else if (target.hasAttribute('data-close-dialog')) closeDialog(target.closest('dialog'));
        else if (target.hasAttribute('data-favorite')) {
            const id = Number(target.dataset.favorite);
            update(state => {
                if (state.favorites.includes(id)) state.favorites = state.favorites.filter(item => item !== id);
                else state.favorites.push(id);
            });
        } else if (target.hasAttribute('data-remove-score')) {
            const id = target.closest('[data-rating-panel]').dataset.ratingPanel;
            update(state => { delete state.ratings[id]; });
            toast('Оценка удалена.');
        } else if (target.hasAttribute('data-bookmark')) {
            const id = Number(target.dataset.bookmark);
            if (!Number.isInteger(id) || id < 1) return;
            const alreadySaved = read().watchlist.includes(id);
            update(state => { state.watchlist = alreadySaved ? state.watchlist.filter(item => item !== id) : [...state.watchlist, id]; });
            toast(alreadySaved ? 'Фильм убран из «Буду смотреть».' : 'Фильм добавлен в «Буду смотреть».');
        } else if (target.hasAttribute('data-rate')) {
            const id = Number(target.closest('[data-rating-panel]')?.dataset.ratingPanel);
            const score = Number(target.dataset.rate);
            if (!Number.isInteger(id) || id < 1 || !Number.isInteger(score) || score < 1 || score > 10) return;
            update(state => { state.ratings[id] = score; if (!state.watched.includes(id)) state.watched.push(id); });
            toast(`Ваша оценка ${score}/10 сохранена в профиле.`);
        } else if (target.hasAttribute('data-scroll')) {
            const rail = target.closest('.film-section')?.querySelector('.film-rail');
            if (rail) rail.scrollBy({left: Number(target.dataset.scroll) * rail.clientWidth * .85, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
        } else if (target.hasAttribute('data-season-tab')) {
            const season = target.dataset.seasonTab;
            const box = target.closest('[data-seasons]');
            box.querySelectorAll('[data-season-tab]').forEach(tab => tab.setAttribute('aria-selected', String(tab === target)));
            box.querySelectorAll('.episode-list').forEach(list => { list.hidden = list.id !== `season-${season}`; });
        } else if (target.hasAttribute('data-show-slide')) {
            const index = Number(target.dataset.showSlide);
            const slides = [...document.querySelectorAll('[data-slide]')];
            if (!slides.some(slide => Number(slide.dataset.slide) === index)) return;
            slides.forEach(slide => { const selected = Number(slide.dataset.slide) === index; slide.hidden = !selected; slide.classList.toggle('is-active', selected); });
            document.querySelectorAll('[data-show-slide]').forEach(button => { const selected = Number(button.dataset.showSlide) === index; button.classList.toggle('is-active', selected); button.setAttribute('aria-pressed', String(selected)); });
            const counter = document.getElementById('slide-counter');
            if (counter) counter.textContent = `${String(index + 1).padStart(2, '0')} / ${String(slides.length).padStart(2, '0')}`;
            syncHeroVideos();
        } else if (target.hasAttribute('data-hero-sound')) {
            const video = target.closest('[data-slide]')?.querySelector('[data-hero-video]');
            if (!video) return;
            video.muted = !video.muted;
            target.setAttribute('aria-pressed', String(!video.muted));
            target.setAttribute('aria-label', video.muted ? 'Включить звук фрагмента' : 'Выключить звук фрагмента');
            if (video.paused) video.play().catch(() => {});
        } else if (target.hasAttribute('data-demo')) toast(target.dataset.demo);
        else if (target.hasAttribute('data-follow')) {
            const name = target.closest('.person-card')?.querySelector('h3')?.textContent.trim();
            if (!name) return;
            const selected = read().friends.includes(name);
            update(state => { state.friends = selected ? state.friends.filter(item => item !== name) : [...state.friends, name]; });
            toast(selected ? 'Убрано из друзей.' : 'Добавлено в друзья.');
        } else if (target.hasAttribute('data-share')) share(target.dataset.share || location.href);
        else if (target.hasAttribute('data-open-clip')) {
            const player = document.querySelector('[data-player]');
            const live = window.KinoPlayer?.getState?.();
            window.KinoPlayer?.pause?.();
            openClip({movieId: target.dataset.movieId || player?.dataset.movieId, title: target.dataset.title || player?.dataset.title, image: target.dataset.image || player?.dataset.image, duration: live?.duration || target.dataset.duration || player?.dataset.duration, start: Number(live?.currentTime || 0)});
        } else if (target.hasAttribute('data-clip-action')) {
            const clip = read().clips.find(item => item.id === target.dataset.clipId);
            if (!clip) return;
            if (target.dataset.clipAction === 'share') share(clipLink(clip).href);
            else if (target.dataset.clipAction === 'pin') {
                update(state => { const item = state.clips.find(item => item.id === clip.id); if (item) item.pinned = !item.pinned; });
                toast(clip.pinned ? 'Клип откреплён от профиля.' : 'Клип закреплён в профиле.');
            } else if (target.dataset.clipAction === 'delete') {
                update(state => { state.clips = state.clips.filter(item => item.id !== clip.id); });
                toast('Клип удалён из коллекции.');
            }
        }
    });

    document.addEventListener('keydown', event => {
        const typing = event.target instanceof Element && (event.target.matches('input, textarea, select') || event.target.isContentEditable);
        if (!typing && (event.key === '/' || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k')) && !document.querySelector('dialog[open]')) {
            event.preventDefault();
            openDialog(document.getElementById('search-dialog'));
            document.getElementById('global-search')?.focus();
        }
    });

    const settingsForm = document.getElementById('settings-form');
    if (settingsForm) {
        const settings = read().settings;
        if (text(settings.name, 60)) settingsForm.elements.name.value = settings.name;
        if (['friends', 'all', 'private'].includes(settings.visibility)) settingsForm.elements.visibility.value = settings.visibility;
        if (typeof settings.notifications === 'boolean') settingsForm.elements.notifications.checked = settings.notifications;
        settingsForm.addEventListener('submit', event => {
            event.preventDefault();
            const name = settingsForm.elements.name.value.trim();
            const state0 = read();
            if (!name) {
                settingsForm.elements.name.setCustomValidity('Введите имя в профиле.');
                settingsForm.elements.name.reportValidity();
                return;
            }
            const color = settingsForm.querySelector('input[name=color]:checked')?.value || state0.settings.color || 'peach';
            const bio = settingsForm.elements.bio ? settingsForm.elements.bio.value.trim().slice(0, 160) : '';
            update(state => { state.settings = {...state.settings, name, bio, color, visibility: settingsForm.elements.visibility.value, notifications: settingsForm.elements.notifications.checked}; });
            document.querySelectorAll('.profile-menu summary .avatar, .profile-identity .profile-avatar').forEach(element => {
                element.classList.remove('peach', 'blue', 'purple', 'green', 'gold'); element.classList.add(color); });
            toast('Настройки сохранены.');
        });
        settingsForm.elements.name.addEventListener('input', () => settingsForm.elements.name.setCustomValidity(''));
    }

    const clipForm = document.getElementById('clip-form');
    const clipFormat = document.getElementById('clip-format');
    if (clipFormat) clipFormat.addEventListener('change', () => {
        document.getElementById('clip-preview')?.classList.toggle('portrait', clipFormat.value === 'portrait');
    });
    if (clipForm) clipForm.addEventListener('submit', event => {
        event.preventDefault();
        const title = document.getElementById('clip-title').value.trim();
        const start = Number(document.getElementById('clip-start').value);
        const end = Number(document.getElementById('clip-end').value);
        const error = document.getElementById('clip-error');
        if (!activeClip || !title || !Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > activeClip.duration || end - start > 60) {
            error.textContent = 'Введите название и границы внутри фильма: конец позже начала, длительность от 1 до 60 секунд.';
            return;
        }
        const clip = {id: typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `clip-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`, movieId: activeClip.movieId, movieTitle: activeClip.movieTitle, title: title.slice(0, 80), image: activeClip.image, start, end, format: document.getElementById('clip-format').value, pinned: document.getElementById('clip-pin').checked, createdAt: new Date().toISOString()};
        update(state => { state.clips.unshift(clip); });
        closeDialog(document.getElementById('clip-dialog'));
        toast('Клип сохранён в «Мои клипы». Ссылкой на него можно поделиться.');
        activeClip = null;
    });

    window.addEventListener('storage', event => {
        if (event.key !== STORAGE_KEY && event.key !== null) return;
        storageAvailable = true;
        if (!event.newValue) memoryState = copy(defaults);
        refresh();
        document.dispatchEvent(new CustomEvent('kinodemo:change', {detail: read()}));
    });
    document.addEventListener('change', event => {
        if (event.target instanceof HTMLInputElement && event.target.hasAttribute('data-autosubmit')) event.target.form?.requestSubmit();
    });

    // Hero slider videos: only the visible slide plays; nothing moves for reduced-motion users.
    const calm = matchMedia('(prefers-reduced-motion: reduce)');
    let heroVisible = true;
    function syncHeroVideos() {
        document.querySelectorAll('[data-hero-video]').forEach(video => {
            const active = !video.closest('[data-slide]')?.hidden;
            if (active && heroVisible && !calm.matches && document.visibilityState === 'visible') {
                video.preload = 'auto';
                video.play().catch(() => { /* autoplay refused: the poster stays */ });
            } else {
                video.pause();
                if (!active) { video.muted = true; video.closest('[data-slide]')?.querySelector('[data-hero-sound]')?.setAttribute('aria-pressed', 'false'); }
            }
        });
    }
    const hero = document.querySelector('.hero');
    if (hero && hero.querySelector('[data-hero-video]')) {
        if ('IntersectionObserver' in window) {
            new IntersectionObserver(entries => { heroVisible = entries[0].isIntersecting; syncHeroVideos(); }, {threshold: 0.15}).observe(hero);
        }
        document.addEventListener('visibilitychange', syncHeroVideos);
        calm.addEventListener?.('change', syncHeroVideos);
        syncHeroVideos();
    }
    window.addEventListener('kinosfera:recommendations-updated', () => syncBookmarks(read()));
    refresh();
})();

