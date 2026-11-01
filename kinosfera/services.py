"""Recommendations and personal statistics computed from the user's own data."""
from collections import Counter
import math


def genre_weights(rows_by_id, state, progress):
    weights = Counter()
    for movie_id, score in state.get('ratings', {}).items():
        row = rows_by_id.get(int(movie_id))
        if row:
            for genre in row['film'].genres:
                weights[genre] += (int(score) - 5.5) / 2
    for movie_id in state.get('watched', []):
        row = rows_by_id.get(movie_id)
        if row:
            for genre in row['film'].genres:
                weights[genre] += 0.6
    for movie_id in state.get('watchlist', []):
        row = rows_by_id.get(movie_id)
        if row:
            for genre in row['film'].genres:
                weights[genre] += 0.8
    for item in progress:
        row = rows_by_id.get(item['movie_id'])
        if row:
            for genre in row['film'].genres:
                weights[genre] += 0.5 + 0.5 * min(1.0, item['position'] / max(1.0, item['duration']))
    return weights


def recommend(rows, stats, state=None, progress=(), limit=12):
    """Rank unseen films: genre affinity, community rating, popularity, availability."""
    state = state or {}
    rows_by_id = {row['film'].id: row for row in rows}
    weights = genre_weights(rows_by_id, state, progress)
    seen = set(state.get('watched', [])) | {int(k) for k in state.get('ratings', {})} | \
        {p['movie_id'] for p in progress if p['completed'] or p['position'] > 120}
    ranked = []
    for row in rows:
        film = row['film']
        if film.id in seen:
            continue
        info = stats.get(film.id, {})
        affinity = sum(weights[g] for g in film.genres) / math.sqrt(len(film.genres) or 1)
        community = (info.get('avg') or film.rating) / 10
        score = affinity + 2.0 * community + 0.25 * math.log1p(info.get('views', 0)) + (0.4 if row['torrent'] else 0)
        best = max(film.genres, key=lambda g: weights[g]) if film.genres else ''
        if weights and weights[best] > 0.5:
            reason = f'Вы любите жанр «{best.lower()}»'
        elif info.get('avg') and info.get('votes', 0) >= 2:
            reason = f'Зрители ставят {info["avg"]}'
        else:
            reason = 'Высокий рейтинг'
        ranked.append((score, film.id, row, reason))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [(row, reason) for _, _, row, reason in ranked[:limit]]


def similar(rows, film, limit=10):
    target = set(film.genres)
    scored = []
    for row in rows:
        other = row['film']
        if other.id == film.id:
            continue
        overlap = len(target & set(other.genres))
        if overlap:
            scored.append((overlap / len(target | set(other.genres)), other.rating, row))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [row for _, _, row in scored[:limit]]


def taste_match(mine, theirs):
    """Similarity of two users' ratings in percent (None if too little overlap)."""
    common = set(mine) & set(theirs)
    if len(common) < 2:
        return None
    diff = sum(abs(mine[m] - theirs[m]) for m in common) / len(common)
    return max(0, round(100 - diff * 11))


def profile_stats(rows_by_id, state, progress, reviews):
    ratings = {int(k): v for k, v in state.get('ratings', {}).items()}
    completed = {p['movie_id'] for p in progress if p['completed']} | set(state.get('watched', []))
    seconds = sum(p['watched_seconds'] for p in progress)
    genres = Counter()
    for movie_id in completed | set(ratings):
        row = rows_by_id.get(movie_id)
        if row:
            for genre in row['film'].genres:
                genres[genre] += 1 + max(0, ratings.get(movie_id, 6) - 6) / 2
    return dict(watched=len(completed), ratings=len(ratings),
                avg_score=round(sum(ratings.values()) / len(ratings), 1) if ratings else None,
                hours=round(seconds / 3600, 1), minutes=int(seconds // 60), reviews=len(reviews), watchlist=len(state.get('watchlist', [])),
                genres=[g for g, _ in genres.most_common(5)])
