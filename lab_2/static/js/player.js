/* A local timeline preview. No video stream or shared playback is connected. */
(() => {
    'use strict';
    const screen = document.querySelector('[data-player]');
    if (!screen) return;
    const duration = Math.max(1, Number(screen.dataset.duration) || 1);
    const movieId = Number(screen.dataset.movieId);
    const progress = screen.querySelector('#player-progress');
    const timeline = screen.querySelector('#timeline');
    const status = screen.querySelector('[data-player-status]');
    const playButtons = [...screen.querySelectorAll('[data-play]')];
    const params = new URLSearchParams(location.search);
    const requestedStart = Number(params.get('start'));
    const requestedEnd = Number(params.get('end'));
    const clipMode = !screen.closest('.room-page') && params.has('start') && params.has('end') &&
        Number.isFinite(requestedStart) && Number.isFinite(requestedEnd) && requestedStart >= 0 &&
        requestedEnd > requestedStart && requestedEnd <= duration && requestedEnd - requestedStart <= 60;
    const rangeStart = clipMode ? requestedStart : 0;
    const rangeEnd = clipMode ? requestedEnd : duration;
    let currentTime = clipMode ? rangeStart : params.has('start') && Number.isFinite(requestedStart)
        ? Math.max(0, Math.min(duration, requestedStart)) : 0;
    let playing = false;
    let timer = null;
    let recorded = false;
    let locked = false;

    function toast(message) {
        if (window.KinoDemo) window.KinoDemo.toast(message);
    }
    function recordWatch() {
        if (recorded || !Number.isFinite(movieId) || movieId < 1) return;
        recorded = true;
        const append = state => {
            if (!Array.isArray(state.watched)) state.watched = [];
            if (!state.watched.some(id => Number(id) === movieId)) state.watched.push(movieId);
        };
        if (window.KinoDemo) { window.KinoDemo.update(append); return; }
        try {
            const state = JSON.parse(localStorage.getItem('kinosfera-demo') || '{}');
            append(state);
            localStorage.setItem('kinosfera-demo', JSON.stringify(state));
        } catch (_) { /* The timeline remains usable without browser storage. */ }
    }
    function formatTime(seconds) {
        const total = Math.floor(seconds);
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const secs = total % 60;
        return (hours ? `${String(hours).padStart(2, '0')}:` : '') +
            `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    }
    function buttonIcon(button) {
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('aria-hidden', 'true');
        svg.classList.add('icon');
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', playing ? 'M8 5v14M16 5v14' : 'M8 5l11 7-11 7V5z');
        svg.append(path);
        button.replaceChildren(svg);
        button.setAttribute('aria-label', playing ? 'Приостановить демонстрацию' : 'Воспроизвести демонстрацию');
        button.setAttribute('aria-pressed', String(playing));
    }
    function render() {
        if (progress) {
            progress.min = String(rangeStart);
            progress.max = String(rangeEnd);
            progress.value = String(Math.floor(currentTime));
            progress.setAttribute('aria-valuetext', `${formatTime(currentTime)} из ${formatTime(duration)}`);
        }
        if (timeline) timeline.textContent = formatTime(currentTime);
        playButtons.forEach(buttonIcon);
        if (status && !locked) {
            status.textContent = currentTime >= rangeEnd ? 'Демонстрация завершена · видеопоток не подключён'
                : playing ? 'Демо воспроизводится · видеопоток не подключён'
                    : currentTime > 0 ? 'Демо на паузе · видеопоток не подключён'
                        : 'Демо плеера · видеопоток не подключён';
        }
        screen.dispatchEvent(new CustomEvent('kinoplayer:change', { detail: { currentTime, duration, playing, movieId } }));
    }
    function pause() {
        playing = false;
        if (timer !== null) window.clearInterval(timer);
        timer = null;
        render();
    }
    function play() {
        if (locked || playing) return;
        if (currentTime >= rangeEnd) currentTime = rangeStart;
        playing = true;
        recordWatch();
        timer = window.setInterval(() => {
            currentTime = Math.min(rangeEnd, currentTime + 1);
            if (currentTime >= rangeEnd) pause();
            else render();
        }, 1000);
        render();
    }
    function seekTo(value) {
        if (locked) return;
        const target = Number(value);
        if (!Number.isFinite(target)) return;
        currentTime = Math.min(rangeEnd, Math.max(rangeStart, target));
        if (currentTime >= rangeEnd && playing) pause();
        else render();
    }
    playButtons.forEach(button => button.addEventListener('click', () => playing ? pause() : play()));
    screen.querySelectorAll('[data-seek]').forEach(button => {
        button.addEventListener('click', () => seekTo(currentTime + Number(button.dataset.seek)));
    });
    if (progress) progress.addEventListener('input', () => seekTo(progress.value));
    const fullscreenButton = screen.querySelector('[data-fullscreen]');
    if (fullscreenButton) {
        fullscreenButton.addEventListener('click', async () => {
            try {
                if (document.fullscreenElement) await document.exitFullscreen();
                else if (screen.requestFullscreen) await screen.requestFullscreen();
                else toast('В этом браузере полноэкранный режим недоступен.');
            } catch (_) { toast('Браузер не разрешил открыть полноэкранный режим.'); }
        });
        document.addEventListener('fullscreenchange', () => {
            const expanded = document.fullscreenElement === screen;
            fullscreenButton.setAttribute('aria-label', expanded ? 'Выйти из полноэкранного режима' : 'Во весь экран');
            fullscreenButton.setAttribute('aria-pressed', String(expanded));
        });
    }
    window.addEventListener('pagehide', pause);
    if (clipMode) {
        const note = document.createElement('div');
        note.className = 'panel clip-playback-notice';
        const heading = document.createElement('h3');
        heading.textContent = `Клип: ${(params.get('clip') || 'Сохранённый момент').slice(0, 80)}`;
        const description = document.createElement('p');
        description.className = 'muted';
        description.textContent = `${formatTime(rangeStart)} — ${formatTime(rangeEnd)} · демо фрагмента без видеопотока`;
        const link = document.createElement('a');
        link.className = 'text-link';
        link.href = `/watch/${movieId}`;
        link.textContent = 'Открыть весь фильм →';
        note.append(heading, description, link);
        screen.before(note);
    }
    window.KinoPlayer = {
        play, pause, seekTo,
        getState: () => ({ currentTime, duration, playing, movieId }),
        setLocked(value, message) {
            if (value) pause();
            locked = Boolean(value);
            screen.querySelectorAll('[data-play], [data-seek], #player-progress').forEach(control => { control.disabled = locked; });
            if (status && message) status.textContent = message;
        }
    };
    render();
})();
