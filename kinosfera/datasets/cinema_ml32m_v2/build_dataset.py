"""Create one business schema from v1; existing observations always win.

Reads only the standalone v1 files. Never opens the application database.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
FILES = {}
DROP = {'source', 'is_synthetic', 'is_synthetic_profile', 'profile_source',
        'metadata_provenance', 'synthetic_fields', 'simulated_playback_available',
        'playback_source', 'source_rating_0_5_to_5'}
RENAME = {'real_rating_count': 'rating_count', 'synthetic_event_count': 'event_count',
          'last_real_rating_at': 'last_rating_at', 'first_source_activity_at': 'first_activity_at',
          'episodes_modeled': 'episode_count'}
RATING_COLUMNS = ['rating_id', 'user_id', 'movie_id', 'score', 'rated_at', 'source_movie_id', 'split']
STAT_COLUMNS = ['movie_id', 'rating_count', 'rating_mean', 'impressions', 'clicks', 'sessions',
                'completed_sessions', 'total_watched_seconds', 'active_watchlist_users', 'active_not_interested_users']


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def dump(name, value):
    (HERE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def read(folder, name):
    with gzip.open(folder / 'data' / (name + '.csv.gz'), 'rt', encoding='utf-8', newline='') as f:
        yield from csv.DictReader(f)


def columns(original):
    return [RENAME.get(c, c) for c in original if c not in DROP]


def project(row):
    return {RENAME.get(c, c): value for c, value in row.items() if c not in DROP}


@contextmanager
def writer(name, fields):
    target = HERE / 'data' / (name + '.csv.gz')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.gz.tmp')
    count = 0
    with temporary.open('wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0, compresslevel=4) as gz, io.TextIOWrapper(gz, encoding='utf-8', newline='') as stream:
        out = csv.DictWriter(stream, fields, lineterminator='\n')
        out.writeheader()

        def emit(row):
            nonlocal count
            out.writerow(row)
            count += 1
        yield emit
    temporary.replace(target)
    FILES[name] = dict(file='data/' + target.name, columns=fields, rows=count,
                       bytes=target.stat().st_size, sha256=sha(target))
    print(f'Written {name}: {count:,}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=HERE.parent / 'cinema_ml32m_v1')
    args = parser.parse_args()
    source = args.source.resolve()
    assert source != HERE and (source / 'schema.json').is_file()
    manifest = load(source / 'manifest.json')
    schema = load(source / 'schema.json')
    assert len(schema['files']) == 25
    for name, info in manifest['files'].items():
        assert sha(source / info['file']) == info['sha256'], f'Changed source file: {name}'
    print('Verified all v1 source checksums.', flush=True)

    candidates = defaultdict(dict)
    for row in read(source, 'ratings_synthetic'):
        uid, mid = row['user_id'], int(row['movie_id'])
        assert mid not in candidates[uid]
        candidates[uid][mid] = row
    candidate_count = sum(map(len, candidates.values()))
    accepted = {}
    conflicts = 0
    movie_ratings = defaultdict(Counter)
    user_ratings = Counter()
    last_rating = Counter()
    first_activity = {}
    original_rows = 0
    preserved_digest = hashlib.sha256()

    def activity(uid, at_ms):
        first_activity[uid] = min(first_activity.get(uid, at_ms), at_ms)

    # Only one user's original rows are buffered (at most the catalog size).
    with writer('ratings', RATING_COLUMNS) as emit:
        def flush(uid, group):
            nonlocal conflicts
            existing = {int(r['movie_id']) for r in group}
            assert len(existing) == len(group)
            extras = []
            for mid, row in candidates.pop(uid, {}).items():
                if mid in existing:
                    conflicts += 1
                    continue
                # Do not invent an external MovieLens ID or an official split.
                added = dict(rating_id=row['rating_id'], user_id=uid, movie_id=row['movie_id'],
                             score=row['score'], rated_at=row['rated_at'], source_movie_id='', split='')
                extras.append(added)
                accepted[added['rating_id']] = (uid, mid, int(added['score']))
            merged = group + extras
            merged.sort(key=lambda r: (int(r['rated_at']), int(r['movie_id'])))
            for row in merged:
                mid, score, at = int(row['movie_id']), int(row['score']), int(row['rated_at'])
                movie_ratings[mid].update(count=1, total=score)
                user_ratings[uid] += 1
                last_rating[uid] = max(last_rating[uid], at)
                activity(uid, at * 1000)
                emit(row)
        current = None
        group = []
        for row in read(source, 'ratings'):
            uid = row['user_id']
            if current is not None and uid != current:
                flush(current, group)
                group = []
            current = uid
            projected = {c: row[c] for c in RATING_COLUMNS}
            preserved_digest.update((json.dumps([projected[c] for c in RATING_COLUMNS], ensure_ascii=False, separators=(',', ':'))+'\n').encode('utf-8'))
            group.append(projected)
            original_rows += 1
            if original_rows % 1_000_000 == 0:
                print(f'Preserved {original_rows:,} original ratings.', flush=True)
        if current is not None:
            flush(current, group)
    assert not candidates
    assert original_rows == manifest['files']['ratings']['rows']
    assert original_rows + len(accepted) == FILES['ratings']['rows']
    assert candidate_count == len(accepted) + conflicts

    metadata_sources = {}
    for name, original_columns in schema['files'].items():
        if name in {'ratings', 'ratings_synthetic', 'users', 'user_activity', 'movie_stats', 'events'}:
            continue
        with writer(name, columns(original_columns)) as emit:
            for row in read(source, name):
                if name == 'movies':
                    # The v1 metadata already use one value per field and fill only gaps.
                    # Preserve every value; move lineage out of the business columns.
                    metadata_sources[row['movie_id']] = json.loads(row['metadata_provenance'])
                if name == 'tags':
                    activity(row['user_id'], int(row['created_at']) * 1000)
                emit(project(row))

    event_counts = Counter()
    last_event = Counter()
    event_types = Counter()
    removed_events = 0
    rating_events_seen = set()
    with writer('events', columns(schema['files']['events'])) as emit:
        number = 0
        for row in read(source, 'events'):
            payload = None
            if row['event_type'] == 'rating_set':
                payload = json.loads(row['payload'])
                rid = payload.get('rating_id')
                # Discard generated updates to real ratings and obsolete intermediate
                # generated snapshots. Every remaining rating event has a canonical row.
                if rid not in accepted:
                    removed_events += 1
                    continue
                assert (row['user_id'], int(row['movie_id']), int(payload['score'])) == accepted[rid]
                assert rid not in rating_events_seen
                rating_events_seen.add(rid)
            if row['event_type'] == 'onboarding_preferences':
                payload = json.loads(row['payload'])
                payload.pop('derived_from', None)
            if payload is not None:
                row['payload'] = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            number += 1
            row['event_id'] = f'ev_{number}'
            uid, at = row['user_id'], int(row['event_at_ms'])
            activity(uid, at)
            last_event[uid] = max(last_event[uid], at)
            event_counts[uid] += 1
            event_types[row['event_type']] += 1
            emit(project(row))
            if number % 1_000_000 == 0:
                print(f'Reconciled {number:,} events.', flush=True)
    assert rating_events_seen == set(accepted)

    with writer('users', columns(schema['files']['users'])) as emit:
        for row in read(source, 'users'):
            row['real_rating_count'] = str(user_ratings[row['user_id']])
            emit(project(row))
    with writer('user_activity', columns(schema['files']['user_activity'])) as emit:
        for row in read(source, 'user_activity'):
            uid = row['user_id']
            row.update(first_source_activity_at=str(first_activity[uid] // 1000),
                       last_real_rating_at=str(last_rating[uid]), last_event_at_ms=str(last_event[uid]),
                       days_since_last_event=str(round((manifest['as_of']*1000-last_event[uid])/86400000, 6)),
                       real_rating_count=str(user_ratings[uid]), synthetic_event_count=str(event_counts[uid]))
            emit(project(row))
    with writer('movie_stats', STAT_COLUMNS) as emit:
        for row in read(source, 'movie_stats'):
            stats = movie_ratings[int(row['movie_id'])]
            projected = {c: row[c] for c in STAT_COLUMNS if c not in {'rating_count', 'rating_mean'}}
            projected['rating_count'] = str(stats['count'])
            projected['rating_mean'] = str(round(stats['total']/stats['count'], 4)) if stats['count'] else ''
            emit(projected)

    removed_columns = {name: [c for c in original if c in DROP] for name, original in schema['files'].items() if name != 'ratings_synthetic'}
    provenance = dict(
        source_dataset='cinema_ml32m_v1', source_manifest_sha256=sha(source/'manifest.json'),
        policy='One canonical value; preserve existing source observations; fill missing pairs only.',
        original_ratings_preserved=original_rows, filled_rating_pairs=len(accepted),
        generated_candidates_skipped_existing_rating=conflicts, removed_incompatible_or_obsolete_rating_events=removed_events,
        original_rating_projection_sha256=preserved_digest.hexdigest(),
        original_rating_projection_columns=RATING_COLUMNS,
        original_rating_id_prefix='r_', filled_rating_id_prefix='sr_',
        metadata_values_changed=0, movie_field_sources=metadata_sources,
        behavior='Existing test simulation from v1; not observed behavior of MovieLens users.',
        accounts='Anonymous MovieLens identifiers with generated names and example.invalid email addresses.',
        metadata='TMDB cache and official IMDb snapshot; missing values filled in v1. Kinopoisk ratings/votes are generated test values.',
        non_applicable_fields='Do not invent external IDs, a franchise, a video source or unchosen preferences.',
        removed_business_columns=removed_columns, renamed_columns=RENAME)
    dump('provenance.json', provenance)
    dump('schema.json', dict(format=schema['format'], times=schema['times'],
         files={name: info['columns'] for name, info in FILES.items()}, keys=schema['keys'], nulls=schema['nulls'],
         notes='One ratings table and one set of rating aggregates. Generation lineage is documentation, not business columns.'))
    dump('manifest.json', dict(dataset='cinema_ml32m_v2', version=2, users=manifest['users'],
         movies=len(manifest['allowed_catalog_movie_ids']), as_of=manifest['as_of'], seed=manifest['seed'],
         files=FILES, allowed_catalog_movie_ids=manifest['allowed_catalog_movie_ids'],
         tables=len(FILES), rows=sum(f['rows'] for f in FILES.values()),
         source_manifest_sha256=sha(source/'manifest.json'),
         rating_fill=dict(preserved=original_rows, added=len(accepted), skipped_existing=conflicts),
         event_types=dict(event_types), application_database_accessed=False))
    shutil.copyfile(source/'MOVIELENS_SOURCE_README.txt', HERE/'MOVIELENS_SOURCE_README.txt')
    assert len(FILES) == 24
    assert all(not any(word in field for word in ('synthetic', 'simulated', 'real_rating', 'modeled'))
               for info in FILES.values() for field in info['columns'])
    print(json.dumps({'tables':len(FILES), 'fields':sum(len(v['columns']) for v in FILES.values()),
                      'ratings':FILES['ratings']['rows'], 'original_preserved':original_rows,
                      'added_only_missing':len(accepted), 'skipped_existing':conflicts,
                      'application_database_accessed':False}), flush=True)


if __name__ == '__main__':
    main()
