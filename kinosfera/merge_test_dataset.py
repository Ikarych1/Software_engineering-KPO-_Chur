"""Merge a project-format fixture into a working cinema DB without replacing any existing rows.

The source is read-only. All inserts and the import receipt commit together;
existing rows are compared with a SQLite backup before committing.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time


ORDER = ('users', 'user_state', 'user_preferences', 'episodes', 'rooms', 'room_members',
         'room_messages', 'ratings', 'reviews', 'progress', 'episode_progress',
         'recommendation_requests', 'impressions', 'clicks', 'searches', 'views',
         'watch_sessions', 'bookmark_events', 'not_interested', 'tags', 'events', 'payments')
TEXT_IDS = {'recommendation_requests', 'impressions', 'clicks', 'searches',
            'watch_sessions', 'bookmark_events', 'tags', 'events'}
INTEGER_IDS = {'reviews', 'views', 'room_messages', 'payments'}
OPERATIONAL = ('login_attempts', 'password_resets', 'tmdb_jobs', 'tmdb_rows', 'tmdb_cache')
CATALOG_ENRICHMENT_VERSION = 2


def qi(name):
    return '"' + name.replace('"', '""') + '"'


def jd(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8*1024*1024), b''):
            result.update(chunk)
    return result.hexdigest()


def missing_metadata(value):
    if isinstance(value, bool):
        return False
    if value in (None, '', [], {}, 0):
        return True
    return isinstance(value, str) and value.strip().casefold() in ('—', '–', '-', 'n/a', 'нет данных', 'неизвестно', '0')


def tables(db, schema='main'):
    return [r[0] for r in db.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def columns(db, table, schema='main'):
    return [dict(r) for r in db.execute(f'PRAGMA {schema}.table_info({qi(table)})')]


def counts(db, schema='main'):
    return {t: db.execute(f'SELECT COUNT(*) FROM {schema}.{qi(t)}').fetchone()[0] for t in tables(db, schema)}


def unchanged_rows(db, exclude=()):
    """Check every original column of every original row, including BLOBs and auth data."""
    checked = {}
    for table in tables(db, 'protected'):
        if table in exclude:
            continue
        meta = columns(db, table, 'protected')
        keys = [r['name'] for r in meta if r['pk']]
        if not keys:
            raise ValueError(f'Cannot protect table without a primary key: {table}')
        join = ' AND '.join(f'p.{qi(k)} IS d.{qi(k)}' for k in keys)
        different = ' OR '.join(f'p.{qi(r["name"])} IS NOT d.{qi(r["name"])}' for r in meta)
        bad = db.execute(f'''SELECT 1 FROM protected.{qi(table)} p
            LEFT JOIN main.{qi(table)} d ON {join}
            WHERE d.{qi(keys[0])} IS NULL OR ({different}) LIMIT 1''').fetchone()
        if bad:
            raise ValueError(f'An existing row changed: {table}; rolling back')
        checked[table] = db.execute(f'SELECT COUNT(*) FROM protected.{qi(table)}').fetchone()[0]
    return checked


def base36(value):
    out = ''
    while value:
        value, rem = divmod(value, 36)
        out = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'[rem] + out
    return (out or '0').rjust(5, '0')


def merge_dataset(source, destination, *, backup_dir=None, progress=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or not source.is_file() or not destination.is_file():
        raise ValueError('Source and destination must be different existing database files.')
    notify = progress or (lambda message: None)
    source_stat = (source.stat().st_size, source.stat().st_mtime_ns)
    notify('Hashing the read-only source')
    source_hash = digest(source)
    prefix = 'fixture_' + source_hash[:12] + ':'
    started = time.perf_counter()
    with closing(sqlite3.connect(destination, timeout=60, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA cache_size=-65536')
        db.execute('ATTACH DATABASE ? AS fixture', (source.as_uri()+'?mode=ro',))
        if 'dataset_imports' in tables(db):
            receipt = db.execute('SELECT report FROM dataset_imports WHERE source_sha256=?', (source_hash,)).fetchone()
            if receipt:
                report = json.loads(receipt[0])
                report['already_imported'] = True
                return report
        source_counts = counts(db, 'fixture')
        for table in OPERATIONAL:
            if source_counts.get(table, 0):
                raise ValueError(f'Fixture contains operational/private records in {table}; not a supported test dataset.')
        source_films = {r['id']: dict(r) for r in db.execute('SELECT id,data,imdb_id,tmdb_id,tmdb_kind FROM fixture.films')}
        target_films = {r['id']: dict(r) for r in db.execute('SELECT id,data,imdb_id,tmdb_id,tmdb_kind FROM main.films')}
        for film_id, film in source_films.items():
            other = target_films.get(film_id)
            identity = lambda r: (json.loads(r['data'])['title'], r['imdb_id'], r['tmdb_id'], r['tmdb_kind'])
            if not other or identity(film) != identity(other):
                raise ValueError(f'Fixture film {film_id} does not match the working catalog; nothing imported.')
        before = counts(db)
        backup_dir = Path(backup_dir or destination.parent/'backups')
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = backup_dir/f'{destination.stem}-before-fixture-{timestamp}.sqlite3'
        try:
            db.execute('BEGIN IMMEDIATE')
            # A separate reader can back up the last committed WAL snapshot while this
            # writer lock prevents another writer from changing the protected baseline.
            notify('Creating a consistent backup and protecting existing records')
            with closing(sqlite3.connect(destination.resolve().as_uri()+'?mode=ro', uri=True)) as reader:
                with closing(sqlite3.connect(backup)) as saved:
                    reader.backup(saved)
            db.execute('ATTACH DATABASE ? AS protected', (backup.resolve().as_uri()+'?mode=ro',))
            db.execute('''CREATE TABLE IF NOT EXISTS dataset_imports(
                source_sha256 TEXT PRIMARY KEY, imported_at TEXT NOT NULL, report TEXT NOT NULL CHECK(json_valid(report)))''')
            # Recheck after acquiring the lock, in case a second importer won the race.
            existing = db.execute('SELECT report FROM dataset_imports WHERE source_sha256=?', (source_hash,)).fetchone()
            if existing:
                db.rollback()
                report = json.loads(existing[0]); report['already_imported'] = True
                return report
            user_offset = db.execute('SELECT COALESCE(MAX(id),0) FROM users').fetchone()[0]
            id_offsets = {t: db.execute(f'SELECT COALESCE(MAX(id),0) FROM {qi(t)}').fetchone()[0] for t in INTEGER_IDS}
            source_user_ids = {r[0] for r in db.execute('SELECT id FROM fixture.users')}
            if not source_user_ids or min(source_user_ids) < 1:
                raise ValueError('Fixture user IDs must be positive integers.')
            for table in ORDER:
                for col in ('user_id', 'host_id'):
                    if col in {r['name'] for r in columns(db, table, 'fixture')}:
                        if db.execute(f'SELECT 1 FROM fixture.{qi(table)} WHERE {qi(col)}<1 LIMIT 1').fetchone():
                            raise ValueError(f'Invalid fixture account reference in {table}.{col}')
            used_rooms = {r[0] for r in db.execute('SELECT code FROM rooms')}
            room_map = {}
            number = 0
            for row in db.execute('SELECT code FROM fixture.rooms ORDER BY code'):
                while True:
                    candidate = 'T'+base36(number); number += 1
                    if len(candidate) != 6:
                        raise ValueError('Room code namespace exhausted.')
                    if candidate not in used_rooms:
                        break
                room_map[row[0]] = candidate
                used_rooms.add(candidate)
            episode_map = {}
            for row in db.execute('''SELECT s.id, d.id AS existing FROM fixture.episodes s
                LEFT JOIN main.episodes d ON d.movie_id=s.movie_id AND d.season=s.season AND d.episode=s.episode'''):
                episode_map[row['id']] = row['existing'] or prefix+row['id']

            def text_id(value):
                return prefix+str(value) if value is not None and value != '' else value

            def room_id(value):
                return room_map[value] if value else value

            def episode_id(value):
                return episode_map[value] if value else value

            def profile(data):
                value = json.loads(data)
                if any(i not in source_user_ids for i in value.get('friends', [])):
                    raise ValueError('Unknown fixture friend reference')
                value['friends'] = [i+user_offset for i in value.get('friends', [])]
                value['rooms'] = {room_id(k): v for k, v in value.get('rooms', {}).items()}
                for clip in value.get('clips', []):
                    clip['id'] = text_id(clip['id'])
                return jd(value)

            def room_state(data):
                value = json.loads(data)
                if value.get('by') is not None:
                    if value['by'] not in source_user_ids:
                        raise ValueError('Unknown fixture room actor')
                    value['by'] += user_offset
                if value.get('catalog_episode_id'):
                    value['catalog_episode_id'] = episode_id(value['catalog_episode_id'])
                return jd(value)

            def payload(data):
                value = json.loads(data)
                for key in ('bookmark_event_id', 'impression_id', 'click_id', 'clip_id', 'search_id', 'request_id', 'session_id'):
                    if value.get(key):
                        value[key] = text_id(value[key])
                if value.get('episode_id'):
                    value['episode_id'] = episode_id(value['episode_id'])
                for key in ('room_code', 'room_id'):
                    if value.get(key):
                        value[key] = room_id(value[key])
                if value.get('review_id') is not None:
                    value['review_id'] += id_offsets['reviews']
                return jd(value)

            for name, fn in [('fixture_text', text_id), ('fixture_room', room_id), ('fixture_episode', episode_id),
                             ('fixture_profile', profile), ('fixture_room_state', room_state), ('fixture_payload', payload)]:
                db.create_function(name, 1, fn)
            inserted = {}
            for table in ORDER:
                target_columns = {r['name'] for r in columns(db, table)}
                names = [r['name'] for r in columns(db, table, 'fixture') if r['name'] in target_columns]
                expressions = []
                for name in names:
                    value = 's.'+qi(name)
                    if name == 'user_id' or name == 'host_id' or table == 'users' and name == 'id':
                        value += '+'+str(user_offset)
                    elif name == 'id' and table in TEXT_IDS:
                        value = f'fixture_text({value})'
                    elif name == 'id' and table in INTEGER_IDS:
                        value += '+'+str(id_offsets[table])
                    elif name == 'id' and table == 'episodes' or name == 'episode_id':
                        value = f'fixture_episode({value})'
                    elif name in ('room_code', 'code'):
                        value = f'fixture_room({value})'
                    elif name in ('request_id', 'session_id', 'impression_id'):
                        value = f'fixture_text({value})'
                    elif table == 'watch_sessions' and name == 'view_id':
                        value += '+'+str(id_offsets['views'])
                    elif table == 'users' and name == 'role':
                        value = "'USER'"  # A fixture must never introduce another site administrator.
                    elif table == 'users' and name == 'email':
                        fallback = 'fixture-'+source_hash[:12]+'-'
                        value = f'''CASE WHEN EXISTS(SELECT 1 FROM main.users u WHERE lower(u.email)=lower(s.email))
                            THEN '{fallback}' || s.id || '@example.invalid' ELSE s.email END'''
                    elif table == 'user_state' and name == 'data':
                        value = f'fixture_profile({value})'
                    elif table == 'rooms' and name == 'state':
                        value = f'fixture_room_state({value})'
                    elif table == 'events' and name == 'payload':
                        value = f'fixture_payload({value})'
                    expressions.append(value)
                where = ''
                if table == 'episodes':
                    where = ''' WHERE NOT EXISTS(SELECT 1 FROM main.episodes e
                        WHERE e.movie_id=s.movie_id AND e.season=s.season AND e.episode=s.episode)'''
                notify(f'Importing {table}: {source_counts[table]:,} source rows')
                db.execute(f'INSERT INTO main.{qi(table)} ({",".join(map(qi,names))}) '
                           f'SELECT {",".join(expressions)} FROM fixture.{qi(table)} s{where}')
                inserted[table] = db.execute('SELECT changes()').fetchone()[0]
                if table != 'episodes' and inserted[table] != source_counts[table]:
                    raise ValueError(f'Incomplete import: {table}')
            # Catalog and media data already present are authoritative. Only absent
            # source/cache rows may be inserted; no live metadata or video is replaced.
            for table, keys in [('film_sources', ('movie_id', 'source')), ('media_info', ('key',))]:
                names = [r['name'] for r in columns(db, table, 'fixture')]
                join = ' AND '.join(f'd.{qi(k)} IS s.{qi(k)}' for k in keys)
                db.execute(f'INSERT INTO main.{qi(table)} ({",".join(map(qi,names))}) '
                           f'SELECT {",".join("s."+qi(n) for n in names)} FROM fixture.{qi(table)} s '
                           f'WHERE NOT EXISTS(SELECT 1 FROM main.{qi(table)} d WHERE {join})')
                inserted[table] = db.execute('SELECT changes()').fetchone()[0]
            notify('Checking foreign keys, logical references and unchanged original data')
            if db.execute('PRAGMA main.foreign_key_check').fetchone():
                raise ValueError('Foreign-key check failed; rolling back.')
            protected = unchanged_rows(db)
            if source_stat != (source.stat().st_size, source.stat().st_mtime_ns):
                raise ValueError('Source changed during import; rolling back.')
            # Native user_state JSON and ratings remain two synchronized views of the same votes.
            bad_ratings = db.execute('''SELECT 1 FROM main.user_state p, json_each(p.data,'$.ratings') j
                LEFT JOIN main.ratings r ON r.user_id=p.user_id AND r.movie_id=CAST(j.key AS INTEGER)
                WHERE p.user_id>? AND (r.score IS NULL OR r.score<>j.value) LIMIT 1''', (user_offset,)).fetchone()
            if bad_ratings:
                raise ValueError('Profile ratings differ from rating rows.')
            checks = {
                'movie_references': f'''SELECT 1 FROM ratings r LEFT JOIN films f ON f.id=r.movie_id WHERE r.user_id>{user_offset} AND f.id IS NULL LIMIT 1''',
                'session_view_consistency': '''SELECT 1 FROM watch_sessions s JOIN views v ON v.id=s.view_id
                    WHERE s.user_id<>v.user_id OR s.movie_id<>v.movie_id OR s.started_at<>v.started_at OR s.room_code IS NOT v.room_code LIMIT 1''',
                'click_impression_consistency': '''SELECT 1 FROM clicks c JOIN impressions i ON i.id=c.impression_id
                    WHERE c.user_id<>i.user_id OR c.movie_id<>i.movie_id OR c.request_id<>i.request_id LIMIT 1''',
                'room_state_user': f'''SELECT 1 FROM rooms r LEFT JOIN users u ON u.id=json_extract(r.state,'$.by')
                    WHERE r.host_id>{user_offset} AND json_extract(r.state,'$.by') IS NOT NULL AND u.id IS NULL LIMIT 1''',
                'profile_friends': '''SELECT 1 FROM user_state p,json_each(p.data,'$.friends') j
                    LEFT JOIN users u ON u.id=j.value WHERE u.id IS NULL LIMIT 1''',
                'profile_rooms': '''SELECT 1 FROM user_state p,json_each(p.data,'$.rooms') j
                    LEFT JOIN rooms r ON r.code=j.key WHERE r.code IS NULL LIMIT 1''',
                'profile_clips': '''SELECT 1 FROM user_state p,json_each(p.data,'$.clips') j
                    LEFT JOIN films f ON f.id=json_extract(j.value,'$.movieId') WHERE f.id IS NULL LIMIT 1''',
            }
            for name, sql in checks.items():
                if db.execute(sql).fetchone():
                    raise ValueError(f'Logical reference check failed: {name}')
            notify('Checking SQLite integrity before commit')
            integrity = db.execute('PRAGMA main.integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise ValueError(f'SQLite integrity check failed: {integrity}')
            report = dict(source=str(source), destination=str(destination), source_sha256=source_hash,
                          source_read_only=True, source_counts=source_counts, backup=str(backup.resolve()),
                          user_id_offset=user_offset, integer_id_offsets=id_offsets, text_id_prefix=prefix,
                          imported_rows=inserted, before_counts=before, after_counts=counts(db),
                          protected_original_rows=protected, all_original_rows_unchanged=True,
                          catalog_preserved=True, imported_admins_downgraded_to_user=True,
                          foreign_keys='ok', integrity=integrity, logical_checks=list(checks),
                          already_imported=False, elapsed_seconds=round(time.perf_counter()-started, 2))
            report['after_counts']['dataset_imports'] += 1
            db.execute('INSERT INTO dataset_imports VALUES(?,?,?)', (source_hash, datetime.now(timezone.utc).isoformat(), jd(report)))
            db.commit()
            notify('Merge committed successfully')
            return report
        except BaseException:
            db.rollback()
            raise


def enrich_catalog_gaps(source, destination, report, *, progress=None):
    """Copy fixture metadata only into absent/empty fields; retain live movie IDs and sources."""
    notify = progress or (lambda message: None)
    if report.get('catalog_enrichment', {}).get('version', 0) >= CATALOG_ENRICHMENT_VERSION:
        return report
    source, destination = Path(source).resolve(), Path(destination).resolve()
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    metadata_backup = Path(report['backup']).parent/f'catalog-before-enrichment-{timestamp}.jsonl'
    with closing(sqlite3.connect(destination, timeout=60, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('ATTACH DATABASE ? AS fixture', (source.as_uri()+'?mode=ro',))
        db.execute('ATTACH DATABASE ? AS protected', (Path(report['backup']).resolve().as_uri()+'?mode=ro',))
        try:
            db.execute('BEGIN IMMEDIATE')
            receipt = db.execute('SELECT report FROM dataset_imports WHERE source_sha256=?', (report['source_sha256'],)).fetchone()
            if not receipt:
                raise ValueError('No matching import receipt; cannot enrich catalog.')
            current = json.loads(receipt[0])
            if current.get('catalog_enrichment', {}).get('version', 0) >= CATALOG_ENRICHMENT_VERSION:
                db.rollback()
                return current
            notify('Filling only missing movie metadata; keeping existing values and video sources')
            filled = {}
            changed = 0
            rows = db.execute('''SELECT a.id,a.data,a.art,a.updated_at,b.data AS source_data,b.art AS source_art
                FROM main.films a JOIN fixture.films b ON a.id=b.id ORDER BY a.id''').fetchall()
            with metadata_backup.open('x', encoding='utf-8') as saved:
                for row in rows:
                    updated = {}
                    for column in ('data', 'art'):
                        old, incoming = json.loads(row[column]), json.loads(row['source_'+column])
                        value = dict(old)
                        for key, item in incoming.items():
                            if column == 'data' and key in ('id', 'is_hidden'):
                                continue
                            if missing_metadata(old.get(key)) and not missing_metadata(item):
                                value[key] = item
                                name = column+'.'+key
                                filled[name] = filled.get(name, 0)+1
                        updated[column] = value
                    if updated['data'] == json.loads(row['data']) and updated['art'] == json.loads(row['art']):
                        continue
                    saved.write(jd({k: row[k] for k in ('id', 'data', 'art', 'updated_at')})+'\n')
                    db.execute('UPDATE films SET data=?,art=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                               (jd(updated['data']), jd(updated['art']), row['id']))
                    changed += 1
            # Existing accounts and their full history must still equal the pre-import backup.
            unchanged_rows(db, exclude=('films',))
            other_columns = [r['name'] for r in columns(db, 'films') if r['name'] not in ('data', 'art', 'updated_at')]
            different = ' OR '.join(f'p.{qi(c)} IS NOT d.{qi(c)}' for c in other_columns)
            if db.execute(f'''SELECT 1 FROM protected.films p LEFT JOIN main.films d ON d.id=p.id
                              WHERE d.id IS NULL OR ({different}) LIMIT 1''').fetchone():
                raise ValueError('Movie identity or video source changed; rolling back metadata enrichment.')
            if current.get('catalog_enrichment'):
                report['catalog_enrichment_history'] = current.get('catalog_enrichment_history', [])+[current['catalog_enrichment']]
            report['catalog_enrichment'] = dict(version=CATALOG_ENRICHMENT_VERSION, films_updated=changed, filled_fields=filled,
                metadata_backup=str(metadata_backup.resolve()), metadata_backup_sha256=digest(metadata_backup),
                existing_known_values_preserved=True, original_accounts_and_history_unchanged=True,
                movie_ids_and_video_sources_unchanged=True)
            report['all_original_rows_unchanged_before_catalog_enrichment'] = report.pop('all_original_rows_unchanged', True)
            report['original_accounts_and_history_unchanged'] = True
            db.execute('UPDATE dataset_imports SET report=? WHERE source_sha256=?', (jd(report), report['source_sha256']))
            db.commit()
            notify('Catalog metadata committed; original accounts and history verified unchanged')
            return report
        except BaseException:
            db.rollback()
            raise


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=root/'datasets/cinema_project_v3/cinema_test.sqlite3')
    parser.add_argument('--database', type=Path, default=root/'.local/cinema.sqlite3')
    parser.add_argument('--report', type=Path, default=root/'.local/test_dataset_import.json')
    args = parser.parse_args()
    report = merge_dataset(args.source, args.database, progress=lambda s: print(s, flush=True))
    report = enrich_catalog_gaps(args.source, args.database, report, progress=lambda s: print(s, flush=True))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(jd(dict(already_imported=report['already_imported'], counts=report['after_counts'], report=str(args.report.resolve()))), flush=True)


if __name__ == '__main__':
    main()
