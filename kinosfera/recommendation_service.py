"""Application recommendation policy; mixdex only retrieves geometric neighbors."""
from dataclasses import dataclass, field

from recommendation_catalog import CatalogIndex, labels

POLICY = 'mixdex-v0'
MAX_RESULTS = 10


@dataclass
class UserContext:
    ratings: dict = field(default_factory=dict)
    progress: list = field(default_factory=list)
    watchlist: list = field(default_factory=list)
    completed: set = field(default_factory=set)
    declined: set = field(default_factory=set)
    preferences: dict = field(default_factory=dict)


@dataclass
class Result:
    items: list
    personalized: bool
    snapshot_id: str | None
    request_id: str | None = None

    @property
    def title(self):
        return 'В вашем вкусе' if self.personalized else 'Стоит посмотреть'

    @property
    def subtitle(self):
        if not self.items:
            return 'Подходящих рекомендаций пока нет. Вы можете выбрать фильм в каталоге.'
        return ('По вашим оценкам, просмотрам и предпочтениям' if self.personalized
                else 'Общая подборка по популярности и рейтингу — пока не хватает личных данных')


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a or b else 0.0


def preference_score(row, preferences):
    film, art = row['film'], row.get('art', {})
    values = []
    if preferences.get('genres'):
        values.append(jaccard(labels(film.genres), labels(preferences['genres'])))
    if preferences.get('content_types'):
        values.append(float(film.kind in preferences['content_types']))
    if preferences.get('languages'):
        values.append(float(art.get('original_language') in preferences['languages']))
    for key, column in (('actor_ids', 'cast'), ('director_ids', 'directors')):
        if preferences.get(key):
            wanted = {str(v) for v in preferences[key]}
            present = {str(v.get('person_id')) for v in art.get(column, []) if isinstance(v, dict)}
            values.append(float(bool(wanted & present)))
    return sum(values) / len(values) if values else 0.0


class RecommendationService:
    def __init__(self, catalog=None):
        self.catalog = catalog or CatalogIndex()

    def recommend(self, rows, stats, context=None, *, popularity=None, limit=10,
                  allow_rewatch=False, namespace='default'):
        if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
            raise ValueError('limit must be between 1 and 10')
        context = context or UserContext()
        by_id = {r['film'].id: r for r in rows if not r['film'].is_hidden}
        snapshot = self.catalog.get(list(by_id.values()), namespace)
        snapshot_id = snapshot.stats()['snapshot_id'] if snapshot else None
        blocked = set(context.declined)
        blocked.update(i for i, rating in context.ratings.items() if rating['score'] <= 4)
        if not allow_rewatch:
            blocked.update(context.completed)
            blocked.update(context.ratings)
        eligible = set(by_id) - blocked
        if not eligible:
            return Result([], False, snapshot_id)

        rated = sorted((i for i, r in context.ratings.items() if i in by_id and r['score'] >= 7),
                       key=lambda i: (-context.ratings[i]['score'], -context.ratings[i]['updated_at'], i))
        watched = [p['movie_id'] for p in sorted(context.progress, key=lambda p: (-p['updated_at'], p['movie_id']))
                   if p['movie_id'] in by_id and p['watched_seconds'] > 0
                   and (p['completed'] or p['duration'] > 0 and p['watched_seconds'] >= .8*p['duration'])
                   and context.ratings.get(p['movie_id'], {}).get('score', 10) >= 7]
        anchors = [i for i in dict.fromkeys(rated + watched + [i for i in context.watchlist if i in by_id])
                   if i not in context.declined and context.ratings.get(i, {}).get('score', 10) >= 7][:4]
        indexed = bool(snapshot and snapshot.stats()['informative_features'])
        anchors = anchors if indexed else []
        preferences = {k: v for k, v in context.preferences.items()
                       if k in ('genres', 'content_types', 'languages', 'actor_ids', 'director_ids') and v}
        p_scores = {i: preference_score(by_id[i], preferences) for i in eligible}
        popularity = popularity or {}
        max_pop = max((popularity.get(i, 0) for i in eligible), default=0)
        quality = {i: max(0., min(1., (stats.get(i, {}).get('avg') or by_id[i]['film'].rating or 0)/10))
                   for i in eligible}
        t_scores = {i: (.7*popularity.get(i, 0)/max_pop + .3*quality[i]) if max_pop else quality[i]
                    for i in eligible}
        popular = sorted(eligible, key=lambda i: (-t_scores[i], -quality[i], i))
        candidates = set(popular[:20])
        if preferences:
            candidates.update(i for i in sorted(eligible, key=lambda i: (-p_scores[i], i))[:20] if p_scores[i] > 0)
        for anchor in anchors:
            candidates.update(i for i, _ in snapshot.search_by_id(anchor, k=40, allowed_ids=eligible)['items'])
        if allow_rewatch:
            candidates.update(set(anchors) & eligible)

        closeness, nearest = {}, {}
        for anchor in anchors:
            if anchor in candidates:
                closeness[anchor], nearest[anchor] = 1., anchor
            # Score every candidate with the same metric, including popularity-only candidates.
            for i, distance in snapshot.search_by_id(anchor, k=max(1, len(candidates)), allowed_ids=candidates)['items']:
                score = 1 / (1 + distance)
                if score > closeness.get(i, -1):
                    closeness[i], nearest[i] = score, anchor
        personal_candidates = bool(closeness or any(p_scores.values()))
        active_c = bool(anchors and closeness)
        active_p = bool(preferences and any(p_scores.values()))
        denominator = .6*active_c + .25*active_p + .15
        ranked = []
        for i in candidates:
            c, p, t = closeness.get(i, 0.), p_scores[i], t_scores[i]
            score = (.6*c*active_c + .25*p*active_p + .15*t)/denominator
            if active_c and nearest.get(i) == i:
                reason = 'Из ваших предпочтений — для повторного просмотра'
            elif active_c and i in nearest and .6*c >= .25*p:
                anchor = by_id[nearest[i]]['film']
                common_genres = set(labels(by_id[i]['film'].genres)) & set(labels(anchor.genres))
                reason = (f'Общие жанры с «{anchor.title}»' if common_genres
                          else f'По году и длительности близко к «{anchor.title}»')
            elif active_p and p > 0:
                reason = 'Совпадает с вашими предпочтениями'
            elif max_pop and popularity.get(i, 0):
                reason = 'Популярно за последние две недели'
            else:
                reason = 'По общему рейтингу'
            ranked.append(dict(id=i, score=score, reason=reason,
                               components=dict(similarity=c, preferences=p, popularity=t)))
        ranked.sort(key=lambda item: (-item['score'], item['id']))
        return Result(ranked[:limit], personal_candidates, snapshot_id)

    def similar(self, rows, movie_id, *, limit=10, namespace='default'):
        by_id = {r['film'].id: r for r in rows if not r['film'].is_hidden}
        if movie_id not in by_id:
            return []
        snapshot = self.catalog.get(list(by_id.values()), namespace)
        if not snapshot or not snapshot.stats()['informative_features']:
            return []
        return [by_id[i] for i, _ in snapshot.search_by_id(movie_id, k=limit, allowed_ids=by_id)['items']]
