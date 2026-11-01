"""Backward compatibility of additive engagement storage."""
from dataclasses import asdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import SCHEMA, Store
from models import MediaItem


class EngagementSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'test.sqlite3'
        film=MediaItem(1,'Film','Description',['Drama'],2020,'US',7.5,120,'12+')
        self.state=dict(watched=[1],ratings={'1':8},watchlist=[],clips=[],settings={'name':'Viewer'},rooms={},friends=[])
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO users(id,email,username,password_hash,role) VALUES(1,'test@example.invalid','Viewer','unused','ADMIN')")
            db.execute('INSERT INTO films(id,data,art,revision) VALUES(?,?,?,?)',(1,json.dumps(asdict(film)),'{}','test'))
            db.execute('INSERT INTO user_state VALUES(?,?)',(1,json.dumps(self.state)))
            db.execute('INSERT INTO ratings VALUES(?,?,?,?)',(1,1,8,100.0))
            db.commit()
        self.store=Store(self.path)

    def tearDown(self): self.temp.cleanup()

    def test_old_data_and_store_contract_survive_repeated_installation(self):
        self.assertEqual(self.store.state(1),self.state)
        self.assertEqual(self.store.film(1)['film'].title,'Film')
        self.assertEqual(self.store.account(user_id=1)['username'],'Viewer')
        self.assertEqual(self.store.ratings_of([1]),{1:{1:8}})
        again=Store(self.path)
        self.assertEqual(again.state(1),self.state)
        with again.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ratings').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_preferences_survive_ordinary_profile_updates_and_fks_are_enforced(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO user_preferences(user_id,genres,updated_at) VALUES(?,?,?)',(1,'["Drama"]',100))
        self.store.update_state(1,lambda s:s['watchlist'].append(1))
        with self.store.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT genres FROM user_preferences WHERE user_id=1').fetchone()[0]),['Drama'])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute("INSERT INTO recommendation_requests VALUES('missing',999,100,'home','test')")

    def test_episode_activity_preserves_rewatch_totals_without_inventing_files(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO episodes VALUES('ep_1',1,1,1,'Episode',100,NULL)")
            for n,start,end,position,watched,completed in [(1,100,200,100,100,1),(2,300,320,20,20,0)]:
                db.execute('INSERT INTO views VALUES(?,?,?,?,?)',(n,1,1,None,start))
                db.execute('''INSERT INTO watch_sessions(id,view_id,user_id,movie_id,episode_id,started_at,ended_at,
                    duration,start_position,end_position,watched_seconds,paused_seconds,buffered_seconds,completed,start_kind,view_number,stop_reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (f'ws_{n}',n,1,1,'ep_1',start,end,100,0,position,watched,0,0,completed,'first' if n==1 else 'rewatch',n,'finished' if n==1 else 'user_exit'))
        with self.store.connect() as db:
            row=dict(db.execute('SELECT * FROM episode_activity').fetchone())
        self.assertIsNone(row['file_id'])
        self.assertEqual((row['position'],row['watched_seconds'],row['completed']),(20,120,1))
        self.store.delete_film(1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM episodes').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM watch_sessions').fetchone()[0],0)


if __name__=='__main__': unittest.main()
