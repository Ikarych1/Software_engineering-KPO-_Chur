"""Verify schema identity, all adapted ratings, native JSON and Store compatibility."""
from contextlib import closing
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import struct
import sys

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT))
from database import Store
from build_database import schema_signature, rows, uid, seconds, sha


def main():
    target=HERE/'cinema_test.sqlite3'
    manifest=json.loads((HERE/'manifest.json').read_text(encoding='utf-8'))
    source=json.loads((HERE.parent/'cinema_ml32m_v2/manifest.json').read_text(encoding='utf-8'))
    store=Store(target)  # The same migrations used by the website, including new covering indexes.
    with closing(sqlite3.connect((ROOT/'.local/cinema.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as live:
        expected=schema_signature(live)
    checks=[]
    with closing(sqlite3.connect(target)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA cache_size=-131072')
        actual=schema_signature(db)
        assert actual==expected,'Project and fixture schemas differ'
        assert sum(v['kind']=='table' for v in actual.values())==30
        checks.append('Exact same 30 tables, columns, types, defaults, PK, UK, indexes, foreign keys and view as the current project database')
        counts={t:db.execute('SELECT COUNT(*) FROM "'+t+'"').fetchone()[0] for t,v in actual.items() if v['kind']=='table'}
        for table,n in manifest['counts'].items(): assert counts[table]==n,(table,n,counts[table])
        for table,key in [('films','movies'),('users','users'),('ratings','ratings'),('reviews','reviews'),('progress','progress'),
                          ('rooms','rooms'),('room_members','room_members'),('episodes','episodes'),('watch_sessions','watch_sessions'),
                          ('recommendation_requests','recommendation_requests'),('impressions','impressions'),('clicks','clicks'),
                          ('searches','searches'),('bookmark_events','bookmark_events'),('events','events'),('tags','tags'),('not_interested','not_interested')]:
            assert counts[table]==source['files'][key]['rows'],table
        assert counts['views']==counts['watch_sessions'] and counts['user_state']==counts['users']==counts['user_preferences']
        checks.append('All source business records retained and mapped; one native profile per user and one native view per session')

        digest=hashlib.sha256(); seen=0; watched=watchlist=clips=0
        groups=iter(itertools.groupby(db.execute('SELECT user_id,movie_id,score,updated_at FROM ratings ORDER BY user_id,movie_id'),key=lambda r:r[0]))
        for user,data in db.execute('SELECT user_id,data FROM user_state ORDER BY user_id'):
            state=json.loads(data); group_user,records=next(groups)
            assert group_user==user
            rating_map={}
            for _,mid,score,at in records:
                rating_map[str(mid)]=score; digest.update(struct.pack('<iiid',user,mid,score,at));seen+=1
            assert rating_map==state['ratings'],'Native JSON/table rating mismatch'
            assert set(state)=={'watched','ratings','watchlist','clips','settings','rooms','friends'}
            assert all(type(mid) is int for key in ('watched','watchlist') for mid in state[key])
            watched+=len(state['watched']);watchlist+=len(state['watchlist']);clips+=len(state['clips'])
            for clip in state['clips']:
                assert {'id','movieId','movieTitle','title','image','start','end','format','pinned','createdAt'}==set(clip)
                assert 0<=clip['start']<clip['end'] and clip['end']-clip['start']<=60.002
        assert next(groups,None) is None
        assert seen==source['files']['ratings']['rows'] and digest.hexdigest()==manifest['expected_ratings_sha256']
        assert watched==source['files']['watched']['rows'] and watchlist==source['files']['watchlist']['rows'] and clips==source['files']['clips']['rows']
        checks.append('Every one of 6,219,466 adapted ratings verified by ordered value hash; native JSON mirrors every score; all watched/bookmark/clip records retained')
        print('Verified all ratings and 50,000 native JSON profiles.',flush=True)

        episode_rows={r['episode_id']:r for r in rows('episodes')}
        expected_progress={(uid(r['user_id']),int(r['movie_id']),r['episode_id']):r for r in rows('episode_progress')}
        view_count=0
        for user,mid,eid,file_id,pos,duration,watched_seconds,completed,updated_at in db.execute('SELECT * FROM episode_activity'):
            key=(user,mid,eid); original=expected_progress.pop(key);episode=episode_rows[eid]
            assert file_id==(int(episode['source_file_id']) if episode['source_file_id'] else None)
            assert abs(pos-float(original['position']))<.002 and abs(watched_seconds-float(original['watched_seconds']))<.03
            assert abs(duration-float(original['duration']))<.002 and completed==int(original['completed'])
            assert abs(updated_at-seconds(original['updated_at_ms']))<.00001
            view_count+=1
        assert not expected_progress and view_count==manifest['source_episode_progress_rows']
        for user,mid,file_id in db.execute('SELECT user_id,movie_id,file_id FROM episode_progress'):
            available=json.loads(db.execute('SELECT episodes FROM films WHERE id=?',(mid,)).fetchone()[0])
            assert file_id in {e['file_id'] for e in available},'Invented playable episode file'
        checks.append('All 13,675 episode snapshots reproduced by episode_activity; 91 source-backed snapshots use native episode_progress; no fabricated video file IDs')
        assert not db.execute('''SELECT 1 FROM watch_sessions s JOIN views v ON v.id=s.view_id
            WHERE s.user_id<>v.user_id OR s.movie_id<>v.movie_id OR s.started_at<>v.started_at
            OR s.room_code IS NOT v.room_code LIMIT 1''').fetchone()
        assert not db.execute('''SELECT 1 FROM clicks c JOIN impressions i ON i.id=c.impression_id
            WHERE c.user_id<>i.user_id OR c.movie_id<>i.movie_id OR c.request_id<>i.request_id LIMIT 1''').fetchone()
        assert not db.execute('''SELECT 1 FROM room_members m JOIN rooms r ON r.code=m.code
            WHERE m.user_id=r.host_id AND (m.joined_at<r.created_at OR m.last_seen>r.ended_at) LIMIT 1''').fetchone()
        assert not list(db.execute('PRAGMA foreign_key_check'))
        print('All data and cross-table links passed. Checking physical integrity...',flush=True)
        integrity=[r[0] for r in db.execute('PRAGMA integrity_check')]
        assert integrity==['ok'],integrity[:5]
        checks.append('Session/view, click/impression and room membership consistency; all foreign keys and full SQLite integrity_check')
    # Decode through the same dataclass and access methods that the app actually uses.
    first=manifest['credential']['user_id']
    decoded=store.films(include_hidden=True)
    assert len(decoded)==source['movies'] and all(type(r['film'].id) is int for r in decoded)
    assert store.user(store.account(user_id=first)).id==first
    assert store.state(first)['ratings']=={str(k):v for k,v in store.ratings_of([first])[first].items()}
    assert isinstance(store.progress(first),list) and isinstance(store.user_rooms(first),list)
    checks.append('Native Store.films/MediaItem, account/User, JSON state, ratings_of, progress and rooms work without an adapter')
    with closing(sqlite3.connect(target)) as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.execute('PRAGMA journal_mode=DELETE')
    checksum=sha(target)
    target.with_suffix('.sqlite3.sha256').write_text(checksum+'  '+target.name+'\n',encoding='utf-8')
    report=dict(passed=True,schema_matches_project=True,tables=30,views=['episode_activity'],counts=counts,
                ratings_verified=seen,watched_records=watched,watchlist_records=watchlist,clips=clips,
                episode_snapshots=view_count,integrity_check=integrity,checks=checks,sha256=checksum,bytes=target.stat().st_size)
    (HERE/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (HERE/'schema_signature.json').write_text(json.dumps(actual,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('counts','checks')},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
