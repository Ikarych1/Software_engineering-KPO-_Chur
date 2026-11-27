/* Admin: live engine dashboard, source health check, image previews. */
(() => {
    'use strict';
    const csrf = document.querySelector('meta[name=csrf-token]')?.content || '';
    const rate = b => !b ? '0 КБ/с' : b >= 1048576 ? `${(b / 1048576).toFixed(1)} МБ/с` : `${Math.max(1, Math.round(b / 1024))} КБ/с`;
    const size = b => b >= 1073741824 ? `${(b / 1073741824).toFixed(1)} ГБ` : `${Math.round((b || 0) / 1048576)} МБ`;
    const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = String(text); return n; };

    function table(container, headers, rows) {
        if (!container) return;
        if (!rows.length) { container.replaceChildren(el('p', 'muted small-text', container.dataset.empty || 'Пусто')); return; }
        const t = el('table');
        const head = el('tr');
        headers.forEach(h => head.append(el('th', '', h)));
        t.append(head);
        rows.forEach(cells => {
            const tr = el('tr');
            cells.forEach(cell => { const td = el('td'); cell instanceof Node ? td.append(cell) : td.textContent = String(cell); tr.append(td); });
            t.append(tr);
        });
        container.replaceChildren(t);
    }
    function postButton(action, key, label) {
        const form = el('form');
        form.method = 'post';
        form.action = action;
        const token = el('input'); token.type = 'hidden'; token.name = 'csrf_token'; token.value = csrf;
        const input = el('input'); input.type = 'hidden'; input.name = 'key'; input.value = key;
        const button = el('button', 'btn btn-secondary btn-small', label);
        form.append(token, input, button);
        return form;
    }

    const live = document.querySelector('[data-engine-live]');
    if (live) {
        const history = [];
        const canvas = live.querySelector('[data-sparkline]');
        const full = live.hasAttribute('data-engine-full');
        const set = (name, value) => live.querySelectorAll(`[data-live="${name}"]`).forEach(n => { n.textContent = value; });
        function draw() {
            if (!canvas) return;
            const ctx = canvas.getContext('2d');
            const w = canvas.width, h = canvas.height;
            ctx.clearRect(0, 0, w, h);
            const max = Math.max(1048576, ...history);
            ctx.strokeStyle = '#ff7948';
            ctx.lineWidth = 2;
            ctx.fillStyle = 'rgba(255,121,72,.12)';
            ctx.beginPath();
            history.forEach((v, i) => {
                const x = (i / 89) * w, y = h - 4 - (v / max) * (h - 12);
                i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
            });
            ctx.stroke();
            if (history.length > 1) {
                ctx.lineTo(((history.length - 1) / 89) * w, h);
                ctx.lineTo(0, h);
                ctx.closePath();
                ctx.fill();
            }
        }
        async function poll() {
            try {
                const response = await fetch('/admin/api/engine', {cache: 'no-store'});
                const data = await response.json();
                set('down', rate(data.totals?.download_rate));
                set('up', rate(data.totals?.upload_rate));
                set('peers', data.totals?.peers ?? 0);
                set('sessions', (data.sessions || []).length);
                set('hls', data.hls_jobs ?? 0);
                set('cache', data.cache ? `${size(data.cache.used)} / ${size(data.cache.limit)}` : '—');
                set('state', !data.available ? 'libtorrent не установлен' : data.running ? `работает · порт ${data.port}` : 'запустится при первом просмотре');
                history.push(data.totals?.download_rate || 0);
                if (history.length > 90) history.shift();
                draw();
                if (full) {
                    table(live.querySelector('[data-live-torrents]'), ['Файл', 'Загрузка', 'Отдача', 'Пиры', 'Скачано', 'Зрители', ''],
                        (data.torrents || []).map(t => [t.name, rate(t.download_rate), rate(t.upload_rate), `${t.peers} (${t.seeds} сидов)`,
                            `${Math.round(t.progress * 100)}%`, t.viewers, postButton('/admin/engine/stop', t.key, 'Остановить')]));
                    table(live.querySelector('[data-live-sessions]'), ['Зритель', 'Фильм', 'Позиция', 'Состояние', 'Режим', 'Комната'],
                        (data.sessions || []).map(s => [s.user, s.movie_id, `${Math.floor(s.position / 60)} мин`, s.buffering ? 'буферизация' : s.playing ? 'смотрит' : 'пауза', s.mode === 'direct' ? 'оригинал' : s.mode === 'hls' ? 'HLS' : '—', s.room || '—']));
                    table(document.querySelector('[data-live-cache]'), ['Файл', 'На диске', 'Последний просмотр', ''],
                        (data.cache?.entries || []).map(e => [e.name, size(e.bytes), e.last_used ? new Date(e.last_used * 1000).toLocaleString('ru-RU') : '—',
                            e.active ? el('span', 'badge', 'используется') : postButton('/admin/engine/evict', e.key, 'Удалить')]));
                }
            } catch (_) { set('state', 'нет связи'); }
        }
        live.querySelectorAll('[data-live-torrents],[data-live-sessions]').forEach(n => { n.dataset.empty = n.dataset.liveTorrents !== undefined ? 'Нет активных раздач — они запускаются, когда кто-то смотрит фильм.' : 'Сейчас никто не смотрит.'; });
        const cacheBox = document.querySelector('[data-live-cache]');
        if (cacheBox) cacheBox.dataset.empty = 'Кэш пуст.';
        poll();
        setInterval(poll, 2000);
    }

    const size2 = b => b >= 1073741824 ? `${(b / 1073741824).toFixed(1)} ГБ` : `${Math.max(1, Math.round((b || 0) / 1048576))} МБ`;
    const plural = (n, one, few, many) => (n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? few : many);

    // ------------------------------------------------------------- torrent preview (main source / new season)
    document.querySelectorAll('[data-source-picker]').forEach(picker => {
        const fileInput = picker.querySelector('[data-source-file]');
        const magnetInput = picker.querySelector('[data-source-magnet]');
        const readButton = picker.querySelector('[data-source-read]');
        const tokenInput = picker.querySelector('[data-source-token]');
        const preview = picker.querySelector('[data-source-preview]');
        const submit = picker.querySelector('[data-season-submit]');
        const kind = () => picker.dataset.kind || document.querySelector('[data-kind-switch] input:checked')?.value || 'movie';
        let poll = null, started = 0, pending = null;
        picker.reading = () => pending;

        function render(job) {
            preview.hidden = false;
            preview.className = `source-preview is-${job.state}`;
            preview.replaceChildren();
            if (job.state === 'running') {
                const seconds = Math.round((Date.now() - started) / 1000);
                preview.append(el('span', 'spinner'), el('b', '', job.message || 'Читаем раздачу…'),
                    el('small', 'muted', `${seconds} с · обычно 5–60 секунд; если дольше — у раздачи мало участников`));
                return;
            }
            if (job.state === 'error') { preview.append(el('b', '', '✕ ' + (job.message || 'Не удалось прочитать раздачу.'))); return; }
            const meta = job.meta || {};
            preview.append(el('b', '', `✓ Раздача прочитана: ${meta.title || meta.name}`),
                el('span', '', `${meta.files} ${plural(meta.files, 'файл', 'файла', 'файлов')} · ${size2(meta.size)} · видео: ${meta.videos}`));
            if (kind() === 'series' && meta.episodes) {
                preview.append(el('span', 'source-found', meta.episodes.length
                    ? `Найдено серий: ${meta.episodes.length} — ${meta.seasons}` : 'Серии не найдены'));
                if (meta.episodes.length) {
                    const details = el('details');
                    details.append(el('summary', '', 'Список серий'));
                    const list = el('ol', 'source-episodes');
                    meta.episodes.slice(0, 60).forEach(ep => list.append(el('li', '', `S${ep.season} · E${ep.episode} — ${ep.name.split('/').pop()}`)));
                    details.append(list);
                    preview.append(details);
                }
            } else if (meta.selected) {
                preview.append(el('span', 'source-found', `В карточке будет: ${meta.selected.split('/').pop()}`));
            }
            (meta.warnings || []).forEach(text => preview.append(el('span', 'source-warning', '⚠ ' + text)));
            preview.append(el('small', 'muted', submit ? 'Нажмите «Добавить сезон», чтобы подключить раздачу.' : 'Сохраните карточку, чтобы подключить раздачу.'));
        }
        function finish(job) {
            clearTimeout(poll);
            tokenInput.value = job.state === 'done' ? job.token : '';
            if (submit) submit.disabled = job.state !== 'done';
            if (readButton) readButton.disabled = false;
            render(job);
            const done = pending;
            pending = null;
            done?.resolve(job);
        }
        async function follow(token) {
            try {
                const job = await (await fetch(`${picker.dataset.inspectUrl}/${token}`, {cache: 'no-store'})).json();
                if (job.state === 'running') { render(job); poll = setTimeout(() => follow(token), 1000); return; }
                finish(job);
            } catch (_) { poll = setTimeout(() => follow(token), 2000); }
        }
        async function inspect(body) {
            clearTimeout(poll);
            tokenInput.value = '';
            if (submit) submit.disabled = true;
            if (readButton) readButton.disabled = true;
            started = Date.now();
            let resolve;
            pending = new Promise(r => { resolve = r; });
            pending.resolve = resolve;
            render({state: 'running', message: body.has('magnet') ? 'Ищем участников раздачи и получаем список файлов…' : 'Читаем торрент-файл…'});
            body.append('csrf_token', csrf);
            body.append('kind', kind());
            try {
                const response = await fetch(picker.dataset.inspectUrl, {method: 'POST', body, headers: {'X-CSRF-Token': csrf, Accept: 'application/json'}});
                const job = await response.json();
                if (job.state === 'running') follow(job.token); else finish(job);
            } catch (_) { finish({state: 'error', message: 'Сервер недоступен. Обновите страницу.'}); }
        }
        fileInput?.addEventListener('change', () => {
            if (!fileInput.files?.length) return;
            const body = new FormData();
            body.append('torrent', fileInput.files[0]);
            inspect(body);
        });
        readButton?.addEventListener('click', () => {
            const value = magnetInput.value.trim();
            if (!value) { magnetInput.focus(); return; }
            const body = new FormData();
            body.append('magnet', value);
            inspect(body);
        });
        if (submit) fileInput?.addEventListener('change', () => { submit.disabled = !fileInput.files?.length; });
        magnetInput?.addEventListener('input', () => { tokenInput.value = ''; });
    });

    document.querySelectorAll('[data-confirm]').forEach(button => button.addEventListener('click', event => {
        if (!confirm(button.dataset.confirm)) event.preventDefault();
    }));

    // ------------------------------------------------------------- source check, step by step
    const health = document.querySelector('.source-health');
    if (health) {
        const text = health.querySelector('[data-health-text]');
        const button = health.querySelector('[data-health-check]');
        const steps = health.querySelector('[data-health-steps]');
        const episode = health.querySelector('[data-health-episode]');
        let timer = null, began = 0;
        const step = (name, state, detail) => {
            const item = steps.querySelector(`[data-step="${name}"]`);
            item.className = `is-${state}`;
            item.querySelector('span').textContent = detail;
        };
        const url = () => health.dataset.healthUrl + (episode ? `?ep=${episode.value}` : '');
        async function check() {
            const data = await (await fetch(url(), {cache: 'no-store'})).json();
            const seconds = Math.round((Date.now() - began) / 1000);
            if (data.error) { text.textContent = data.error; return true; }
            if (data.state === 'idle') { step('peers', 'active', `запускаем раздачу… ${seconds} с`); return false; }
            if (!data.peers) {
                step('peers', seconds > 45 ? 'warn' : 'active', seconds > 45
                    ? `участники не найдены за ${seconds} с — раздача может быть мёртвой или очень медленной`
                    : `ищем участников… ${seconds} с`);
            } else {
                step('peers', 'done', `${data.peers} ${plural(data.peers, 'участник', 'участника', 'участников')} (${data.seeds} с полной копией) · ${rate(data.download_rate)}`);
            }
            const need = Math.min(data.size || 0, 8 * 1048576);
            if (data.media || data.head >= need) step('head', 'done', `загружено ${size2(data.head)} — ${data.file}`);
            else step('head', data.peers ? 'active' : 'wait', data.peers ? `загружаем: ${Math.round((data.head || 0) / need * 100)}% из ${size2(need)}` : 'ждём участников');
            if (data.media) {
                const tracks = (data.media.audio_tracks || []).length;
                step('probe', 'done', `${data.media.video.height}p ${String(data.media.video.codec).toUpperCase()}` +
                    (tracks > 1 ? ` · ${tracks} ${plural(tracks, 'аудиодорожка', 'аудиодорожки', 'аудиодорожек')}` : '') +
                    ` → ${data.plan?.mode === 'direct' ? 'прямой поток без перекодирования' : 'перекодирование на лету'}`);
                text.textContent = 'Готово: видео можно смотреть.';
                return true;
            }
            if (data.probe === 'error') { step('probe', 'failed', data.probe_error || 'не удалось прочитать видео'); text.textContent = 'Проверка завершилась с ошибкой.'; return true; }
            step('probe', data.head >= need ? 'active' : 'wait', data.head >= need ? 'определяем качество и дорожки…' : 'после загрузки начала');
            return false;
        }
        async function run() {
            clearTimeout(timer);
            button.disabled = true;
            steps.hidden = false;
            ['peers', 'head', 'probe'].forEach(name => step(name, 'wait', 'ждём'));
            text.textContent = 'Проверяем раздачу — это может занять до пары минут.';
            began = Date.now();
            await fetch(health.dataset.warmUrl, {method: 'POST', headers: {'X-CSRF-Token': csrf, Accept: 'application/json', 'Content-Type': 'application/json'},
                body: JSON.stringify(episode ? {episode: Number(episode.value)} : {})}).catch(() => {});
            const tick = async () => {
                let done = false;
                try { done = await check(); } catch (_) { /* next tick */ }
                if (done || Date.now() - began > 240000) { button.disabled = false; if (!done) text.textContent = 'Раздача отвечает слишком медленно. Попробуйте позже.'; return; }
                timer = setTimeout(tick, 1500);
            };
            tick();
        }
        button.addEventListener('click', run);
        episode?.addEventListener('change', run);
        if (health.dataset.autoCheck) { health.scrollIntoView({block: 'center'}); run(); }
    }

    document.querySelectorAll('[data-preview-input]').forEach(input => input.addEventListener('change', () => {
        const file = input.files?.[0];
        const img = document.querySelector(`[data-preview="${input.dataset.previewInput}"]`);
        if (!file || !img) return;
        img.src = URL.createObjectURL(file);
        img.hidden = false;
    }));
    const heroEditor = document.querySelector('[data-hero-editor]');
    const clipButton = heroEditor?.querySelector('[data-clip-make]');
    if (clipButton) {
        const status = heroEditor.querySelector('[data-clip-status]');
        const preview = heroEditor.querySelector('[data-hero-preview]');
        const url = heroEditor.dataset.clipUrl;
        let timer = null;
        const show = job => {
            status.textContent = job.message || '';
            status.classList.toggle('form-error', job.state === 'error');
            clipButton.disabled = job.state === 'running';
            if (job.state === 'running') { timer = setTimeout(poll, 1500); return; }
            if (job.state === 'done' && job.url && preview) {
                preview.src = job.url;
                preview.hidden = false;
                heroEditor.querySelector('[data-hero-empty]')?.remove();
                preview.play().catch(() => {});
            }
        };
        async function poll() {
            try { show(await (await fetch(url, {headers: {Accept: 'application/json'}})).json()); }
            catch (_) { timer = setTimeout(poll, 3000); }
        }
        clipButton.addEventListener('click', async () => {
            clearTimeout(timer);
            const body = new FormData();
            body.append('csrf_token', csrf);
            body.append('start', heroEditor.querySelector('[data-clip-start]').value);
            body.append('length', heroEditor.querySelector('[data-clip-length]').value);
            const episode = heroEditor.querySelector('[data-clip-episode]');
            if (episode) body.append('episode', episode.value);
            clipButton.disabled = true;
            status.textContent = 'Запускаем…';
            const response = await fetch(url, {method: 'POST', body, headers: {'X-CSRF-Token': csrf, Accept: 'application/json'}});
            const job = await response.json().catch(() => ({state: 'error', message: 'Сервер недоступен.'}));
            show(response.ok ? job : {state: 'error', message: job.error || job.message});
        });
        poll();
    }
    const editor = document.querySelector('[data-film-editor]');
    // Film / series: show the matching source controls without reloading the form.
    editor?.querySelector('[data-kind-switch]')?.addEventListener('change', event => {
        const kind = event.target.value;
        editor.querySelectorAll('[data-kind-only]').forEach(node => { node.hidden = node.dataset.kindOnly !== kind; });
        const label = editor.querySelector('[data-duration-label]');
        if (label) label.textContent = kind === 'series' ? 'Длительность серии, мин.' : 'Длительность, мин.';
        const eyebrow = editor.closest('.admin-page')?.querySelector('.section-heading .eyebrow');
        if (eyebrow) eyebrow.textContent = kind === 'series' ? 'КАРТОЧКА СЕРИАЛА' : 'КАРТОЧКА ФИЛЬМА';
    });
    editor?.addEventListener('invalid', event => {
        const field = event.target;
        const label = field.labels?.[0]?.textContent.trim() || field.name;
        const error = document.getElementById('editor-error');
        error.hidden = false;
        error.textContent = field.name === 'description' ? 'Описание должно содержать от 10 до 6000 символов.' : label + ' — ' + field.validationMessage;
        field.setAttribute('aria-describedby', 'editor-error');
    }, true);
    editor?.addEventListener('submit', async event => {
        const picker = editor.querySelector('.source-picker[data-source-picker]');
        const magnet = picker?.querySelector('[data-source-magnet]');
        const token = picker?.querySelector('[data-source-token]');
        const action = event.submitter?.getAttribute('formaction');
        if (!action && magnet && magnet.value.trim() && magnet.value.trim() !== magnet.defaultValue.trim() && !token.value) {
            event.preventDefault();
            picker.querySelector('[data-source-read]').click();
            const job = await picker.reading();
            if (job?.state === 'done') editor.requestSubmit(event.submitter || undefined);
            return;
        }
        const button = editor.querySelector('[data-submit-label]');
        if (button) { button.textContent = button.dataset.submitLabel; button.disabled = true; }
    });
})();
