"""CLI entry point for the same durable TMDB importer used by the admin UI."""
import argparse
from pathlib import Path
import time

from database import Store
from tmdb_import import ImportJobs, MAX_LIST_BYTES, Settings, read_list

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description='Импорт CSV из официального API TMDB; ключ: TMDB_READ_TOKEN или TMDB_API_KEY.')
    parser.add_argument('csv', type=Path, nargs='?')
    parser.add_argument('--resume', metavar='JOB_ID', help='Продолжить сохранённый импорт')
    parser.add_argument('--database', type=Path, default=ROOT/'.local/cinema.sqlite3')
    parser.add_argument('--draft', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    if bool(args.csv) == bool(args.resume):
        parser.error('Укажите CSV или --resume JOB_ID.')
    try:
        jobs = ImportJobs(Store(args.database), Settings(ROOT/'.local/tmdb-settings.json'))
        if args.resume:
            token = args.resume
            jobs.resume(token)
        else:
            with args.csv.open('rb') as stream:
                entries = read_list(stream.read(MAX_LIST_BYTES+1))
            token = jobs.start(entries, publish=not args.draft, refresh=args.refresh)
        print(f'Импорт: {token}', flush=True)
        last = None
        while True:
            job = jobs.status(token)
            progress = job['completed'], job['state'], job['message']
            if progress != last:
                print(f'{job["completed"]}/{job["total"]}: {job["message"]}', flush=True)
                last = progress
            if job['state'] != 'running':
                print(job['counts'])
                return 0 if job['state'] == 'done' else 1
            time.sleep(1)
    except KeyboardInterrupt:
        print('\nПрогресс сохранён. Продолжите импорт через --resume или в админ-панели.')
        return 130
    except (ValueError, OSError) as exc:
        print(str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
