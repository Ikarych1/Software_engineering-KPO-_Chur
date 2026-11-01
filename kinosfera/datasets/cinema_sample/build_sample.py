"""Extract a small, referentially closed sample from the separate v3 fixture.

The source is always opened read-only. No application database is modified.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import random
import shutil
import sqlite3
import sys
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SEED = 20261101


def qid(name):
    return '"' + name.replace('"', '""') + '"'


def jd(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def select(db, table, field=None, values=None):
    sql = 'SELECT * FROM ' + qid(table)
    if field is not None:
        if not values:
            return []
        values = sorted(values)
        sql += ' WHERE ' + qid(field) + ' IN (' + ','.join('?' for _ in values) + ')'
        return [dict(row) for row in db.execute(sql, values)]
    return [dict(row) for row in db.execute(sql)]


def schema_objects(db):
    return {(row['type'], row['name']): row['sql'] for row in db.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def payload_valid(payload, ids):
    keys = {'bookmark_event_id': 'bookmark_events', 'impression_id': 'impressions',
            'click_id': 'clicks', 'clip_id': 'clips', 'review_id': 'reviews',
            'search_id': 'searches', 'episode_id': 'episodes', 'room_code': 'rooms',
            'room_id': 'rooms', 'selected_movie_id': 'films'}
    return all(not payload.get(key) or payload[key] in ids[target] for key, target in keys.items())


def choose(db, user_count, film_count, room_count):
    rng = random.Random(SEED)
    admin = db.execute("SELECT id FROM users WHERE role='ADMIN' ORDER BY id LIMIT 1").fetchone()[0]
    users = {admin}
    rooms = []
    candidates = select(db, 'rooms')
    rng.shuffle(candidates)
    for room in candidates:
        members = select(db, 'room_members', 'code', {room['code']})
        messages = select(db, 'room_messages', 'code', {room['code']})
        participants = {room['host_id']} | {row['user_id'] for row in members}
        participants |= {row['user_id'] for row in messages if row['user_id'] is not None}
        actor = json.loads(room['state']).get('by')
        if actor:
            participants.add(actor)
        if len(users | participants) <= user_count - 15:
            users |= participants
            rooms.append(room)
        if len(rooms) == room_count:
            break
    native_progress = [dict(row) for row in db.execute('SELECT * FROM episode_progress ORDER BY user_id,movie_id LIMIT 5')]
    users |= {row['user_id'] for row in native_progress}
    episode_users = [row[0] for row in db.execute(
        'SELECT DISTINCT user_id FROM watch_sessions WHERE episode_id IS NOT NULL LIMIT 5')]
    users.update(episode_users)
    candidates = [row[0] for row in db.execute('SELECT id FROM users ORDER BY id') if row[0] not in users]
    users.update(rng.sample(candidates, user_count - len(users)))
    states = {row['user_id']: json.loads(row['data']) for row in select(db, 'user_state', 'user_id', users)}
    ratings = select(db, 'ratings', 'user_id', users)
    sessions = select(db, 'watch_sessions', 'user_id', users)
    films = {row['movie_id'] for row in rooms + native_progress}
    # Include existing application seed titles so starting the site adds no new films.
    sys.path.insert(0, str(ROOT))
    from storage import CATALOG
    from ui_data import EXTRA_FILMS
    existing_films = {row[0] for row in db.execute('SELECT id FROM films')}
    films.update(film.id for film in CATALOG + EXTRA_FILMS if film.id in existing_films)
    # Keep one real source-backed title, without inventing any video source.
    first_source = db.execute('SELECT movie_id FROM film_sources ORDER BY movie_id LIMIT 1').fetchone()
    if first_source:
        films.add(first_source[0])
    films.update(row['movie_id'] for row in sessions if row['episode_id'] is not None)
    clips = [clip for uid in sorted(states) for clip in states[uid].get('clips', [])]
    films.update(clip['movieId'] for clip in clips[:10])
    counts = Counter(row['movie_id'] for row in ratings)
    for movie_id, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        if len(films) >= film_count:
            break
        films.add(movie_id)
    if len(films) < film_count:
        for row in db.execute('SELECT id FROM films ORDER BY id'):
            films.add(row[0])
            if len(films) >= film_count:
                break
    return users, films, rooms, states


def extract(db, users, films, rooms, states):
    tables = {row[0]: [] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    codes = {room['code'] for room in rooms}
    tables['users'] = select(db, 'users', 'id', users)
    tables['films'] = select(db, 'films', 'id', films)
    tables['rooms'] = rooms
    tables['room_members'] = select(db, 'room_members', 'code', codes)
    tables['room_messages'] = select(db, 'room_messages', 'code', codes)
    tables['episodes'] = select(db, 'episodes', 'movie_id', films)
    tables['film_sources'] = select(db, 'film_sources', 'movie_id', films)
    tables['user_preferences'] = select(db, 'user_preferences', 'user_id', users)
    tables['payments'] = select(db, 'payments', 'user_id', users)
    for name in ('ratings', 'reviews', 'progress', 'episode_progress', 'not_interested', 'tags', 'bookmark_events'):
        tables[name] = [row for row in select(db, name, 'user_id', users) if row['movie_id'] in films]
    tables['recommendation_requests'] = select(db, 'recommendation_requests', 'user_id', users)
    requests = {row['id'] for row in tables['recommendation_requests']}
    tables['impressions'] = [row for row in select(db, 'impressions', 'user_id', users)
                             if row['movie_id'] in films and row['request_id'] in requests]
    impressions = {row['id'] for row in tables['impressions']}
    tables['clicks'] = [row for row in select(db, 'clicks', 'user_id', users) if row['impression_id'] in impressions]
    tables['searches'] = [row for row in select(db, 'searches', 'user_id', users)
                          if row['selected_movie_id'] is None or row['selected_movie_id'] in films]
    episodes = {row['id'] for row in tables['episodes']}
    tables['watch_sessions'] = [row for row in select(db, 'watch_sessions', 'user_id', users)
                               if row['movie_id'] in films
                               and (row['episode_id'] is None or row['episode_id'] in episodes)
                               and (row['room_code'] is None or row['room_code'] in codes)
                               and (row['request_id'] is None or row['request_id'] in requests)]
    tables['views'] = select(db, 'views', 'id', {row['view_id'] for row in tables['watch_sessions']})
    ratings = {}
    for row in tables['ratings']:
        ratings.setdefault(row['user_id'], {})[str(row['movie_id'])] = row['score']
    clips = set()
    for user_id, state in states.items():
        state['watched'] = [mid for mid in state.get('watched', []) if mid in films]
        state['watchlist'] = [mid for mid in state.get('watchlist', []) if mid in films]
        state['ratings'] = ratings.get(user_id, {})
        state['clips'] = [clip for clip in state.get('clips', []) if clip['movieId'] in films]
        state['rooms'] = {code: value for code, value in state.get('rooms', {}).items() if code in codes}
        state['friends'] = [friend for friend in state.get('friends', []) if friend in users]
        clips.update(clip['id'] for clip in state['clips'])
        tables['user_state'].append(dict(user_id=user_id, data=jd(state)))
    ids = {name: {row['id'] for row in tables[name]} for name in
           ('watch_sessions', 'bookmark_events', 'impressions', 'clicks', 'reviews', 'searches', 'episodes')}
    ids.update(clips=clips, rooms=codes, films=films)
    tables['events'] = [row for row in select(db, 'events', 'user_id', users)
                        if (row['movie_id'] is None or row['movie_id'] in films)
                        and (row['session_id'] is None or row['session_id'] in ids['watch_sessions'])
                        and (row['request_id'] is None or row['request_id'] in requests)
                        and payload_valid(json.loads(row['payload']), ids)]
    return tables


def validate(db, source_schema, tables):
    assert schema_objects(db) == source_schema, 'Schema changed'
    assert db.execute('PRAGMA integrity_check').fetchall()[0][0] == 'ok'
    assert not list(db.execute('PRAGMA foreign_key_check')), 'Broken foreign keys'
    users = {row['id'] for row in tables['users']}
    films = {row['id'] for row in tables['films']}
    rooms = {row['code'] for row in tables['rooms']}
    ratings = {}
    for row in tables['ratings']:
        ratings.setdefault(row['user_id'], {})[str(row['movie_id'])] = row['score']
    counts = {name: db.execute('SELECT COUNT(*) FROM ' + qid(name)).fetchone()[0] for name in tables}
    watched = watchlist = clips = 0
    for row in tables['user_state']:
        state = json.loads(row['data'])
        assert state['ratings'] == ratings.get(row['user_id'], {})
        assert all(mid in films for key in ('watched', 'watchlist') for mid in state[key])
        assert all(clip['movieId'] in films for clip in state['clips'])
        assert all(friend in users for friend in state['friends'])
        assert all(code in rooms for code in state['rooms'])
        watched += len(state['watched']); watchlist += len(state['watchlist']); clips += len(state['clips'])
    for name, records in tables.items():
        for row in records:
            if 'user_id' in row and row['user_id'] is not None:
                assert row['user_id'] in users, (name, row['user_id'])
            if 'movie_id' in row and row['movie_id'] is not None:
                assert row['movie_id'] in films, (name, row['movie_id'])
            if row.get('room_code'):
                assert row['room_code'] in rooms
    for room in tables['rooms']:
        assert room['host_id'] in users and json.loads(room['state']).get('by') in users
    for row in tables['episode_progress']:
        files = json.loads(next(f['episodes'] for f in tables['films'] if f['id'] == row['movie_id']))
        assert row['file_id'] in {episode['file_id'] for episode in files}
    assert not db.execute('''SELECT 1 FROM watch_sessions s JOIN views v ON v.id=s.view_id
        WHERE s.user_id<>v.user_id OR s.movie_id<>v.movie_id OR s.started_at<>v.started_at
        OR s.room_code IS NOT v.room_code LIMIT 1''').fetchone()
    assert not db.execute('''SELECT 1 FROM clicks c JOIN impressions i ON i.id=c.impression_id
        WHERE c.user_id<>i.user_id OR c.movie_id<>i.movie_id OR c.request_id<>i.request_id LIMIT 1''').fetchone()
    assert len(tables['watch_sessions']) == len(tables['views'])
    payload_ids = {name: {row['id'] for row in tables[name]} for name in
                   ('bookmark_events','impressions','clicks','reviews','searches','episodes')}
    payload_ids.update(rooms=rooms, films=films,
                       clips={clip['id'] for row in tables['user_state'] for clip in json.loads(row['data'])['clips']})
    assert all(payload_valid(json.loads(row['payload']),payload_ids) for row in tables['events'])
    assert all(counts[name] > 0 for name in ('ratings', 'reviews', 'progress', 'episode_progress', 'watch_sessions',
        'user_preferences', 'impressions', 'clicks', 'searches', 'bookmark_events', 'not_interested', 'tags', 'rooms', 'events'))
    assert clips > 0 and watched > 0 and watchlist > 0
    return dict(passed=True, schema_matches_source=True, tables=30, views=['episode_activity'], counts=counts,
                watched=watched, watchlist=watchlist, clips=clips,
                episode_activity=db.execute('SELECT COUNT(*) FROM episode_activity').fetchone()[0],
                integrity_check='ok', foreign_key_violations=0,
                logical_links='checked, including JSON profiles and event payload references')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=HERE.parent/'cinema_project_v3/cinema_test.sqlite3')
    parser.add_argument('--users', type=int, default=100)
    parser.add_argument('--films', type=int, default=200)
    parser.add_argument('--rooms', type=int, default=10)
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_file():
        parser.error('Source SQLite fixture not found')
    stamp = (source.stat().st_size, source.stat().st_mtime_ns)
    target = HERE/'cinema_sample.sqlite3'
    staging = HERE/'cinema_sample.building.sqlite3'
    if staging.exists():
        raise SystemExit('A staging database already exists; inspect it before rebuilding.')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as src:
        src.row_factory = sqlite3.Row
        src.execute('PRAGMA query_only=ON')
        src.execute('BEGIN')
        expected_schema = schema_objects(src)
        users, films, rooms, states = choose(src, args.users, args.films, args.rooms)
        tables = extract(src, users, films, rooms, states)
        with closing(sqlite3.connect(staging)) as dst:
            dst.row_factory = sqlite3.Row
            dst.execute('PRAGMA foreign_keys=OFF')
            for (kind, name), sql in expected_schema.items():
                if kind == 'table':
                    dst.execute(sql)
            for name, records in tables.items():
                if records:
                    columns = list(records[0])
                    dst.executemany('INSERT INTO '+qid(name)+' ('+','.join(qid(c) for c in columns)+') VALUES ('+
                                    ','.join('?' for _ in columns)+')', [[row[c] for c in columns] for row in records])
            for (kind, name), sql in expected_schema.items():
                if kind != 'table':
                    dst.execute(sql)
            dst.execute('PRAGMA user_version='+str(src.execute('PRAGMA user_version').fetchone()[0]))
            dst.commit()
            dst.execute('PRAGMA foreign_keys=ON')
            report = validate(dst, expected_schema, tables)
            dst.execute('VACUUM')
        src.rollback()
    assert stamp == (source.stat().st_size, source.stat().st_mtime_ns), 'Source file changed'
    staging.replace(target)
    report.update(source=str(source.relative_to(ROOT)), source_open_mode='read-only', source_unchanged=True,
                  seed=SEED, bytes=target.stat().st_size, sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                  ids='original', new_synthetic_records=0,
                  selection='100 users including complete selected rooms; popular titles, clips and episode coverage',
                  omitted_operational_tables=['login_attempts','password_resets','media_info','tmdb_jobs','tmdb_rows','tmdb_cache'])
    (HERE/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (HERE/'schema.sql').write_text('\n\n'.join(sql+';' for sql in expected_schema.values())+'\n',encoding='utf-8')
    target.with_suffix('.sqlite3.sha256').write_text(report['sha256']+'  '+target.name+'\n',encoding='ascii')
    shutil.copyfile(HERE.parent/'cinema_project_v3/MOVIELENS_SOURCE_README.txt', HERE/'MOVIELENS_SOURCE_README.txt')
    archive = ROOT/'exports/cinema_sample.zip'
    archive.parent.mkdir(exist_ok=True)
    names = ['cinema_sample.sqlite3','cinema_sample.sqlite3.sha256','README.md','example_queries.sql',
             'schema.sql','validation.json','MOVIELENS_SOURCE_README.txt','build_sample.py','verify_sample.py']
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as out:
        for name in names:
            out.write(HERE/name,'cinema_sample/'+name)
    with zipfile.ZipFile(archive) as check:
        assert check.testzip() is None
    print(json.dumps(dict(database=str(target),bytes=report['bytes'],users=len(users),films=len(films),
                         ratings=report['counts']['ratings'],archive=str(archive),archive_bytes=archive.stat().st_size),ensure_ascii=True))


if __name__ == '__main__':
    main()
