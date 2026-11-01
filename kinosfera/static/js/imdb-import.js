(() => {
    'use strict';
    const root = document.querySelector('[data-imdb-import]');
    if (!root) return;
    const form = document.getElementById('imdb-form');
    const button = document.getElementById('imdb-submit');
    const panel = document.getElementById('imdb-status');
    const error = document.getElementById('imdb-error');
    const cancel = document.getElementById('imdb-cancel');
    const csrf = document.querySelector('meta[name=csrf-token]').content;
    const labels = {added:'Добавлен', updated:'Обновлён', ambiguous:'Нужно уточнение', not_found:'Не найден', mismatch:'Другой год', conflict:'Дубликаты в каталоге', incomplete:'Недостаточно данных'};
    let token, timer, active = true;
    const jobUrl = () => root.dataset.jobUrl.replace('JOB', encodeURIComponent(token));
    function fail(message) { error.textContent = message; error.hidden = false; }
    async function request(url, options = {}) {
        const response = await fetch(url, {...options, cache:'no-store', headers:{...options.headers,'X-CSRF-Token':csrf}});
        const data = await response.json().catch(() => null);
        if (!response.ok) throw new Error(data?.error || 'Не удалось получить ответ сервера. Обновите страницу.');
        return data;
    }
    function resultList(results) {
        const parent = document.getElementById('imdb-results');
        parent.replaceChildren();
        results.forEach(row => {
            const item = document.createElement('article'); item.className = 'imdb-result';
            const title = document.createElement('strong'); title.textContent = `${row.line}. ${row.title || row.input}`;
            const status = document.createElement('span'); status.className = 'badge'; status.textContent = labels[row.status] || row.status;
            const message = document.createElement('p'); message.className = 'muted small-text'; message.textContent = row.message;
            item.append(title,status,message);
            if (row.candidates) {
                const choices = document.createElement('p'); choices.className = 'muted small-text';
                choices.textContent = row.candidates.map(c => `${c.title} (${c.year || '—'}) — ${c.imdb_id}`).join('; ');
                item.append(choices);
            }
            if (Number.isInteger(row.movie_id) && row.movie_id > 0) {
                const link = document.createElement('a'); link.className = 'text-link'; link.href = `/admin/films/${row.movie_id}`; link.textContent = 'Открыть карточку →'; item.append(link);
            }
            parent.append(item);
        });
    }
    async function poll() {
        clearTimeout(timer);
        if (!token || !active) return;
        try {
            const job = await request(jobUrl());
            panel.hidden = false;
            document.getElementById('imdb-status-heading').textContent = job.state === 'running' ? 'Импорт выполняется' : job.state === 'done' ? 'Готово' : job.state === 'cancelled' ? 'Импорт отменён' : 'Не удалось завершить импорт';
            document.getElementById('imdb-message').textContent = job.message;
            const progress = document.getElementById('imdb-progress');
            progress.hidden = job.state !== 'running';
            if (job.stage === 'download' && job.total > 0) progress.value = Math.min(1, job.downloaded/job.total);
            else progress.removeAttribute('value');
            document.getElementById('imdb-detail').textContent = job.state !== 'running' ? '' :
                job.stage === 'download' ? `${(job.downloaded/1048576).toFixed(1)} МБ${job.total ? ` из ${(job.total/1048576).toFixed(1)} МБ` : ''}` :
                job.stage === 'scan' && job.scanned ? `Прочитано строк: ${Number(job.scanned).toLocaleString('ru-RU')}` : '';
            cancel.hidden = job.state !== 'running'; button.disabled = job.state === 'running';
            if (job.state === 'running') timer = setTimeout(poll, 1500);
            else {
                resultList(job.results || []);
                const report = document.getElementById('imdb-report'); report.hidden = !job.results?.length; report.href = `${jobUrl()}/report.csv`;
            }
        } catch (err) { fail(err.message); button.disabled = false; cancel.hidden = true; }
    }
    form.addEventListener('submit', async event => {
        event.preventDefault(); error.hidden = true; button.disabled = true;
        clearTimeout(timer);
        try {
            const file = form.elements.list.files[0];
            if (!file || file.size > 512*1024) throw new Error('Выберите CSV размером до 512 КБ.');
            const data = await request(root.dataset.startUrl, {method:'POST', body:new FormData(form)});
            token = data.job;
            const url = new URL(location.href); url.searchParams.set('job',token); history.replaceState(null,'',url);
            document.getElementById('imdb-results').replaceChildren();
            document.getElementById('imdb-report').hidden = true;
            await poll();
            panel.scrollIntoView({behavior:'smooth',block:'start'});
        } catch (err) { fail(err.message); button.disabled = false; }
    });
    cancel.addEventListener('click', async () => {
        cancel.disabled = true;
        try { await request(`${jobUrl()}/cancel`, {method:'POST'}); await poll(); }
        catch (err) { fail(err.message); }
        finally { cancel.disabled = false; }
    });
    token = new URLSearchParams(location.search).get('job');
    if (token && /^[a-f0-9]{32}$/.test(token)) poll();
    window.addEventListener('pagehide', () => { active = false; clearTimeout(timer); });
})();
