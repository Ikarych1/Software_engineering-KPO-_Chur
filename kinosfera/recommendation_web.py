"""Flask integration; user identity always comes from the authenticated session."""
import os
from pathlib import Path

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, url_for

from accounts import login_required, store
from recommendation_catalog import CatalogIndex
from recommendation_service import RecommendationService
from recommendation_store import RecommendationStore

blueprint = Blueprint('recommendations', __name__)


def init_recommendations(app):
    directory = os.environ.get('CINEMA_RECOMMENDATION_CACHE') or Path(app.root_path) / '.local/recommendations'
    app.extensions['recommendations'] = RecommendationService(CatalogIndex(directory))
    app.register_blueprint(blueprint)


def warmup(app):
    """Prepare the expensive first aggregates and native snapshot before serving requests."""
    repository = app.extensions['store']
    repository.film_stats()
    app.extensions['recommendations'].catalog.get(repository.films(), namespace=str(repository.path.resolve()))


def recommend(rows, stats, *, surface='home', limit=10, allow_rewatch=False):
    repository = RecommendationStore(store())
    user_id = g.user.id if g.get('user') else None
    result = current_app.extensions['recommendations'].recommend(
        rows, stats, repository.context(user_id, rows), popularity=repository.popularity(), limit=limit,
        allow_rewatch=allow_rewatch, namespace=str(store().path.resolve()))
    repository.record(user_id, surface, result)
    return result


def similar(rows, movie_id, limit=10):
    return current_app.extensions['recommendations'].similar(
        rows, movie_id, limit=limit, namespace=str(store().path.resolve()))


def rail_context(result, rows, surface='home', allow_rewatch=False):
    by_id = {r['film'].id: r for r in rows}
    return dict(id='for-you', title=result.title, subtitle=result.subtitle,
                films=[by_id[item['id']]['film'] for item in result.items],
                request_id=result.request_id or '', surface=surface, label='Подборка для вас', allow_rewatch=allow_rewatch)


@blueprint.get('/api/recommendations')
def get_recommendations():
    surface = request.args.get('surface', 'home')
    if surface not in ('home', 'profile'):
        abort(400, 'Неизвестный раздел рекомендаций.')
    try:
        limit = int(request.args.get('limit', '10' if surface == 'home' else '8'))
        if not 1 <= limit <= 10:
            raise ValueError
    except ValueError:
        abort(400, 'Количество рекомендаций должно быть от 1 до 10.')
    if request.args.get('rewatch', '0') not in ('0', '1'):
        abort(400, 'Некорректный режим повторного просмотра.')
    rows = store().films()
    result = recommend(rows, store().film_stats(), surface=surface, limit=limit,
                       allow_rewatch=request.args.get('rewatch') == '1')
    by_id = {r['film'].id: r for r in rows}
    payload = [dict(id=item['id'], title=by_id[item['id']]['film'].title,
                    url=url_for('movie_detail', movie_id=item['id']), reason=item['reason'],
                    score=item['score'], available=bool(by_id[item['id']]['torrent'])) for item in result.items]
    response = jsonify(items=payload, personalized=result.personalized, request_id=result.request_id,
                       incomplete=len(payload) < limit,
                       html=render_template('recommendation_fragment.html',
                                            rec=rail_context(result, rows, surface, request.args.get('rewatch') == '1')))
    response.headers['Cache-Control'] = 'no-store'
    return response


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, 'Ожидается JSON-объект.')
    return data


@blueprint.post('/api/recommendations/<request_id>/feedback')
@login_required
def feedback(request_id):
    data = body()
    ids, action = data.get('movie_ids'), data.get('action', 'shown')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
        abort(400, 'Некорректный список произведений.')
    if action not in ('shown', 'click') or action == 'click' and len(ids) != 1:
        abort(400, 'Некорректное действие.')
    try:
        RecommendationStore(store()).feedback(g.user.id, request_id, ids, clicked=action == 'click')
    except LookupError:
        abort(404)
    except ValueError:
        abort(400, 'Произведение отсутствует в этой подборке.')
    return jsonify(ok=True)


@blueprint.post('/api/recommendations/interest')
@login_required
def interest():
    data = body()
    movie_id, active = data.get('movie_id'), data.get('not_interested')
    if type(movie_id) is not int or movie_id <= 0 or type(active) is not bool:
        abort(400, 'Некорректный выбор произведения.')
    try:
        RecommendationStore(store()).decline(g.user.id, movie_id, active)
    except LookupError:
        abort(404)
    return jsonify(ok=True)

