"""Offline 20,000 distinct films through the real durable importer; no public API calls."""
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from database import Store
from tmdb_import import ImportJobs, Settings, read_list


class BulkClient:
    def __init__(self,*args,**kwargs):
        self.checkpoint = kwargs['checkpoint']

    def get(self,path,**params):
        self.checkpoint()
        key = int(path.rsplit('/',1)[1])
        return dict(id=key,title=f'Фильм {key}',original_title=f'Movie {key}',release_date='2020-01-01',runtime=100,
                    overview='Тестовое описание для проверки массового импорта.',genres=[{'name':'Драма'}],vote_average=7.0,vote_count=10)


def main():
    data = ('tmdb_id,type\n'+''.join(f'{i},movie\n' for i in range(1,20001))).encode()
    entries = read_list(data)
    with tempfile.TemporaryDirectory(dir=ROOT/'.local') as tmp:
        store = Store(Path(tmp)/'catalog.sqlite3')
        settings = Settings(Path(tmp)/'settings.json'); settings.save('benchmark-test')
        jobs = ImportJobs(store,settings,BulkClient)
        started = time.monotonic()
        token = jobs.start(entries)
        while True:
            job = jobs.status(token)
            if job['state'] != 'running': break
            print(f'Processed {job["completed"]}/20000',flush=True)
            time.sleep(10)
        assert job['state'] == 'done',job
        assert job['counts'] == {'added':20000},job['counts']
        assert len(job['results']) == 100 and job['pages'] == 200
        assert len(jobs.status(token,200)['results']) == 100
        with store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM films').fetchone()[0] == 20000
        resumed = ImportJobs(store,settings,BulkClient)
        assert resumed.status(token)['completed'] == 20000
        print(f'PASS: 20,000 distinct films, {time.monotonic()-started:.1f}s, persisted progress, 200 pages of results.',flush=True)


if __name__ == '__main__':
    main()
