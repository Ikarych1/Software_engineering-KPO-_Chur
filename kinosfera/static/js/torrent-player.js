/* Single-viewer player page: start/resume, live torrent stats, clips, hotkeys. */
(() => {
    'use strict';
    const screen = document.querySelector('[data-torrent-player]');
    if (!screen || !window.KinoStream) return;
    const video = document.getElementById('torrent-video');
    const message = document.getElementById('torrent-playback-status');
    const start = document.getElementById('watch-start');
    const restart = document.getElementById('watch-restart');
    const retry = document.getElementById('torrent-retry');
    const empty = document.getElementById('torrent-empty');
    const clip = document.getElementById('torrent-clip');
    const prep = document.getElementById('prep-progress');
    const phase = document.getElementById('prep-phase');
    const statsBox = document.getElementById('stream-stats');
    const movieId = Number(screen.dataset.movieId);
    const params = new URLSearchParams(location.search);
    const fmt = window.KinoFormat;
    const episode = screen.dataset.episode != null ? Number(screen.dataset.episode) : null;
    const stream = new KinoStream({video, movieId, episode});
    const nextCard = document.getElementById('next-episode-card');
    let nextTimer = null;

    // Series: offer the next episode when this one ends (10 s countdown, can be cancelled).
    function offerNext() {
        if (!screen.dataset.nextUrl || !nextCard || nextTimer) return;
        let left = 10;
        nextCard.querySelector('#next-episode-title').textContent = screen.dataset.nextLabel || 'Следующая серия';
        nextCard.querySelector('#next-episode-go').href = screen.dataset.nextUrl;
        nextCard.querySelector('#next-episode-count').textContent = String(left);
        nextCard.hidden = false;
        nextTimer = setInterval(() => {
            left -= 1;
            nextCard.querySelector('#next-episode-count').textContent = String(left);
            if (left <= 0) { clearInterval(nextTimer); location.href = screen.dataset.nextUrl; }
        }, 1000);
    }
    function cancelNext() {
        clearInterval(nextTimer);
        nextTimer = null;
        if (nextCard) nextCard.hidden = true;
    }
    document.getElementById('next-episode-cancel')?.addEventListener('click', cancelNext);
    video.addEventListener('ended', offerNext);
    video.addEventListener('seeking', () => { if (!video.ended) cancelNext(); });
    let clipEnd = Infinity, busy = false;

    const duration = () => Number.isFinite(video.duration) ? video.duration : 0;
    const getState = () => ({movieId, currentTime: video.currentTime || 0, duration: duration(), playing: !video.paused && !video.ended});
    function emit() {
        screen.dataset.duration = String(duration() || screen.dataset.duration);
        clip.disabled = !duration();
        screen.dispatchEvent(new CustomEvent('kinoplayer:change', {detail: getState()}));
    }
    function showError(text) {
        message.textContent = text;
        retry.hidden = false;
        busy = false;
        if (start) start.disabled = false;
        if (restart) restart.disabled = false;
        prep.hidden = true;
        empty.hidden = false;
        empty.querySelector('.prep-actions')?.removeAttribute('hidden');
    }

    try { const volume = Number(localStorage.getItem('kinosfera-volume')); if (volume >= 0 && volume <= 1) video.volume = volume; } catch (_) { /* optional */ }
    video.addEventListener('volumechange', () => { try { localStorage.setItem('kinosfera-volume', String(video.volume)); } catch (_) { /* optional */ } });

    stream.addEventListener('phase', event => {
        phase.textContent = event.detail.message;
        message.textContent = event.detail.message;
    });
    stream.addEventListener('stats', event => {
        const s = event.detail || {};
        statsBox.hidden = false;
        statsBox.querySelector('[data-stat=rate]').textContent = fmt.rate(s.download_rate);
        statsBox.querySelector('[data-stat=peers]').textContent = String(s.peers ?? 0);
        statsBox.querySelector('[data-stat=buffer]').textContent = s.buffer_seconds != null ? (s.buffer_seconds > 600 ? '10+ мин' : `${Math.round(s.buffer_seconds)} с`) : '—';
        statsBox.querySelector('[data-stat=progress]').textContent = `${Math.round((s.progress || 0) * 100)}%`;
        const mode = s.mode || stream.mode;
        const quality = stream.current?.quality && stream.current.quality !== 'source' ? ` · ${stream.current.quality}p` : '';
        statsBox.querySelector('[data-stat=mode]').textContent = mode === 'direct' ? 'Оригинал без перекодирования' : mode === 'hls' ? `Перекодирование на лету${quality}` : 'Подготовка';
    });
    window.KinoTracks?.mount(screen, stream);
    stream.addEventListener('attached', event => {
        empty.hidden = true;
        const quality = event.detail.quality && event.detail.quality !== 'source' ? ` · ${event.detail.quality}p` : '';
        message.textContent = event.detail.mode === 'direct' ? 'Оригинальное качество · перемотка мгновенная после загрузки' : `Видео перекодируется для браузера по мере просмотра${quality}`;
    });
    stream.addEventListener('autoplay-blocked', () => { message.textContent = 'Нажмите ▶ в плеере.'; });
    stream.addEventListener('error', event => showError(event.detail.message));

    async function begin(fromStart = false) {
        if (busy) return;
        busy = true;
        if (start) start.disabled = true;
        if (restart) restart.disabled = true;
        retry.hidden = true;
        empty.querySelector('.prep-actions')?.setAttribute('hidden', '');
        prep.hidden = false;
        let resumeAt = null;
        const requested = Number(params.get('start'));
        if (params.has('start') && Number.isFinite(requested)) resumeAt = Math.max(0, requested);
        else if (fromStart) resumeAt = 0;
        else resumeAt = Number(screen.dataset.resume) || 0;
        try {
            await stream.start({resumeAt});
            busy = false;
        } catch (error) { showError(error.message); }
    }
    start?.addEventListener('click', () => begin(false));
    restart?.addEventListener('click', () => begin(true));
    retry.addEventListener('click', () => begin(false));

    video.addEventListener('pause', () => { if (stream.mode) message.textContent = video.ended ? 'Просмотр завершён' : 'На паузе · загрузка вперёд продолжается'; emit(); });
    video.addEventListener('playing', () => {
        message.textContent = 'Приятного просмотра'; retry.hidden = true;
        emit();
    });
    stream.addEventListener('buffering', event => { if (event.detail.buffering && stream.mode && !video.paused) message.textContent = 'Буферизация…'; });
    video.addEventListener('loadedmetadata', () => {
        const startAt = Number(params.get('start'));
        const endAt = Number(params.get('end'));
        if (params.has('end') && endAt > startAt && endAt <= duration() && endAt - startAt <= 60) clipEnd = endAt;
        emit();
    });
    video.addEventListener('timeupdate', () => { if (video.currentTime >= clipEnd) { video.pause(); clipEnd = Infinity; } emit(); });
    video.addEventListener('durationchange', emit);
    clip.addEventListener('click', () => {
        video.pause();
        window.KinoDemo?.openClip({movieId, title: screen.dataset.title, image: screen.dataset.image, duration: duration(), start: video.currentTime});
    });
    document.addEventListener('keydown', event => {
        if (!stream.mode || event.target.closest('input, textarea, select, dialog') || event.ctrlKey || event.metaKey || event.altKey) return;
        const key = event.key.toLowerCase();
        if (key === ' ' || key === 'k') { event.preventDefault(); video.paused ? video.play() : video.pause(); }
        else if (key === 'arrowright') { event.preventDefault(); video.currentTime = Math.min(duration(), video.currentTime + 10); }
        else if (key === 'arrowleft') { event.preventDefault(); video.currentTime = Math.max(0, video.currentTime - 10); }
        else if (key === 'f' || key === 'а') { event.preventDefault(); document.fullscreenElement ? document.exitFullscreen() : screen.requestFullscreen?.(); }
        else if (key === 'm' || key === 'ь') { video.muted = !video.muted; }
        else if ((key === 'n' || key === 'т') && event.shiftKey && screen.dataset.nextUrl) { location.href = screen.dataset.nextUrl; }
    });
    window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
    window.KinoPlayer = {getState, play: () => video.play(), pause: () => video.pause(),
        seekTo: value => { if (duration()) video.currentTime = Math.max(0, Math.min(Number(value) || 0, duration())); }};
    if (params.has('start') || params.get('autoplay') === '1') begin(false);
    emit();
})();
