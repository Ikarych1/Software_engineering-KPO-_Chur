"""Validate a copied sample database; never open the supplied file for writing."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('database', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'tests/artifacts/recommendations/sample.json')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='kinosfera-sample-') as directory:
        directory = Path(directory)
        db_path = directory/'working.sqlite3'
        shutil.copyfile(args.database, db_path)
        os.environ.update(CINEMA_DATABASE=str(db_path), CINEMA_CACHE_DIR=str(directory/'media'),
                          CINEMA_RECOMMENDATION_CACHE=str(directory/'index'), CINEMA_HWACCEL='off')
        from app import app
        from recommendation_store import RecommendationStore
        store = app.extensions['store']; repo = RecommendationStore(store)
        with store.connect() as db:
            users = [dict(r) for r in db.execute('SELECT id,session_version FROM users WHERE active=1')]
        timings, lists, personalized, unavailable = [], [], 0, 0

        def query(user):
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(user_id=user['id'], session_version=user['session_version'])
            start = time.perf_counter()
            response = client.get('/api/recommendations')
            elapsed = time.perf_counter()-start
            assert response.status_code == 200, response.text
            data = response.json
            ids = [i['id'] for i in data['items']]
            assert len(ids) <= 10 and len(ids) == len(set(ids))
            rows = store.films(); context = repo.context(user['id'], rows)
            assert not set(ids) & (context.declined | context.completed | set(context.ratings))
            assert set(ids) <= {r['film'].id for r in rows}
            return elapsed, data

        for user in users:
            elapsed, data = query(user)
            timings.append(elapsed); lists.append(tuple(i['id'] for i in data['items']))
            personalized += int(data['personalized'])
            unavailable += sum(not i['available'] for i in data['items'])
        with ThreadPoolExecutor(20) as pool:
            concurrent = list(pool.map(query, users[:20]))
        ordered = sorted(timings)
        report = dict(users=len(users), distinct_lists=len(set(lists)), personalized=personalized,
                      cards_without_source=unavailable, sequential_ms=dict(p50=ordered[len(ordered)//2]*1000,
                      p95=ordered[min(len(ordered)-1, int(len(ordered)*.95))]*1000, max=max(ordered)*1000),
                      concurrent_20_max_ms=max(v[0] for v in concurrent)*1000,
                      snapshot_builds=app.extensions['recommendations'].catalog.builds,
                      all_filters_valid=True, source_database_modified=False,
                      limitation='Flask test-client timings, not browser latency or the complete 20-user/video-room acceptance load')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
