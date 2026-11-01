(() => {
  const root = document.querySelector('[data-preferences]');
  if (!root) return;
  const form = root.querySelector('form'), grid = document.getElementById('taste-movies');
  const genres = [...form.querySelectorAll('[name="genres"]')];
  const chosen = new Map();
  const next = document.getElementById('taste-next'), back = document.getElementById('taste-back');
  const submit = document.getElementById('taste-submit'), search = document.getElementById('taste-search');
  const more = document.getElementById('taste-more'), status = document.getElementById('taste-status');
  const picked = document.getElementById('taste-picked'), inputs = document.getElementById('taste-selected-inputs');
  const query = document.getElementById('taste-query');
  let page = 1, revision = 0, timer;
  function itemFrom(label) {
    return {id: Number(label.dataset.movieId), title: label.querySelector('strong').textContent,
      image: label.querySelector('img').getAttribute('src')};
  }
  grid.querySelectorAll('input:checked').forEach(i => chosen.set(Number(i.value), itemFrom(i.closest('label'))));
  function update() {
    const count = genres.filter(i => i.checked).length;
    document.getElementById('genre-count').textContent = `${count} / 3`;
    document.getElementById('movie-count').textContent = `${chosen.size} / 3`;
    genres.forEach(i => i.disabled = count >= 3 && !i.checked);
    next.disabled = count !== 3;
    submit.disabled = count !== 3 || chosen.size !== 3;
    grid.querySelectorAll('[name="movie_ids"]').forEach(i => {
      i.checked = chosen.has(Number(i.value)); i.disabled = !i.checked && chosen.size >= 3;
      i.removeAttribute('name'); i.dataset.choice = 'true';
    });
    grid.querySelectorAll('[data-choice]').forEach(i => {
      i.checked = chosen.has(Number(i.value)); i.disabled = !i.checked && chosen.size >= 3;
    });
    picked.replaceChildren(); inputs.replaceChildren();
    chosen.forEach(item => {
      const button = document.createElement('button'); button.type = 'button';
      const img = document.createElement('img'); img.src = item.image; img.alt = '';
      button.append(img, document.createTextNode(`${item.title} ×`));
      button.setAttribute('aria-label', `Убрать ${item.title}`);
      button.onclick = () => {chosen.delete(item.id); update();}; picked.append(button);
      const input = document.createElement('input'); input.type = 'hidden'; input.name = 'movie_ids'; input.value = item.id;
      inputs.append(input);
    });
    picked.hidden = chosen.size === 0;
  }
  function step(movies) {
    root.querySelector('[data-step="genres"]').hidden = movies;
    root.querySelector('[data-step="movies"]').hidden = !movies;
  }
  genres.forEach(i => i.addEventListener('change', update));
  grid.addEventListener('change', e => {
    const i = e.target; if (i.type !== 'checkbox') return;
    const item = itemFrom(i.closest('label'));
    if (i.checked && chosen.size < 3) chosen.set(item.id, item); else chosen.delete(item.id);
    update();
  });
  async function load(append = false) {
    const request = ++revision;
    const wantedPage = append ? page + 1 : 1;
    status.textContent = 'Ищем кино…'; more.disabled = true;
    try {
      const response = await fetch(`/auth/preferences/films?${new URLSearchParams({q:query.value,page:wantedPage})}`, {headers:{'Accept':'application/json'}});
      if (!response.ok || response.redirected) throw new Error();
      const data = await response.json(); if (request !== revision) return;
      if (!append) grid.replaceChildren();
      data.items.forEach(item => {
        const label = document.createElement('label'); label.className = 'taste-movie'; label.dataset.movieId = item.id;
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = item.id; checkbox.dataset.choice = 'true';
        const poster = document.createElement('span'); poster.className = 'taste-poster';
        const img = document.createElement('img'); img.src = item.image; img.alt = ''; img.loading = 'lazy';
        const check = document.createElement('span'); check.className = 'taste-check'; check.textContent = '✓'; check.setAttribute('aria-hidden','true');
        poster.append(img,check);
        const title = document.createElement('strong'); title.textContent = item.title;
        const meta = document.createElement('small'); meta.textContent = `${item.year} · ${item.genres.slice(0,2).join(', ')}`;
        label.append(checkbox,poster,title,meta); grid.append(label);
      });
      page = wantedPage; more.hidden = !data.has_more;
      status.textContent = grid.children.length ? '' : 'Ничего не найдено. Попробуйте другое название.';
      update();
    } catch (e) {if (request === revision) status.textContent = 'Не удалось загрузить фильмы. Попробуйте ещё раз.';}
    finally {if (request === revision) more.disabled = false;}
  }
  next.hidden = back.hidden = search.hidden = false;
  next.onclick = () => {step(true); query.focus();}; back.onclick = () => step(false);
  search.onclick = () => {clearTimeout(timer); load();}; more.onclick = () => load(true);
  query.addEventListener('input', () => {clearTimeout(timer); timer = setTimeout(() => load(), 300);});
  query.addEventListener('keydown', e => {if(e.key === 'Enter'){e.preventDefault();clearTimeout(timer);load();}});
  form.addEventListener('submit', e => {update(); if (submit.disabled) e.preventDefault();});
  update(); step(genres.filter(i => i.checked).length === 3);
  more.hidden = false;
})();
