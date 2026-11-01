/* KinoStream: browser side of torrent streaming.
 * Chooses the fastest delivery the browser supports — the original file over HTTP
 * Range when codecs allow, otherwise HLS — and falls back automatically.
 * Shared by the player page (torrent-player.js) and watch-party rooms (room.js). */
(() => {
    'use strict';
    const csrf = () => document.querySelector('meta[name=csrf-token]')?.content || '';
    const isChromium = () => Boolean(navigator.userAgentData?.brands?.some(b => /Chromium|Google Chrome|Microsoft Edge/.test(b.brand)))
        || (/Chrome\//.test(navigator.userAgent) && !/Firefox\//.test(navigator.userAgent));
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const stored = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
    const store = (key, value) => { try { localStorage.setItem(key, value); } catch (_) { /* optional */ } };

    function formatRate(bytes) {
        if (!bytes) return '0 КБ/с';
        return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} МБ/с` : `${Math.max(1, Math.round(bytes / 1024))} КБ/с`;
    }
    function formatTime(seconds) {
        seconds = Math.max(0, Math.floor(Number(seconds) || 0));
        const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
        return (h ? `${h}:${String(m).padStart(2, '0')}` : String(m).padStart(2, '0')) + `:${String(s).padStart(2, '0')}`;
    }

    class KinoStream extends EventTarget {
        constructor({video, movieId, room = null, episode = null}) {
            super();
            this.video = video;
            this.movieId = Number(movieId);
            this.room = room;
            this.episode = Number.isInteger(episode) ? episode : null;
            this.choice = {quality: null, audio: null};     // null: automatic / remembered preference
            this.current = {quality: null, audio: 0};
            this.clientId = (crypto.randomUUID?.() || `${Date.now()}${Math.random()}`).replace(/[^a-zA-Z0-9]/g, '').slice(0, 32).padEnd(16, '0');
            this.session = null;
            this.state = null;
            this.mode = null;
            this.hls = null;
            this.active = true;
            this.buffering = false;
            this.generation = 0;
            this.heartbeatTimer = null;
            video.addEventListener('waiting', () => this.setBuffering(true));
            video.addEventListener('playing', () => this.setBuffering(false));
            video.addEventListener('canplay', () => { if (!video.paused) this.setBuffering(false); });
            video.addEventListener('seeking', () => this.setBuffering(true));
            video.addEventListener('seeked', () => { if (video.readyState >= 3) this.setBuffering(false); });
            window.addEventListener('pagehide', () => this.destroy(true));
        }

        emit(type, detail = {}) { this.dispatchEvent(new CustomEvent(type, {detail})); }
        setBuffering(value) {
            if (this.buffering === value) return;
            this.buffering = value;
            this.emit('buffering', {buffering: value});
        }

        async api(path, body, {keepalive = false, method = 'POST'} = {}) {
            const options = {method, keepalive, headers: {'X-CSRF-Token': csrf()}};
            if (method === 'POST') {
                options.headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(body || {});
            }
            const response = await fetch(path, options);
            let data = {};
            try { data = await response.json(); } catch (_) { /* handled below */ }
            if (!response.ok) {
                const error = new Error(data.error || 'Сервер недоступен. Обновите страницу.');
                error.status = response.status;
                throw error;
            }
            return data;
        }

        /** Start (or restart) playback; resolves when the video element has a source. */
        async start({resumeAt = null, autoplay = true} = {}) {
            const generation = ++this.generation;
            this.teardownMedia();
            if (this.session) await this.api(`/api/play/s/${this.session}/end`, {}).catch(() => {});
            this.emit('phase', {phase: 'connecting', message: 'Подключаемся к раздаче…'});
            const started = await this.api(`/api/play/${this.movieId}/start`, {client_id: this.clientId, room: this.room, episode: this.episode});
            if (generation !== this.generation) return;
            this.session = started.session_id;
            this.startHeartbeat();
            let state = started;
            const began = performance.now();
            while (state.state !== 'ready') {
                if (state.state === 'error') throw new Error(state.message || 'Не удалось подготовить видео.');
                this.emit('stats', state.stats || {});
                const seconds = Math.round((performance.now() - began) / 1000);
                const peers = state.stats?.peers || 0;
                this.emit('phase', {phase: 'preparing', seconds, message: peers
                    ? `Загружаем начало фильма · ${peers} ${peers % 10 === 1 && peers % 100 !== 11 ? 'участник' : 'участников'} · ${formatRate(state.stats.download_rate)}`
                    : 'Ищем участников раздачи…'});
                await sleep(seconds < 10 ? 700 : 1500);
                if (generation !== this.generation || !this.active) return;
                state = await this.api(`/api/play/s/${this.session}/status`, null, {method: 'GET'});
            }
            this.state = state;
            this.emit('ready', state);
            const position = resumeAt ?? (state.resume_position || 0);
            await this.attach(state, position, autoplay, generation);
        }

        canDirect(plan) {
            if (plan.mode !== 'direct') return false;
            if (plan.chromium_only) return isChromium();
            const type = plan.codecs ? `${plan.mime}; codecs="${plan.codecs}"` : plan.mime;
            return this.video.canPlayType(type) !== '' || this.video.canPlayType(plan.mime) === 'probably';
        }

        /** Quality and audio track to use: explicit choice, then remembered preference, then the server default. */
        resolveChoice(state) {
            const qualities = state.qualities || [];
            const tracks = state.audio_tracks || [];
            const defaultAudio = state.default_audio ?? 0;
            let audio = this.choice.audio;
            if (audio == null) {
                const language = stored('kinosfera-audio-language');
                audio = tracks.find(t => language && t.language === language)?.index ?? defaultAudio;
            }
            if (!tracks.some(t => t.index === audio)) audio = defaultAudio;
            let quality = this.choice.quality ?? stored('kinosfera-quality');
            if (!qualities.some(q => q.id === quality)) quality = state.default_quality || qualities[0]?.id || null;
            // The browser plays the file's main track only: another dubbing needs the transcoded stream.
            if (quality === 'source' && audio !== defaultAudio) quality = qualities.find(q => q.id !== 'source')?.id || quality;
            return {quality, audio, defaultAudio};
        }

        hlsUrl(state, quality, audio) {
            const height = Number(quality) || Number((state.qualities || []).find(q => q.id !== 'source')?.id) || 0;
            return height && state.hls_base ? `${state.hls_base}/${height}-${audio}/index.m3u8` : state.hls_url;
        }

        async attach(state, position, autoplay, generation) {
            const plan = state.plan;
            this.duration = state.duration;
            const {quality, audio, defaultAudio} = this.resolveChoice(state);
            const wantsSource = !quality || quality === 'source';
            if (wantsSource && audio === defaultAudio && this.canDirect(plan)) {
                try {
                    await this.attachDirect(state, position, generation);
                    this.current = {quality: 'source', audio};
                } catch (error) {
                    if (generation !== this.generation) return;
                    this.emit('phase', {phase: 'fallback', message: 'Браузер не смог открыть файл напрямую — включаем перекодирование…'});
                    const fallback = (state.qualities || []).find(q => q.id !== 'source')?.id || null;
                    await this.attachHls(state, position, generation, this.hlsUrl(state, fallback, audio));
                    this.current = {quality: fallback, audio};
                }
            } else {
                const hlsQuality = wantsSource ? (state.qualities || []).find(q => q.id !== 'source')?.id || null : quality;
                await this.attachHls(state, position, generation, this.hlsUrl(state, hlsQuality, audio));
                this.current = {quality: hlsQuality, audio};
            }
            if (generation !== this.generation) return;
            this.emit('attached', {mode: this.mode, quality: this.current.quality, audio: this.current.audio});
            if (autoplay) {
                try { await this.video.play(); }
                catch (_) { this.emit('autoplay-blocked'); }
            }
        }

        attachDirect(state, position, generation) {
            const video = this.video;
            this.mode = 'direct';
            return new Promise((resolve, reject) => {
                let done = false;
                const cleanup = () => {
                    clearTimeout(timer);
                    video.removeEventListener('loadedmetadata', onMeta);
                    video.removeEventListener('error', onError);
                };
                const onMeta = () => {
                    if (done) return;
                    // Matroska labelled as WebM may "load" without a decodable video track.
                    if (!video.videoWidth) { onError(); return; }
                    done = true; cleanup();
                    if (position > 0) video.currentTime = position;
                    video.addEventListener('error', () => this.recover(), {once: true});
                    resolve();
                };
                const onError = () => {
                    if (done) return;
                    done = true; cleanup();
                    video.removeAttribute('src'); video.load();
                    reject(new Error('direct playback failed'));
                };
                const timer = setTimeout(onError, 45000);
                video.addEventListener('loadedmetadata', onMeta);
                video.addEventListener('error', onError);
                video.preload = 'auto';
                video.src = state.direct_url;
                video.load();
            });
        }

        /** Switch quality and/or dubbing in place: same session, same position. */
        async setVariant(change) {
            if (!this.state) return;
            Object.assign(this.choice, change);
            if (change.quality != null) store('kinosfera-quality', change.quality);
            if (change.audio != null) {
                const track = (this.state.audio_tracks || []).find(t => t.index === change.audio);
                if (track?.language) store('kinosfera-audio-language', track.language);
            }
            const position = this.video.currentTime || 0;
            const playing = !this.video.paused && !this.video.ended;
            const generation = ++this.generation;
            this.switching = true;      // pause/seek events of the rebuilt player are not the viewer's
            this.teardownMedia();
            this.emit('phase', {phase: 'switch', message: 'Переключаем качество и звук…'});
            try { await this.attach(this.state, position, playing, generation); }
            catch (error) { this.emit('error', {message: error.message}); }
            finally { setTimeout(() => { if (generation === this.generation) this.switching = false; }, 1500); }
        }

        attachHls(state, position, generation, url = state.hls_url) {
            const video = this.video;
            this.mode = 'hls';
            if (window.Hls?.isSupported()) {
                return new Promise((resolve, reject) => {
                    const hls = new Hls({
                        startPosition: position > 0 ? position : -1,
                        // Enough to ride out a slow swarm without keeping minutes of video in the tab's memory.
                        maxBufferLength: 45, maxMaxBufferLength: 90, backBufferLength: 20,
                        maxBufferSize: 60 * 1000 * 1000, startFragPrefetch: true,
                        manifestLoadingTimeOut: 60000, manifestLoadingMaxRetry: 4,
                        fragLoadingTimeOut: 110000, fragLoadingMaxRetry: 8, fragLoadingRetryDelay: 1000,
                        levelLoadingTimeOut: 60000, enableWorker: true, lowLatencyMode: false,
                    });
                    this.hls = hls;
                    let ready = false;
                    hls.on(Hls.Events.MANIFEST_PARSED, () => { ready = true; resolve(); });
                    hls.on(Hls.Events.ERROR, (_event, data) => {
                        if (!data.fatal) return;
                        if (!ready) { reject(new Error('Не удалось открыть видеопоток.')); return; }
                        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
                            this.emit('phase', {phase: 'retry', message: 'Сеть или раздача замедлились — переподключаемся…'});
                            setTimeout(() => hls.startLoad(), 1500);
                        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                            hls.recoverMediaError();
                        } else {
                            this.emit('error', {message: 'Видеопоток прервался. Нажмите «Повторить».'});
                        }
                    });
                    hls.loadSource(url);
                    hls.attachMedia(video);
                });
            }
            if (video.canPlayType('application/vnd.apple.mpegurl')) {
                video.src = url;
                video.addEventListener('loadedmetadata', () => { if (position > 0) video.currentTime = position; }, {once: true});
                return Promise.resolve();
            }
            return Promise.reject(new Error('Откройте фильм в современном Edge, Chrome, Firefox или Safari.'));
        }

        recover() {
            if (!this.active || this.mode !== 'direct' || !this.state) return;
            const position = this.video.currentTime || 0;
            const wasPlaying = !this.video.paused;
            this.emit('phase', {phase: 'retry', message: 'Восстанавливаем поток…'});
            setTimeout(() => {
                if (!this.active) return;
                this.video.src = this.state.direct_url;
                this.video.addEventListener('loadedmetadata', () => {
                    this.video.currentTime = position;
                    if (wasPlaying) this.video.play().catch(() => {});
                }, {once: true});
                this.video.addEventListener('error', () => this.recover(), {once: true});
            }, 1200);
        }

        startHeartbeat() {
            clearInterval(this.heartbeatTimer);
            this.heartbeatTimer = setInterval(() => this.beat(), 4000);
        }

        async beat() {
            if (!this.session || !this.active) return;
            const video = this.video;
            try {
                const state = await this.api(`/api/play/s/${this.session}/heartbeat`, {
                    position: video.currentTime || 0, playing: !video.paused && !video.ended,
                    buffering: this.buffering, duration: Number.isFinite(video.duration) ? video.duration : (this.duration || 0),
                    mode: this.mode || ''});
                this.emit('stats', Object.assign(state.stats || {}, {mode: this.mode}));
            } catch (error) {
                if (error.status === 404 && this.active) {
                    // Session expired (sleep/network loss): reopen transparently at the same position.
                    const position = video.currentTime || 0;
                    const playing = !video.paused;
                    this.session = null;
                    this.start({resumeAt: position, autoplay: playing}).catch(err => this.emit('error', {message: err.message}));
                }
            }
        }

        teardownMedia() {
            if (this.hls) { this.hls.destroy(); this.hls = null; }
            this.video.pause();
            this.video.removeAttribute('src');
            this.video.load();
            this.mode = null;
        }

        destroy(keepalive = false) {
            if (!this.active) return;
            this.active = false;
            clearInterval(this.heartbeatTimer);
            const position = this.video.currentTime || 0;
            const duration = Number.isFinite(this.video.duration) ? this.video.duration : 0;
            if (this.hls) this.hls.destroy();
            if (this.session) {
                fetch(`/api/play/s/${this.session}/end`, {method: 'POST', keepalive,
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf()},
                    body: JSON.stringify({position, duration})}).catch(() => {});
            }
        }
    }

    /** Quality / dubbing menu over the video (player page and rooms). */
    function mountTracks(container, stream) {
        const box = document.createElement('div');
        box.className = 'tracks-menu';
        box.hidden = true;
        box.innerHTML = '<button type="button" class="tracks-toggle" aria-haspopup="true" aria-expanded="false">'
            + '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h4M12 17h8M14 4v6M8 14v6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
            + '<span class="tracks-label"></span></button><div class="tracks-panel" role="menu" hidden></div>';
        container.append(box);
        const toggle = box.querySelector('.tracks-toggle');
        const panel = box.querySelector('.tracks-panel');
        const close = () => { panel.hidden = true; toggle.setAttribute('aria-expanded', 'false'); };

        function option(text, selected, hint, onPick) {
            const button = document.createElement('button');
            button.type = 'button';
            button.setAttribute('role', 'menuitemradio');
            button.setAttribute('aria-checked', String(selected));
            button.textContent = text;
            if (hint) { const small = document.createElement('small'); small.textContent = hint; button.append(small); }
            button.addEventListener('click', () => { close(); if (!selected) onPick(); });
            return button;
        }
        function group(title, items) {
            const section = document.createElement('div');
            section.className = 'tracks-group';
            const heading = document.createElement('span');
            heading.textContent = title;
            section.append(heading, ...items);
            return section;
        }
        function render() {
            const state = stream.state;
            if (!state) return;
            const qualities = state.qualities || [];
            const tracks = state.audio_tracks || [];
            box.hidden = qualities.length < 2 && tracks.length < 2;
            const current = stream.current;
            const quality = qualities.find(q => q.id === current.quality) || qualities.find(q => q.id === 'source');
            const track = tracks.find(t => t.index === current.audio);
            box.querySelector('.tracks-label').textContent = [quality ? (quality.id === 'source' ? 'Оригинал' : quality.label) : '',
                tracks.length > 1 && track ? track.label.split(' · ')[0] : ''].filter(Boolean).join(' · ');
            const groups = [];
            if (qualities.length > 1) {
                groups.push(group('Качество', qualities.map(q => option(q.label, q.id === current.quality,
                    q.id === 'source' ? 'без перекодирования' : (q.height >= 1080 ? 'нужен быстрый процессор' : ''),
                    () => stream.setVariant(q.id === 'source' ? {quality: 'source', audio: state.default_audio ?? 0} : {quality: q.id})))));
            }
            if (tracks.length > 1) {
                const firstHls = qualities.find(q => q.id !== 'source')?.id;
                groups.push(group('Озвучка', tracks.map(t => option(t.label, t.index === current.audio, '',
                    () => stream.setVariant(current.quality === 'source' && t.index !== (state.default_audio ?? 0)
                        ? {audio: t.index, quality: firstHls} : {audio: t.index})))));
            }
            panel.replaceChildren(...groups);
        }
        toggle.addEventListener('click', event => {
            event.stopPropagation();
            render();
            panel.hidden = !panel.hidden;
            toggle.setAttribute('aria-expanded', String(!panel.hidden));
        });
        document.addEventListener('click', event => { if (!box.contains(event.target)) close(); });
        document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
        stream.addEventListener('attached', render);
        return {render};
    }

    window.KinoStream = KinoStream;
    window.KinoTracks = {mount: mountTracks};
    window.KinoFormat = {rate: formatRate, time: formatTime};
})();
