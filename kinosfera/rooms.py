"""Watch-party rooms: server-authoritative playback state pushed over Server-Sent Events.

The room keeps ``{playing, position, updated_at, rate}``. Clients estimate the server
clock offset, compute the expected position and correct drift (seek for big gaps,
playback-rate nudges for small ones). Optional "wait for everyone": if a member is
buffering the room pauses automatically and resumes when everybody is ready.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import secrets
import threading
import time

from flask import Blueprint, Response, abort, current_app, g, jsonify, redirect, render_template, request, stream_with_context, url_for

from accounts import login_required, require_csrf, store
import series

rooms_bp = Blueprint('rooms', __name__)
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
REACTIONS = ['❤️', '😂', '👏', '😮', '🔥', '😢', '🍿', '👍']
DEFAULT_SETTINGS = dict(control='host', wait_for_all=True, chat=True, reactions=True, locked=False)
COLORS = ['peach', 'blue', 'purple', 'green', 'gold']


@dataclass
class Presence:
    user_id: int
    name: str
    color: str
    connections: int = 0
    status: str = 'idle'            # idle | ready | buffering | playing | paused | error
    position: float = 0.0
    last_seen: float = field(default_factory=time.time)
    buffering_since: float | None = None


class LiveRoom:
    def __init__(self, row):
        self.code = row['code']
        self.movie_id = row['movie_id']
        self.host_id = row['host_id']
        self.title = row['title']
        self.settings = {**DEFAULT_SETTINGS, **(row.get('settings') or {})}
        self.state = {**dict(playing=False, position=0.0, updated_at=time.time(), rate=1.0, by=None), **(row.get('state') or {})}
        self.state['playing'] = False if not row.get('ended_at') else self.state.get('playing', False)
        self.ended_at = row.get('ended_at')
        self.members: dict[int, Presence] = {}
        self.events: deque = deque(maxlen=400)
        self.seq = 0
        self.auto_paused = False
        self.last_members_push = 0.0
        self.reaction_times: dict[int, deque] = {}

    def position_now(self, now=None):
        now = now or time.time()
        state = self.state
        if state['playing']:
            return state['position'] + (now - state['updated_at']) * state.get('rate', 1.0)
        return state['position']


class RoomHub:
    def __init__(self, app):
        self.app = app
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.rooms: dict[str, LiveRoom] = {}
        threading.Thread(target=self._ticker, daemon=True, name='rooms-ticker').start()

    @property
    def store(self):
        return self.app.extensions['store']

    # ------------------------------------------------------------------ lifecycle
    @staticmethod
    def _episode(film, file_id):
        """Series: a valid episode file id (the first one by default); films: None."""
        episodes = film.get('episodes') or []
        if film['film'].kind != 'series' or not episodes:
            return None
        found = series.find(episodes, file_id) if file_id is not None else None
        return (found or episodes[0])['file_id']

    def create(self, user, movie_id, title=None, episode=None):
        film = self.store.film(movie_id)
        if not film:
            raise LookupError('Фильм не найден.')
        episode = self._episode(film, episode)
        for _ in range(10):
            code = ''.join(secrets.choice(ALPHABET) for _ in range(6))
            if not self.store.room(code):
                break
        title = (title or f'Смотрим «{film["film"].title}»').strip()[:80]
        state = dict(playing=False, position=0.0, updated_at=time.time(), rate=1.0, by=None, episode=episode)
        self.store.create_room(code, movie_id, user.id, title, DEFAULT_SETTINGS, state)
        self.store.add_message(code, None, 'КиноСфера', 'system', f'{user.username} создал(а) комнату. Пригласите друзей по коду {code}.')
        playback = self.app.extensions.get('playback')
        if playback:
            threading.Thread(target=playback.prewarm, args=(movie_id, f'room-{code}', 1800),
                             kwargs=dict(file_id=episode), daemon=True).start()
        return code

    def get(self, code):
        with self._lock:
            room = self.rooms.get(code)
            if room is None:
                row = self.store.room(code)
                if row is None:
                    return None
                room = self.rooms[code] = LiveRoom(row)
            return room

    def _persist(self, room):
        self.store.save_room(room.code, state=room.state, settings=room.settings, movie_id=room.movie_id,
                             host_id=room.host_id, title=room.title)

    def publish(self, room, kind, data):
        with self._lock:
            room.seq += 1
            room.events.append((room.seq, kind, data))
            self._cond.notify_all()

    def message(self, room, user, kind, text):
        name = user.username if user else 'КиноСфера'
        message_id = self.store.add_message(room.code, user.id if user else None, name, kind, text)
        self.publish(room, 'chat', dict(id=message_id, user_id=user.id if user else None, name=name, kind=kind,
                                        text=text, created_at=time.time()))

    # ------------------------------------------------------------------ snapshot
    def members(self, room):
        now = time.time()
        rows = self.store.room_members(room.code)
        result = []
        for row in rows:
            presence = room.members.get(row['user_id'])
            online = bool(presence and (presence.connections > 0 or now - presence.last_seen < 12))
            result.append(dict(id=row['user_id'], name=row['username'], host=row['user_id'] == room.host_id,
                               online=online, status=presence.status if presence and online else 'offline',
                               position=round(presence.position, 1) if presence else 0,
                               color=COLORS[row['user_id'] % len(COLORS)]))
        result.sort(key=lambda m: (not m['host'], not m['online'], m['name']))
        return result

    def snapshot(self, room, user_id):
        film = self.store.film(room.movie_id, include_hidden=True)
        return dict(code=room.code, title=room.title, movie_id=room.movie_id, episode=room.state.get('episode'),
                    movie_title=film['film'].title if film else 'Фильм', host_id=room.host_id,
                    is_host=user_id == room.host_id, settings=room.settings, state=room.state,
                    position_now=room.position_now(), members=self.members(room),
                    messages=self.store.messages(room.code, 150), ended=bool(room.ended_at), seq=room.seq,
                    server_time=time.time(), reactions=REACTIONS)

    def push_members(self, room, force=False):
        now = time.time()
        if force or now - room.last_members_push > 1.5:
            room.last_members_push = now
            self.publish(room, 'members', self.members(room))

    # ------------------------------------------------------------------ actions
    def can_control(self, room, user):
        return room.settings.get('control') == 'all' or user.id == room.host_id

    def control(self, room, user, action, position=None, rate=None):
        if room.ended_at:
            raise PermissionError('Киновечер завершён.')
        if not self.can_control(room, user):
            raise PermissionError('Управлять просмотром может только хост комнаты.')
        now = time.time()
        with self._lock:
            state = room.state
            current = room.position_now(now)
            if position is not None:
                if not isinstance(position, (int, float)) or not 0 <= position < 12 * 3600:
                    raise ValueError('Некорректная позиция.')
                current = float(position)
            if action == 'play':
                state.update(playing=True, position=current)
            elif action == 'pause':
                state.update(playing=False, position=current)
            elif action == 'seek':
                state.update(position=current)
            elif action == 'rate':
                if rate not in (0.75, 1.0, 1.25, 1.5):
                    raise ValueError('Недопустимая скорость.')
                state.update(position=current, rate=rate)
            else:
                raise ValueError('Неизвестное действие.')
            state.update(updated_at=now, by=user.username)
            room.auto_paused = False
            self._persist(room)
            self.publish(room, 'state', dict(state, action=action))
        if action in ('play', 'pause') and room.settings.get('announce', True):
            verb = 'запустил(а)' if action == 'play' else 'поставил(а) на паузу'
            self.publish(room, 'notice', dict(text=f'{user.username} {verb} просмотр', at=now))
        elif action == 'seek':
            self.publish(room, 'notice', dict(text=f'{user.username} перемотал(а) на {fmt_time(current)}', at=now))

    def presence(self, room, user, status, position=None):
        now = time.time()
        with self._lock:
            member = room.members.get(user.id)
            if member is None:
                member = room.members[user.id] = Presence(user.id, user.username, COLORS[user.id % len(COLORS)])
            changed = member.status != status
            member.status = status
            member.last_seen = now
            if isinstance(position, (int, float)) and 0 <= position < 12 * 3600:
                member.position = float(position)
            if status == 'buffering':
                member.buffering_since = member.buffering_since or now
            else:
                member.buffering_since = None
            self._evaluate_waiting(room, now)
            self.push_members(room, force=changed)

    def _evaluate_waiting(self, room, now):
        if not room.settings.get('wait_for_all') or room.ended_at:
            return
        active = [m for m in room.members.values() if m.connections > 0 or now - m.last_seen < 8]
        slow = [m for m in active if m.buffering_since and now - m.buffering_since > 2.5]
        if room.state['playing'] and slow:
            room.state.update(playing=False, position=room.position_now(now), updated_at=now, by='auto')
            room.auto_paused = True
            self._persist(room)
            self.publish(room, 'state', dict(room.state, action='auto-pause'))
            names = ', '.join(m.name for m in slow[:3])
            self.publish(room, 'notice', dict(text=f'Ждём {names}: видео догружается…', at=now))
        elif room.auto_paused and not room.state['playing'] and not any(m.status == 'buffering' for m in active):
            room.auto_paused = False
            room.state.update(playing=True, updated_at=now, by='auto')
            self._persist(room)
            self.publish(room, 'state', dict(room.state, action='auto-resume'))
            self.publish(room, 'notice', dict(text='Все готовы — продолжаем!', at=now))

    def chat(self, room, user, text):
        if room.ended_at:
            raise PermissionError('Киновечер завершён.')
        if not room.settings.get('chat') and user.id != room.host_id:
            raise PermissionError('Хост отключил сообщения.')
        text = str(text or '').strip()[:1000]
        if not text:
            raise ValueError('Пустое сообщение.')
        self.message(room, user, 'chat', text)

    def react(self, room, user, emoji):
        if room.ended_at or (not room.settings.get('reactions') and user.id != room.host_id):
            raise PermissionError('Реакции отключены.')
        if emoji not in REACTIONS:
            raise ValueError('Неизвестная реакция.')
        now = time.time()
        recent = room.reaction_times.setdefault(user.id, deque(maxlen=8))
        if len(recent) == 8 and now - recent[0] < 4:
            raise PermissionError('Не так быстро 🙂')
        recent.append(now)
        self.publish(room, 'reaction', dict(emoji=emoji, name=user.username, user_id=user.id))

    def update_settings(self, room, user, values):
        if user.id != room.host_id:
            raise PermissionError('Настройки меняет хост комнаты.')
        clean = {}
        if values.get('control') in ('host', 'all'):
            clean['control'] = values['control']
        for key in ('wait_for_all', 'chat', 'reactions', 'locked'):
            if isinstance(values.get(key), bool):
                clean[key] = values[key]
        title = values.get('title')
        if isinstance(title, str) and 2 <= len(title.strip()) <= 80:
            room.title = title.strip()
        room.settings.update(clean)
        self._persist(room)
        self.publish(room, 'settings', dict(settings=room.settings, title=room.title))

    def change_movie(self, room, user, movie_id, episode=None):
        if user.id != room.host_id:
            raise PermissionError('Фильм выбирает хост комнаты.')
        film = self.store.film(movie_id)
        if not film or not film['torrent']:
            raise ValueError('Этот фильм пока недоступен для просмотра.')
        episode = self._episode(film, episode)
        now = time.time()
        room.movie_id = movie_id
        room.state = dict(playing=False, position=0.0, updated_at=now, rate=1.0, by=user.username, episode=episode)
        self._persist(room)
        playback = self.app.extensions.get('playback')
        if playback:
            threading.Thread(target=playback.prewarm, args=(movie_id, f'room-{room.code}', 1800),
                             kwargs=dict(file_id=episode), daemon=True).start()
        item = series.find(film.get('episodes') or [], episode) if episode is not None else None
        title = film['film'].title + (f' · {series.label(item)}' if item else '')
        self.publish(room, 'movie', dict(movie_id=movie_id, title=title, episode=episode, state=room.state))
        self.message(room, None, 'system', f'{user.username} выбрал(а) «{title}».')

    def transfer(self, room, user, target_id):
        if user.id != room.host_id:
            raise PermissionError('Передать управление может только хост.')
        if not self.store.is_member(room.code, target_id):
            raise ValueError('Участник не найден.')
        room.host_id = target_id
        self._persist(room)
        target = self.store.account(user_id=target_id)
        self.message(room, None, 'system', f'Теперь хост — {target["username"]}.')
        self.publish(room, 'host', dict(host_id=target_id))
        self.push_members(room, force=True)

    def end(self, room, user):
        if user.id != room.host_id and user.role != 'ADMIN':
            raise PermissionError('Завершить киновечер может только хост.')
        if room.ended_at:
            return
        room.ended_at = time.time()
        room.state.update(playing=False, position=room.position_now(), updated_at=room.ended_at)
        self.store.save_room(room.code, ended_at=room.ended_at, state=room.state)
        self.message(room, None, 'system', 'Киновечер завершён. Чат сохранён в истории комнат.')
        self.publish(room, 'ended', dict(at=room.ended_at))

    # ------------------------------------------------------------------ connections
    def connect(self, room, user):
        with self._lock:
            member = room.members.get(user.id)
            if member is None:
                member = room.members[user.id] = Presence(user.id, user.username, COLORS[user.id % len(COLORS)])
            first = member.connections == 0 and time.time() - member.last_seen > 15
            member.connections += 1
            member.last_seen = time.time()
        self.store.join_room(room.code, user.id)
        if first:
            self.publish(room, 'notice', dict(text=f'{user.username} присоединился(ась)', at=time.time()))
        self.push_members(room, force=True)

    def disconnect(self, room, user):
        with self._lock:
            member = room.members.get(user.id)
            if member is not None:
                member.connections = max(0, member.connections - 1)
                member.last_seen = time.time()
        self.push_members(room, force=True)

    def wait(self, room, seq, timeout):
        end = time.monotonic() + timeout
        with self._lock:
            while room.seq <= seq:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)
            oldest = room.events[0][0] if room.events else room.seq + 1
            if seq + 1 < oldest:
                return None       # client fell too far behind: resend snapshot
            return [e for e in room.events if e[0] > seq]

    def _ticker(self):
        while True:
            time.sleep(1)
            now = time.time()
            try:
                with self._lock:
                    for room in list(self.rooms.values()):
                        stale = [m for m in room.members.values() if m.connections == 0 and now - m.last_seen > 12 and m.status != 'offline']
                        for member in stale:
                            member.status = 'offline'
                        if stale:
                            self.push_members(room, force=True)
                        self._evaluate_waiting(room, now)
                        if not any(m.connections for m in room.members.values()) and now - max(
                                [m.last_seen for m in room.members.values()] or [0]) > 3600:
                            del self.rooms[room.code]
            except Exception:  # noqa: BLE001
                pass


def fmt_time(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def init_rooms(app):
    hub = RoomHub(app)
    app.extensions['rooms'] = hub
    app.register_blueprint(rooms_bp)
    return hub


def _hub() -> RoomHub:
    return current_app.extensions['rooms']


def _room_or_404(code):
    code = (code or '').upper()
    room = _hub().get(code) if 4 <= len(code) <= 12 and code.isalnum() else None
    if room is None:
        abort(404)
    return room


# ---------------------------------------------------------------------- pages
@rooms_bp.get('/rooms')
@login_required
def rooms_page():
    mine = store().user_rooms(g.user.id)
    films = [row for row in store().films() if row['torrent']]
    return render_template('rooms.html', active=[r for r in mine if not r['ended_at']],
                           history=[r for r in mine if r['ended_at']], films=films,
                           preselect=request.args.get('movie', type=int), error=request.args.get('error'))


@rooms_bp.post('/rooms/create')
@login_required
def create_room():
    try:
        movie_id = int(request.form.get('movie_id', ''))
        episode = request.form.get('episode', type=int)
        code = _hub().create(g.user, movie_id, request.form.get('title'), episode)
    except (ValueError, LookupError):
        return redirect(url_for('rooms.rooms_page', error='Выберите фильм из библиотеки.'))
    return redirect(url_for('rooms.room_page', code=code))


@rooms_bp.post('/rooms/join')
@login_required
def join_room():
    code = ''.join(ch for ch in request.form.get('code', '').upper() if ch.isalnum())
    if not _hub().get(code):
        return redirect(url_for('rooms.rooms_page', error='Комната с таким кодом не найдена.'))
    return redirect(url_for('rooms.room_page', code=code))


@rooms_bp.get('/room/<code>')
@login_required
def room_page(code):
    room = _room_or_404(code)
    if room.code != code:
        return redirect(url_for('rooms.room_page', code=room.code))
    member = store().is_member(room.code, g.user.id)
    if room.ended_at:
        return redirect(url_for('rooms.archive', code=room.code))
    if room.settings.get('locked') and not member:
        return render_template('room_locked.html', room=room), 403
    store().join_room(room.code, g.user.id)
    film = store().film(room.movie_id, include_hidden=True)
    films = [row for row in store().films() if row['torrent']]
    episodes = film.get('episodes') or [] if film['film'].kind == 'series' else []
    episode = series.find(episodes, room.state.get('episode')) if episodes else None
    return render_template('room.html', room=room, film=film['film'], films=films, episodes=episodes,
                           episode=episode or (episodes[0] if episodes else None),
                           is_host=room.host_id == g.user.id, has_source=bool(film['torrent']))


@rooms_bp.get('/rooms/<code>/archive')
@login_required
def archive(code):
    room = _room_or_404(code)
    if not store().is_member(room.code, g.user.id) and g.user.role != 'ADMIN':
        abort(403)
    row = store().room(room.code)
    film = store().film(room.movie_id, include_hidden=True)
    return render_template('archive.html', room=row, film=film['film'] if film else None,
                           messages=store().messages(room.code, 1000), members=store().room_members(room.code))


# ---------------------------------------------------------------------- API
@rooms_bp.errorhandler(PermissionError)
def _forbidden(error):
    return jsonify(error=str(error)), 403


@rooms_bp.get('/api/time')
def server_time():
    response = jsonify(t=time.time())
    response.headers['Cache-Control'] = 'no-store'
    return response


def _api_room(code):
    if not g.user:
        abort(401)
    room = _room_or_404(code)
    if not store().is_member(room.code, g.user.id):
        abort(403)
    return room


@rooms_bp.get('/api/rooms/<code>/events')
def events(code):
    room = _api_room(code)
    user = g.user
    hub = _hub()
    try:
        since = int(request.headers.get('Last-Event-ID') or request.args.get('since') or -1)
    except ValueError:
        since = -1

    def stream():
        hub.connect(room, user)
        try:
            seq = since
            if seq < 0 or seq > room.seq:
                snap = hub.snapshot(room, user.id)
                seq = snap['seq']
                yield f'id: {seq}\nevent: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n'
            yield 'retry: 2000\n\n'
            while True:
                items = hub.wait(room, seq, 15)
                if items is None:
                    snap = hub.snapshot(room, user.id)
                    seq = snap['seq']
                    yield f'id: {seq}\nevent: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n'
                    continue
                if not items:
                    yield f': ping {time.time():.3f}\n\n'
                    continue
                for number, kind, data in items:
                    seq = number
                    payload = dict(data) if isinstance(data, dict) else dict(items=data)
                    payload['server_time'] = time.time()
                    yield f'id: {number}\nevent: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
        finally:
            hub.disconnect(room, user)

    response = Response(stream_with_context(stream()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


def _json():
    require_csrf()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    return data


@rooms_bp.post('/api/rooms/<code>/control')
def control(code):
    room = _api_room(code)
    data = _json()
    try:
        _hub().control(room, g.user, data.get('action'), data.get('position'), data.get('rate'))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(state=room.state, server_time=time.time())


@rooms_bp.post('/api/rooms/<code>/presence')
def presence(code):
    room = _api_room(code)
    data = _json()
    status = data.get('status') if data.get('status') in ('idle', 'ready', 'buffering', 'playing', 'paused', 'error') else 'idle'
    _hub().presence(room, g.user, status, data.get('position'))
    return jsonify(ok=True, server_time=time.time())


@rooms_bp.post('/api/rooms/<code>/chat')
def chat(code):
    room = _api_room(code)
    try:
        _hub().chat(room, g.user, _json().get('text'))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


@rooms_bp.post('/api/rooms/<code>/react')
def react(code):
    room = _api_room(code)
    try:
        _hub().react(room, g.user, _json().get('emoji'))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


@rooms_bp.post('/api/rooms/<code>/settings')
def settings(code):
    room = _api_room(code)
    _hub().update_settings(room, g.user, _json())
    return jsonify(settings=room.settings, title=room.title)


@rooms_bp.post('/api/rooms/<code>/movie')
def movie(code):
    room = _api_room(code)
    data = _json()
    try:
        episode = data.get('episode')
        _hub().change_movie(room, g.user, int(data.get('movie_id')), episode if type(episode) is int else None)
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc) if isinstance(exc, ValueError) and str(exc) else 'Выберите фильм.'), 400
    return jsonify(ok=True)


@rooms_bp.post('/api/rooms/<code>/host')
def host(code):
    room = _api_room(code)
    data = _json()
    try:
        _hub().transfer(room, g.user, int(data.get('user_id')))
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc) or 'Выберите участника.'), 400
    return jsonify(ok=True)


@rooms_bp.post('/api/rooms/<code>/end')
def end(code):
    room = _api_room(code)
    _json()
    _hub().end(room, g.user)
    return jsonify(ok=True, archive=url_for('rooms.archive', code=room.code))
