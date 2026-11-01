"""Recommendations and personal statistics computed from the user's own data."""
from collections import Counter


# Compatibility entry points share the application policy used by the web layer.
from recommendation_service import RecommendationService, UserContext

_LEGACY = RecommendationService()


def recommend(rows, stats, state=None, progress=(), limit=10):
    state = state or {}
    ratings = {int(i): dict(score=score, updated_at=0.) for i, score in state.get('ratings', {}).items()}
    completed = {p['movie_id'] for p in progress if p['completed']}
    context = UserContext(ratings=ratings, progress=list(progress), watchlist=state.get('watchlist', []), completed=completed)
    by_id = {r['film'].id: r for r in rows}
    result = _LEGACY.recommend(rows, stats, context, limit=min(limit, 10))
    return [(by_id[item['id']], item['reason']) for item in result.items]


def similar(rows, film, limit=10):
    return _LEGACY.similar(rows, film.id, limit=limit)


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

