"""Translate application films into a reusable mixdex snapshot."""
import hashlib
import json
import logging
import math
from pathlib import Path
import threading

from mixdex_catalog import CatalogSnapshot

LOG = logging.getLogger(__name__)


def labels(values):
    return tuple(sorted({str(v).strip().casefold() for v in values or [] if str(v).strip()}))


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else None


class CatalogIndex:
    """One immutable snapshot at a time; readers can finish on the previous one."""
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else None
        self._lock = threading.Lock()
        self._key = None
        self._snapshot = None
        self.builds = 0

    def get(self, rows, namespace='default'):
        rows = sorted((r for r in rows if not r['film'].is_hidden), key=lambda r: r['film'].id)
        if not rows:
            return None
        ids = [r['film'].id for r in rows]
        numeric = [[number(r['film'].year), number(r['film'].duration_min)] for r in rows]
        categorical = [[r['film'].kind or None, labels(r['film'].genres)] for r in rows]
        weights = [int(any(row[j] is not None for row in numeric)) for j in range(2)] + [1, 1]
        geometry = dict(ids=ids, numeric=numeric, categorical=categorical, weights=weights)
        version = hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()
        key = (str(namespace), version)
        with self._lock:
            if key == self._key:
                return self._snapshot
            path = None
            snapshot = None
            if self.directory:
                name = hashlib.sha256(str(namespace).encode()).hexdigest()[:20]
                path = self.directory / f'{name}.json'
                if path.is_file():
                    try:
                        cached = CatalogSnapshot.load(path)
                        if cached.version == version:
                            snapshot = cached
                    except (OSError, ValueError, KeyError):
                        LOG.warning('Rebuilding unreadable recommendation snapshot: %s', path)
            if snapshot is None:
                snapshot = CatalogSnapshot.build(
                    **geometry, numeric_names=['year', 'duration_min'],
                    categorical_names=['kind', 'genres'], categorical_set_columns=[1],
                    numeric_types=['Discrete', 'Continuous'], impute=True, version=version,
                )
                self.builds += 1
                if path:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        snapshot.save(path)
                    except OSError:
                        LOG.warning('Recommendation snapshot remains in memory; save failed', exc_info=True)
            self._key, self._snapshot = key, snapshot
            return snapshot
