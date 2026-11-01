"""Real fixture merge: collisions, JSON references, account preservation and rollback."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from merge_test_dataset import merge_dataset, enrich_catalog_gaps


def logical_hash(path):
    with closing(sqlite3.connect(path)) as db:
        return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()


class DatasetMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.source = ROOT/'datasets/cinema_sample/cinema_sample.sqlite3'
        self.target = self.directory/'working.sqlite3'
        shutil.copyfile(self.source, self.target)
        self.original_source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_source_hash)
        self.temp.cleanup()

    def test_complete_merge_preserves_all_original_rows_and_is_idempotent(self):
        # Real legacy rooms use a username or 'auto' for an action. Preserve that value,
        # while validating actor references of every newly imported fixture room.
        with closing(sqlite3.connect(self.target)) as db:
            db.execute("UPDATE rooms SET state=json_set(state,'$.by','auto') WHERE code=(SELECT code FROM rooms LIMIT 1)")
            db.commit()
        report = merge_dataset(self.source, self.target)
        self.assertTrue(report['all_original_rows_unchanged'])
        self.assertEqual(report['protected_original_rows']['users'], 100)
        self.assertEqual(report['after_counts']['users'], 200)
        self.assertEqual(report['after_counts']['ratings'], 8444)
        self.assertEqual(report['after_counts']['films'], 200)
        self.assertEqual(report['imported_rows']['episodes'], 0)
        offset, prefix = report['user_id_offset'], report['text_id_prefix']
        with closing(sqlite3.connect(self.target)) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE role='ADMIN'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users WHERE id>? AND role<>?', (offset, 'USER')).fetchone()[0], 0)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(db.execute('SELECT COUNT(DISTINCT lower(email)) FROM users').fetchone()[0], 200)
            # Every imported clip reference points to its remapped profile clip ID.
            clips = {clip['id'] for r in db.execute('SELECT data FROM user_state WHERE user_id>?', (offset,))
                     for clip in json.loads(r[0])['clips']}
            for r in db.execute('SELECT payload FROM events WHERE user_id>?', (offset,)):
                payload = json.loads(r[0])
                if payload.get('clip_id'):
                    self.assertIn(payload['clip_id'], clips)
                if payload.get('review_id'):
                    self.assertIsNotNone(db.execute('SELECT 1 FROM reviews WHERE id=?', (payload['review_id'],)).fetchone())
                if payload.get('room_code'):
                    self.assertEqual(len(payload['room_code']), 6)
                    self.assertIsNotNone(db.execute('SELECT 1 FROM rooms WHERE code=?', (payload['room_code'],)).fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events WHERE id LIKE ?', (prefix+'%',)).fetchone()[0], report['source_counts']['events'])
        first = logical_hash(self.target)
        again = merge_dataset(self.source, self.target)
        self.assertTrue(again['already_imported'])
        self.assertEqual(logical_hash(self.target), first)

    def test_error_late_in_import_rolls_everything_back(self):
        broken = self.directory/'broken.sqlite3'
        shutil.copyfile(self.source, broken)
        with closing(sqlite3.connect(broken)) as db:
            db.execute("UPDATE events SET payload=json_set(payload,'$.room_code','ABSENT') WHERE id=(SELECT id FROM events LIMIT 1)")
            db.commit()
        before = logical_hash(self.target)
        with self.assertRaises(sqlite3.OperationalError):
            merge_dataset(broken, self.target)
        self.assertEqual(logical_hash(self.target), before)

    def test_wrong_catalog_is_rejected_without_changing_destination(self):
        wrong = self.directory/'wrong.sqlite3'
        shutil.copyfile(self.source, wrong)
        with closing(sqlite3.connect(wrong)) as db:
            db.execute("UPDATE films SET data=json_set(data,'$.title','A different film') WHERE id=(SELECT id FROM films LIMIT 1)")
            db.commit()
        before = logical_hash(self.target)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            merge_dataset(wrong, self.target)
        self.assertEqual(logical_hash(self.target), before)

    def test_catalog_gap_filling_preserves_real_values_and_accounts(self):
        with closing(sqlite3.connect(self.target)) as db:
            row = db.execute('SELECT id,art FROM films LIMIT 1').fetchone()
            movie_id, art = row[0], json.loads(row[1])
            art.pop('cast', None)
            art['tagline'] = 'Keep this existing real tagline'
            art['imdb'] = '—'
            art['kp'] = '9.9'
            db.execute('UPDATE films SET art=? WHERE id=?', (json.dumps(art), movie_id))
            db.commit()
        report = merge_dataset(self.source, self.target)
        enriched = enrich_catalog_gaps(self.source, self.target, report)
        self.assertEqual(enriched['catalog_enrichment']['films_updated'], 1)
        self.assertTrue(enriched['catalog_enrichment']['original_accounts_and_history_unchanged'])
        with closing(sqlite3.connect(self.target)) as db:
            art = json.loads(db.execute('SELECT art FROM films WHERE id=?', (movie_id,)).fetchone()[0])
            self.assertTrue(art['cast'])
            self.assertNotEqual(art['imdb'], '—')
            self.assertEqual(art['kp'], '9.9')
            self.assertEqual(art['tagline'], 'Keep this existing real tagline')
        again = logical_hash(self.target)
        enrich_catalog_gaps(self.source, self.target, enriched)
        self.assertEqual(logical_hash(self.target), again)


if __name__ == '__main__':
    unittest.main()
