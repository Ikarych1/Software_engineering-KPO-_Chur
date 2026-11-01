"""Administrator-only upload, progress and reports for IMDb imports."""
import csv
import io
import os
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request

from accounts import admin_required, store
from imdb_import import EXAMPLE, ImportJobs, MAX_LIST_BYTES, read_list

imdb = Blueprint('imdb', __name__, url_prefix='/admin/imdb')


def init_imdb(app):
    app.extensions['imdb_importer'] = ImportJobs(Path(os.environ.get('CINEMA_IMDB_CACHE_DIR') or Path(app.root_path) / '.local/imdb'))
    app.register_blueprint(imdb)


@imdb.get('')
@imdb.get('/')
@admin_required
def page():
    return render_template('imdb_import.html')


@imdb.get('/example.csv')
@admin_required
def example():
    return Response('\ufeff' + EXAMPLE, mimetype='text/csv', headers={'Content-Disposition':'attachment; filename="imdb-movies.csv"'})


@imdb.post('/import')
@admin_required
def start():
    request.max_content_length = MAX_LIST_BYTES + 65536
    upload = request.files.get('list')
    try:
        if not upload or not (upload.filename or '').lower().endswith('.csv'):
            raise ValueError('Выберите CSV-файл со списком фильмов.')
        entries = read_list(upload.read(MAX_LIST_BYTES + 1))
        token = current_app.extensions['imdb_importer'].start(store(), entries,
            publish=request.form.get('publish') == 'on', credits=request.form.get('credits') == 'on',
            refresh=request.form.get('refresh') == 'on')
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(job=token), 202


def job_for(token):
    job = current_app.extensions['imdb_importer'].status(token)
    if not job:
        abort(404, 'Импорт не найден. Возможно, сервер был перезапущен. Загрузите CSV повторно — дубликаты не появятся.')
    return job


@imdb.errorhandler(404)
def missing_job(error):
    return jsonify(error=error.description), 404


@imdb.get('/jobs/<token>')
@admin_required
def status(token):
    return jsonify(job_for(token))


@imdb.post('/jobs/<token>/cancel')
@admin_required
def cancel(token):
    job_for(token)
    current_app.extensions['imdb_importer'].cancel(token)
    return jsonify(ok=True)


@imdb.get('/jobs/<token>/report.csv')
@admin_required
def report(token):
    job = job_for(token)
    if job['state'] == 'running':
        return jsonify(error='Импорт ещё выполняется.'), 409
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['line', 'input', 'imdb_id', 'movie_id', 'status', 'message'])
    for row in job['results']:
        cells = [row.get(key, '') for key in ('line', 'input', 'imdb_id', 'movie_id', 'status', 'message')]
        writer.writerow(["'" + str(v) if str(v).lstrip().startswith(('=', '+', '-', '@')) else v for v in cells])
    return Response('\ufeff' + output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename="imdb-report.csv"'})
