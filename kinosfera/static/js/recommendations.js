/* Refresh recommendation fragments and confirm visible cards, never just generated ones. */
(() => {
    let observer, controller, generation = 0;
    const shown = new Set();
    const current = () => document.querySelector('[data-recommendations]');
    const csrf = () => document.querySelector('meta[name=csrf-token]')?.content || '';

    async function post(url, data) {
        const response = await fetch(url, {method: 'POST', keepalive: true,
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf()},
            body: JSON.stringify(data)});
        if (!response.ok) throw new Error('Feedback failed');
    }

    async function feedback(section, ids, action) {
        const request = section.dataset.recommendationRequest;
        if (!request) return;
        await post(`/api/recommendations/${encodeURIComponent(request)}/feedback`, {movie_ids: ids, action});
    }

    function observe(section) {
        observer?.disconnect();
        if (!section || !window.IntersectionObserver || !section.dataset.recommendationRequest) return;
        observer = new IntersectionObserver(async entries => {
            if (document.hidden || !section.isConnected) return;
            const cards = entries.filter(e => e.isIntersecting && e.intersectionRatio >= .5)
                .map(e => e.target).filter(card => !shown.has(`${section.dataset.recommendationRequest}:${card.dataset.movieId}`));
            if (!cards.length) return;
            const keys = cards.map(card => `${section.dataset.recommendationRequest}:${card.dataset.movieId}`);
            keys.forEach(key => shown.add(key));
            try { await feedback(section, cards.map(card => Number(card.dataset.movieId)), 'shown'); }
            catch (_) { keys.forEach(key => shown.delete(key)); }
        }, {threshold: [.5]});
        section.querySelectorAll('[data-movie-id]').forEach(card => observer.observe(card));
    }

    async function refresh() {
        const section = current();
        if (!section || document.hidden) return;
        const token = ++generation;
        controller?.abort();
        controller = new AbortController();
        try {
            const rewatch = section.querySelector('[data-recommendations-rewatch]')?.checked ? '1' : '0';
            const response = await fetch(`/api/recommendations?surface=${encodeURIComponent(section.dataset.recommendations)}&rewatch=${rewatch}`,
                {signal: controller.signal, cache: 'no-store'});
            if (!response.ok) throw new Error('Refresh failed');
            const data = await response.json();
            if (token !== generation || !section.isConnected) return;
            const template = document.createElement('template');
            template.innerHTML = data.html;
            const next = template.content.querySelector('[data-recommendations]');
            if (!next) throw new Error('Missing recommendation fragment');
            section.replaceWith(next);
            shown.clear();
            observe(next);
            window.dispatchEvent(new Event('kinosfera:recommendations-updated'));
        } catch (error) {
            if (error.name !== 'AbortError' && section.isConnected)
                section.querySelector('[data-recommendations-status]').textContent = 'Не удалось обновить подборку. Попробуем ещё раз.';
        }
    }

    document.addEventListener('click', async event => {
        const button = event.target.closest('[data-recommendation-dismiss]');
        if (button) {
            const section = button.closest('[data-recommendations]');
            button.disabled = true;
            try {
                await post('/api/recommendations/interest', {movie_id: Number(button.dataset.recommendationDismiss), not_interested: true});
                await refresh();
            } catch (_) {
                button.disabled = false;
                section.querySelector('[data-recommendations-status]').textContent = 'Не удалось сохранить выбор. Попробуйте ещё раз.';
            }
            return;
        }
        const link = event.target.closest('[data-recommendations] .movie-card a[href]');
        if (link) {
            const section = link.closest('[data-recommendations]');
            const id = Number(link.closest('[data-movie-id]').dataset.movieId);
            feedback(section, [id], 'click').catch(() => {});
        }
    });
    window.addEventListener('kinosfera:state-saved', refresh);
    document.addEventListener('change', event => { if (event.target.matches('[data-recommendations-rewatch]')) refresh(); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    setInterval(refresh, 45000);
    observe(current());
})();
