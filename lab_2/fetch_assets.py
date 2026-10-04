"""Download the static artwork used by the interface demo (no runtime API)."""
from pathlib import Path
from urllib.request import urlretrieve

ASSETS = {
    'interstellar-backdrop': '/pbrkL804c8yAv3zBZR4QPEafpAR.jpg',
    'interstellar': '/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg',
    'inception': '/oYuLEt3zVCKq57qu2F8dT7NIa6f.jpg',
    'green-mile': '/8VG8fDNiy50H4FedGwdSVUPoaJe.jpg',
    'dark-knight': '/qJ2tW6WMUDux911r6m7haRef0WH.jpg',
    'lotr': '/6oom5QYQ2yQTMJIbnvbkBL9cHo6.jpg',
    'pulp-fiction': '/d5iIlFn5s0ImszYzBPb8JPIfbXD.jpg',
    'matrix': '/f89U3ADr1oiB1s9GkdPOEpXUk5H.jpg',
    'forrest-gump': '/arw2vcBveWOVZr6pxd9XTd1TdQa.jpg',
    'dune': '/d5NXSklXo0qyIYkgV94XAgMIckC.jpg',
    'arrival': '/x2FJsf1ElAgr63Y3PNPtJrcmpoe.jpg',
    'blade-runner': '/gajva2L0rPYkEWjzgFlBXCAVBE5.jpg',
    'shutter-island': '/nrmXQ0zcZUL8jFLrakWc90IR8z9.jpg',
    'dune-backdrop': '/jYEW5xZkZk2WTrdbMGAPFuBqbDc.jpg',
}

if __name__ == '__main__':
    directory = Path(__file__).parent / 'static' / 'images'
    directory.mkdir(parents=True, exist_ok=True)
    for name, remote in ASSETS.items():
        target = directory / f'{name}.jpg'
        if target.exists():
            continue
        size = 'w1280' if name.endswith('backdrop') else 'w500'
        try:
            urlretrieve(f'https://image.tmdb.org/t/p/{size}{remote}', target)
            print(f'OK {name}', flush=True)
        except Exception as error:
            print(f'FAILED {name}: {error}', flush=True)
