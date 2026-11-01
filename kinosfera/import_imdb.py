"""Command-line IMDb import using the same service as the administrator UI."""
import argparse
import json
from pathlib import Path
import sys

from database import Store
from imdb_import import DatasetCache, MAX_LIST_BYTES, read_list, run_import

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description='Импорт каталога из официальных бесплатных наборов IMDb')
    parser.add_argument('list', type=Path, help='CSV в UTF-8: imdb_id,title,year')
    parser.add_argument('--database', type=Path, default=ROOT / '.local/cinema.sqlite3')
    parser.add_argument('--cache', type=Path, default=ROOT / '.local/imdb')
    parser.add_argument('--draft', action='store_true', help='Сохранить новые карточки как черновики')
    parser.add_argument('--credits', action='store_true', help='Также получить актёров и режиссёров (дополнительные крупные загрузки)')
    parser.add_argument('--refresh', action='store_true', help='Заново скачать используемые наборы IMDb')
    parser.add_argument('--report', type=Path, help='Сохранить отчёт в JSON')
    args = parser.parse_args()
    last = {'megabytes':-1}

    def progress(**values):
        if 'message' in values:
            print(values['message'], flush=True)
        if 'downloaded' in values and values['downloaded']:
            mb = values['downloaded'] // (10 * 1024 ** 2)
            if mb != last['megabytes']:
                last['megabytes'] = mb
                print(f'  {values["downloaded"] / 1048576:.1f} MiB', flush=True)
    try:
        with args.list.open('rb') as file:
            entries = read_list(file.read(MAX_LIST_BYTES + 1))
        results = run_import(Store(args.database), entries, DatasetCache(args.cache, progress),
                             publish=not args.draft, credits=args.credits, refresh=args.refresh)
        for row in results:
            print(f'{row["line"]}: {row["input"]} — {row["status"]}: {row["message"]}', flush=True)
        if args.report:
            args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if all(row['status'] in ('added','updated') for row in results) else 2
    except (OSError, ValueError) as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Импорт прерван.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
