"""Account registration, session authentication, CSRF and account settings."""
from datetime import timedelta
from functools import wraps
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import unicodedata
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import mailer

auth = Blueprint('auth', __name__)
SETTINGS_KEYS = {'name', 'visibility', 'notifications', 'color', 'quality', 'autoplay', 'bio', 'volume'}
RESET_TTL = 60 * 60          # reset link lifetime, seconds
RESET_COOLDOWN = 60          # minimum pause between letters to one account
RESET_IP_LIMIT = (5, 15 * 60)  # requests per address per window


def store():
    return current_app.extensions['store']


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


def require_csrf():
    supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
    if not hmac.compare_digest(str(supplied), str(session.get('csrf', ''))) or not supplied:
        abort(400, 'Страница устарела. Обновите её и повторите действие.')


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            if request.path.startswith('/api/'):
                return jsonify(error='Войдите в аккаунт.'), 401
            return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user.role != 'ADMIN':
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def init_accounts(app):
    key_path = Path(app.root_path) / '.local/session.key'
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        try:
            with key_path.open('x', encoding='ascii') as file:
                file.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    app.config.setdefault('SECRET_KEY', key_path.read_text(encoding='ascii').strip())
    if not app.config.get('SECRET_KEY'):
        app.config['SECRET_KEY'] = key_path.read_text(encoding='ascii').strip()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      PERMANENT_SESSION_LIFETIME=timedelta(days=14))
    app.register_blueprint(auth)

    @app.before_request
    def load_account():
        g.user = None
        if request.path.startswith('/auth/'):
            request.max_content_length = 16 * 1024
        elif request.path.startswith('/admin/films/'):
            request.max_content_length = 104 * 1024 * 1024      # posters + slider video
        elif request.path == '/admin/tmdb/import':
            request.max_content_length = 10 * 1024 * 1024 + 65536
        elif request.path.startswith('/admin/'):
            request.max_content_length = 3 * 1024 * 1024
        row = store().account(user_id=session['user_id']) if session.get('user_id') else None
        if row and row['active'] and row['session_version'] == session.get('session_version'):
            g.user = store().user(row)
            if not row.get('last_seen') or time.time() - row['last_seen'] > 60:
                store().touch_user(row['id'])
        elif session.get('user_id'):
            session.clear()
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not request.path.startswith('/internal/'):
            require_csrf()

    @app.context_processor
    def account_context():
        return dict(csrf_token=csrf_token, current_user=g.user,
                    initials=''.join(part[0] for part in g.user.username.split()[:2]).upper() if g.user else '',
                    user_state=store().state(g.user.id) if g.user else None)

    @app.after_request
    def private_headers(response):
        if request.path.startswith('/static/'):
            # Static assets are versioned (?v=…) by asset_url(): cache them for a long time.
            if request.args.get('v'):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            else:
                response.headers.setdefault('Cache-Control', 'public, max-age=3600')
        elif 'Cache-Control' not in response.headers and (
                request.path.startswith(('/profile', '/admin', '/auth', '/watch', '/api/', '/room')) or g.get('user')):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response


def sign_in(row):
    session.clear()
    session.update(user_id=row['id'], session_version=row['session_version'])
    session.permanent = True


def safe_next():
    target = request.values.get('next', '')
    if target.startswith('/') and not target.startswith('//') and not urlsplit(target).netloc and '\\' not in target and not any(ord(c) < 32 for c in target):
        return target
    return url_for('index')


def normalize_email(value):
    return unicodedata.normalize('NFKC', value or '').strip().casefold()


def validate_password(password, confirm):
    if not 10 <= len(password) <= 128:
        raise ValueError('Пароль должен содержать от 10 до 128 символов.')
    if password != confirm:
        raise ValueError('Пароли не совпадают.')


@auth.route('/auth/register', methods=['GET', 'POST'])
def register():
    if g.user:
        return redirect(url_for('index'))
    error = None
    first_account = not store().accounts_exist()
    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        name = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        try:
            if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                raise ValueError('Введите корректный email.')
            if not 2 <= len(name) <= 60:
                raise ValueError('Имя должно содержать от 2 до 60 символов.')
            validate_password(password, request.form.get('confirm', ''))
            user_id = store().register(email, name, generate_password_hash(password))
            row = store().account(user_id=user_id)
            sign_in(row)
            flash('Аккаунт создан. Добро пожаловать в КиноСферу!')
            return redirect(url_for('onboarding.preferences', next=url_for('index')))
        except sqlite3.IntegrityError:
            error = 'Этот email уже зарегистрирован. Войдите в аккаунт.'
        except ValueError as exc:
            error = str(exc)
    return render_template('auth.html', mode='register', error=error, first_account=first_account)


