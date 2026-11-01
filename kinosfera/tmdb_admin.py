"""Admin UI and bounded status/report endpoints for official TMDB imports."""
import csv
import io
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request, stream_with_context

from accounts import admin_required
from tmdb_import import Client, EXAMPLE, ImportJobs, MAX_LIST_BYTES, Settings, read_list

tmdb = Blueprint('tmdb', __name__, url_prefix='/admin/tmdb')


def init_tmdb(app):
    settings = Settings(Path(app.root_path)/'.local/tmdb-settings.json')
    app.extensions['tmdb_importer'] = ImportJobs(app.extensions['store'], settings)
    app.register_blueprint(tmdb)

    @app.get('/about/data')
    def data_sources():
        return render_template('data_sources.html')


def jobs():
    return current_app.extensions['tmdb_importer']


@tmdb.get('')
@tmdb.get('/')
@admin_required
def page():
    return render_template('tmdb_import.html', connected=bool(jobs().settings.credential()),
                           environment_key=jobs().settings.from_environment(), recent=jobs().recent())


@tmdb.post('/settings')
@admin_required
def settings():
    if jobs().settings.from_environment():
        return jsonify(error='Ключ задан переменной окружения TMDB_READ_TOKEN или TMDB_API_KEY. Измените её на сервере.'), 400
    credential = (request.form.get('credential') or '').strip()
    try:
        # Validate before replacing the working key; never send it back to the browser.
        client = Client(jobs().store, credential, refresh=True)
        client.get('/authentication')
        jobs().settings.save(credential)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, message='TMDB подключён. Можно запускать импорт.')


@tmdb.get('/example.csv')
@admin_required
def example():
    return Response('\ufeff'+EXAMPLE, mimetype='text/csv', headers={'Content-Disposition':'attachment; filename="tmdb-movies.csv"'})


@tmdb.post('/import')
@admin_required
def start():
    upload = request.files.get('list')
    try:
        if not upload or not (upload.filename or '').lower().endswith('.csv'):
            raise ValueError('Выберите CSV-файл со списком фильмов.')
        entries = read_list(upload.read(MAX_LIST_BYTES+1))
        token = jobs().start(entries, publish=request.form.get('publish') == 'on', refresh=request.form.get('refresh') == 'on')
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(job=token), 202


def job_for(token):
    try:
        page_number = int(request.args.get('page', 1))
    except ValueError:
        page_number = 1
    job = jobs().status(token, page_number)
    if not job:
        abort(404, 'Импорт не найден.')
    return job


@tmdb.errorhandler(404)
@tmdb.errorhandler(413)
def error_response(error):
    return jsonify(error='Выберите CSV размером до 10 МБ.' if error.code == 413 else error.description), error.code


@tmdb.get('/jobs/<token>')
@admin_required
def status(token):
    return jsonify(job_for(token))


@tmdb.post('/jobs/<token>/cancel')
@admin_required
def cancel(token):
    job_for(token)
    jobs().cancel(token)
    return jsonify(ok=True)


@tmdb.post('/jobs/<token>/resume')
@admin_required
def resume(token):
    job_for(token)
    try:
        jobs().resume(token)
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(ok=True), 202


@tmdb.get('/jobs/<token>/report.csv')
@admin_required
def report(token):
    job_for(token)
    manager = jobs()

    def generate():
        out = io.StringIO(newline='')
        writer = csv.writer(out)
        fields = ('line', 'input', 'title', 'imdb_id', 'tmdb_id', 'movie_id', 'status', 'message')
        yield '\ufeff'
        writer.writerow(fields)
        yield out.getvalue()
        for row in manager.report(token):
            out.seek(0); out.truncate(0)
            cells = [row.get(key, '') for key in fields]
            writer.writerow(["'"+str(v) if str(v).lstrip().startswith(('=', '+', '-', '@')) else v for v in cells])
            yield out.getvalue()
    return Response(stream_with_context(generate()), mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename="tmdb-report.csv"'})
