"""Start the site on a separate working database; never overwrite a supplied sample."""
import argparse
import os
from pathlib import Path
import shutil


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='Локальный запуск КиноСферы с рекомендациями')
    parser.add_argument('--sample', type=Path, default=root.parent/'.data/cinema_sample.sqlite3')
    parser.add_argument('--database', type=Path, default=root/'.local/cinema-recommendations.sqlite3')
    parser.add_argument('--port', type=int, default=5057)
    args = parser.parse_args()
    database = args.database.resolve()
    if args.sample.exists() and args.sample.resolve() == database:
        parser.error('Рабочая БД должна отличаться от исходного образца.')
    if not database.exists():
        database.parent.mkdir(parents=True, exist_ok=True)
        if args.sample.is_file():
            shutil.copyfile(args.sample, database)
            print('Создана рабочая копия мини-БД.', flush=True)
    os.environ['CINEMA_DATABASE'] = str(database)
    from app import app
    print(f'КиноСфера: http://127.0.0.1:{args.port}/', flush=True)
    app.run(host='127.0.0.1', port=args.port, threaded=True, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