@auth.route('/auth/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(safe_next())
    error = None
    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        key = hashlib.sha256((str(request.remote_addr) + ':' + email).encode()).hexdigest()
        now = time.time()
        with store().connect() as db:
            attempt = db.execute('SELECT * FROM login_attempts WHERE key=?', (key,)).fetchone()
        if attempt and attempt['failures'] >= 6 and now - attempt['last_at'] < 300:
            error = 'Слишком много попыток. Попробуйте через пять минут.'
        else:
            row = store().account(email=email)
            valid = check_password_hash(row['password_hash'], request.form.get('password', '')) if row else False
            if row and row['active'] and valid:
                with store().connect() as db:
                    db.execute('DELETE FROM login_attempts WHERE key=?', (key,))
                sign_in(row)
                from onboarding import needs_onboarding
                target = safe_next()
                return redirect(url_for('onboarding.preferences', next=target) if needs_onboarding(row['id']) else target)
            failures = attempt['failures'] + 1 if attempt and now - attempt['last_at'] < 300 else 1
            with store().connect() as db:
                db.execute('INSERT INTO login_attempts VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET failures=excluded.failures,last_at=excluded.last_at',
                           (key, failures, now))
            error = 'Неверный email или пароль.'
    return render_template('auth.html', mode='login', error=error, first_account=not store().accounts_exist())


def reset_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def reset_rate_limited():
    """Throttle reset requests per client address (shares the login_attempts table)."""
    limit, window = RESET_IP_LIMIT
    key = 'reset:' + hashlib.sha256(str(request.remote_addr).encode()).hexdigest()
    now = time.time()
    with store().connect() as db:
        attempt = db.execute('SELECT * FROM login_attempts WHERE key=?', (key,)).fetchone()
        if attempt and now - attempt['last_at'] < window and attempt['failures'] >= limit:
            return True
        count = attempt['failures'] + 1 if attempt and now - attempt['last_at'] < window else 1
        db.execute('INSERT INTO login_attempts VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET failures=excluded.failures,last_at=excluded.last_at',
                   (key, count, now if count == 1 else attempt['last_at']))
    return False


@auth.route('/auth/forgot', methods=['GET', 'POST'])
def forgot_password():
    if g.user:
        return redirect(url_for('profile', section='settings'))
    error = None
    email = normalize_email(request.values.get('email'))
    if request.method == 'POST':
        if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            error = 'Введите корректный email.'
        elif reset_rate_limited():
            error = 'Слишком много запросов. Попробуйте через 15 минут.'
        else:
            row = store().account(email=email)
            token = secrets.token_urlsafe(32)
            # The same answer for unknown, disabled and throttled accounts: no account enumeration.
            if row and row['active'] and store().create_password_reset(row['id'], reset_hash(token), RESET_TTL, RESET_COOLDOWN):
                link = mailer.public_url('auth.reset_password', token=token)
                values = dict(name=row['username'], link=link, minutes=RESET_TTL // 60)
                mailer.send(row['email'], 'Восстановление пароля · КиноСфера',
                            render_template('email/password_reset.txt', **values),
                            render_template('email/password_reset.html', **values))
            return render_template('password_reset.html', mode='sent', email=email)
    return render_template('password_reset.html', mode='request', error=error, email=email)


@auth.route('/auth/reset/<token>', methods=['GET', 'POST'])
def reset_password(token):
    token_hash = reset_hash(token[:128])
    row = store().password_reset(token_hash)
    if not row:
        return render_template('password_reset.html', mode='invalid'), 410
    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        try:
            validate_password(password, request.form.get('confirm', ''))
            user_id = store().reset_password(token_hash, generate_password_hash(password))
            if user_id is None:
                return render_template('password_reset.html', mode='invalid'), 410
            hub = current_app.extensions.get('playback')
            if hub:
                hub.end_user(user_id)
            sign_in(store().account(user_id=user_id))
            flash('Пароль изменён. Все остальные сеансы завершены.')
            return redirect(url_for('index'))
        except ValueError as exc:
            error = str(exc)
    return render_template('password_reset.html', mode='reset', error=error, email=row['email'])


@auth.post('/auth/logout')
@login_required
def logout():
    hub = current_app.extensions.get('playback')
    if hub:
        hub.end_user(g.user.id)
    session.clear()
    return redirect(url_for('index'))


@auth.post('/auth/password')
@login_required
def password():
    row = store().account(user_id=g.user.id)
    try:
        if not check_password_hash(row['password_hash'], request.form.get('current_password', '')):
            raise ValueError('Текущий пароль указан неверно.')
        new = request.form.get('password', '')
        validate_password(new, request.form.get('confirm', ''))
        store().change_password(g.user.id, generate_password_hash(new))
        hub = current_app.extensions.get('playback')
        if hub:
            hub.end_user(g.user.id)
        sign_in(store().account(user_id=g.user.id))
        flash('Пароль изменён. Остальные сеансы завершены.')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('profile', section='settings'))


@auth.route('/api/profile/state', methods=['GET', 'POST'])
@login_required
def profile_state():
    if request.method == 'POST':
        request.max_content_length = 512 * 1024
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='Некорректные данные профиля.'), 400
        result = {}
        for key in ('watched', 'watchlist'):
            values = data.get(key, [])
            result[key] = list(dict.fromkeys(v for v in values if type(v) is int and v > 0))[:2000] if isinstance(values, list) else []
        ratings = data.get('ratings', {})
        result['ratings'] = {str(k): v for k, v in ratings.items() if str(k).isdigit() and type(v) is int and 1 <= v <= 10} if isinstance(ratings, dict) else {}
        for key in ('clips', 'friends'):
            result[key] = data.get(key, [])[:1000] if isinstance(data.get(key), list) else []
        settings = data.get('settings', {}) if isinstance(data.get('settings'), dict) else {}
        result['settings'] = {k: (v[:300] if isinstance(v, str) else v) for k, v in settings.items()
                              if k in SETTINGS_KEYS and isinstance(v, (str, bool, int))}
        result['rooms'] = {}
        store().save_state(g.user.id, result)
        return jsonify(ok=True)
    return jsonify(store().state(g.user.id))
