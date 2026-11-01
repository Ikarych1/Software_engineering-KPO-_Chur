(() => {
    'use strict';
    const root = document.querySelector('[data-tmdb-import]');
    if (!root) return;
    const el = name => document.getElementById(`tmdb-${name}`);
    const form = el('form'), submit = el('submit'), error = el('error');
    const csrf = document.querySelector('meta[name=csrf-token]').content;
    const labels = {added:'Добавлен', updated:'Обновлён', ambiguous:'Нужно уточнение', not_found:'Не найден', mismatch:'Не совпали данные', conflict:'Конфликт карточек', incomplete:'Недостаточно данных'};
    const states = {running:'Импорт выполняется',done:'Готово',cancelled:'Импорт остановлен',interrupted:'Можно продолжить импорт',error:'Импорт приостановлен'};
    let token, timer, page = 1, active = true, polling = false;
    const jobUrl = () => root.dataset.jobUrl.replace('JOB', encodeURIComponent(token));
    function fail(message) { error.textContent = message; error.hidden = false; }
    async function request(url, options = {}) {
        const response = await fetch(url, {...options,cache:'no-store',headers:{...options.headers,'X-CSRF-Token':csrf}});
        const data = await response.json().catch(() => null);
        if (!response.ok) {
            const err = new Error(data?.error || 'Не удалось получить ответ сервера. Обновите страницу.');
            err.status = response.status;
            throw err;
        }
        return data;
    }
    function render(results) {
        el('results').replaceChildren();
        results.forEach(row => {
            const item = document.createElement('article'); item.className = 'imdb-result';
            const title = document.createElement('strong'); title.textContent = `${row.line}. ${row.title || row.input}`;
            const status = document.createElement('span'); status.className = 'badge'; status.textContent = labels[row.status] || row.status;
            const message = document.createElement('p'); message.className = 'muted small-text'; message.textContent = row.message;
            item.append(title,status,message);
            if (row.candidates?.length) {
                const choices = document.createElement('p'); choices.className = 'muted small-text';
                choices.textContent = row.candidates.map(c => `TMDB ${c.tmdb_id} · ${c.kind}`).join('; '); item.append(choices);
            }
            if (Number.isInteger(row.movie_id) && row.movie_id > 0) {
                const link = document.createElement('a'); link.className = 'text-link'; link.href = `/admin/films/${row.movie_id}`; link.textContent = 'Открыть карточку →'; item.append(link);
            }
            el('results').append(item);
        });
    }
    async function poll() {
        clearTimeout(timer);
        if (!token || !active || polling) return;
        polling = true;
        try {
            const job = await request(`${jobUrl()}?page=${page}`);
            error.hidden = true;
            page = job.page;
            el('status').hidden = false;
            el('status-heading').textContent = states[job.state] || job.state;
            el('message').textContent = job.message;
            el('progress').value = job.completed / job.total;
            const c = job.counts;
            el('detail').textContent = `Обработано ${job.completed.toLocaleString('ru-RU')} из ${job.total.toLocaleString('ru-RU')} · Добавлено ${c.added || 0} · Обновлено ${c.updated || 0} · Пропущено ${job.completed-(c.added||0)-(c.updated||0)}`;
            submit.disabled = job.state === 'running';
            el('cancel').hidden = job.state !== 'running';
            el('resume').hidden = job.state === 'running' || job.state === 'done';
            el('report').hidden = false; el('report').href = `${jobUrl()}/report.csv`;
            render(job.results);
            el('pagination').hidden = job.pages <= 1;
            el('page').textContent = `${page} / ${job.pages} · до 100 строк на странице`;
            el('prev').disabled = page <= 1; el('next').disabled = page >= job.pages;
            if (job.state === 'running') timer = setTimeout(poll, 1500);
        } catch (err) {
            fail(err.message);
            // Keep polling through a short server restart; the job itself is durable.
            if (![401,403,404].includes(err.status)) timer = setTimeout(poll, 5000);
        } finally { polling = false; }
    }
    el('settings').addEventListener('submit', async event => {
        event.preventDefault(); error.hidden = true;
        const button = event.currentTarget.querySelector('button'); button.disabled = true;
        try {
            const data = await request(el('settings').action,{method:'POST',body:new FormData(el('settings'))});
            el('credential').value = ''; el('connection').textContent = data.message;
        } catch (err) { fail(err.message); }
        finally { button.disabled = false; }
    });
    form.addEventListener('submit', async event => {
        event.preventDefault(); error.hidden = true; submit.disabled = true;
        try {
            const file = form.elements.list.files[0];
            if (!file || file.size > 10*1024*1024) throw new Error('Выберите CSV размером до 10 МБ.');
            const data = await request(root.dataset.startUrl,{method:'POST',body:new FormData(form)});
            clearTimeout(timer); token = data.job; page = 1;
            const url = new URL(location.href); url.searchParams.set('job',token); history.replaceState(null,'',url);
            await poll(); el('status').scrollIntoView({behavior:'smooth',block:'start'});
        } catch (err) { fail(err.message); submit.disabled = false; }
    });
    for (const action of ['cancel','resume']) el(action).addEventListener('click',async () => {
        el(action).disabled = true; error.hidden = true;
        try { await request(`${jobUrl()}/${action}`,{method:'POST'}); await poll(); }
        catch (err) { fail(err.message); }
        finally { el(action).disabled = false; }
    });
    el('prev').addEventListener('click',() => { page--; poll(); });
    el('next').addEventListener('click',() => { page++; poll(); });
    token = new URLSearchParams(location.search).get('job');
    if (token && /^[a-f0-9]{32}$/.test(token)) poll();
    window.addEventListener('pagehide',() => { active = false; clearTimeout(timer); });
    window.addEventListener('pageshow',() => { active = true; if (token) poll(); });
})();
