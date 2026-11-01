"""Adapt v2 to the application's schema plus its additive engagement schema."""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import struct
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
SOURCE=HERE.parent/'cinema_ml32m_v2'
LIVE=ROOT/'.local/cinema.sqlite3'
sys.path.insert(0,str(ROOT))
from engagement_schema import SCHEMA as ENGAGEMENT_SCHEMA
from werkzeug.security import generate_password_hash

TEST_PASSWORD=os.environ.get('CINEMA_TEST_PASSWORD') or secrets.token_urlsafe(24)
ALPHABET='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
TABLE_COUNTS={}


def jd(value): return json.dumps(value,ensure_ascii=False,separators=(',',':'))
def uid(value): return int(value.removeprefix('ml32m_'))
def number(value): return int(value.rsplit('_',1)[1])
def seconds(value): return int(value)/1000
def iso(value): return datetime.fromtimestamp(float(value),timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
def room_code(value):
    n=number(value); result=''
    for _ in range(5): n,r=divmod(n,len(ALPHABET)); result=ALPHABET[r]+result
    assert n==0
    return 'R'+result
def rows(name):
    with gzip.open(SOURCE/'data'/(name+'.csv.gz'),'rt',encoding='utf-8',newline='') as f: yield from csv.DictReader(f)
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while block:=f.read(1024*1024): h.update(block)
    return h.hexdigest()
def insert(db,table,columns,values):
    sql=f'INSERT INTO "{table}" ('+','.join('"'+c+'"' for c in columns)+') VALUES ('+','.join('?' for _ in columns)+')'
    count=0; batch=[]
    db.execute('BEGIN')
    for value in values:
        batch.append(value); count+=1
        if len(batch)==20000:
            db.executemany(sql,batch); batch.clear()
            if count%1000000==0: print(f'Imported {table}: {count:,}',flush=True)
    if batch: db.executemany(sql,batch)
    db.commit(); TABLE_COUNTS[table]=count
    print(f'Complete {table}: {count:,}',flush=True)


def schema_signature(db):
    result={}
    for kind,name,sql in db.execute("SELECT type,name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view') ORDER BY name"):
        if kind=='view': result[name]={'kind':kind,'sql':' '.join(sql.split())}; continue
        columns=[list(r)[1:] for r in db.execute('SELECT * FROM pragma_table_info(?)',(name,))]
        fks=sorted([list(r)[2:] for r in db.execute('SELECT * FROM pragma_foreign_key_list(?)',(name,))],key=str)
        indexes=[]
        for index in db.execute('SELECT * FROM pragma_index_list(?)',(name,)):
            fields=[list(r)[2:] for r in db.execute('SELECT * FROM pragma_index_info(?)',(index[1],))]
            indexes.append([index[2],index[3],index[4],fields])
        result[name]={'kind':kind,'columns':columns,'foreign_keys':fks,'indexes':sorted(indexes,key=str)}
    return result


def main():
    start=time.monotonic(); target=HERE/'cinema_test.sqlite3'
    if target.exists(): raise FileExistsError(target)
    source_manifest=json.loads((SOURCE/'manifest.json').read_text(encoding='utf-8'))
    for name,info in source_manifest['files'].items(): assert sha(SOURCE/info['file'])==info['sha256'],name
    with closing(sqlite3.connect(LIVE.resolve().as_uri()+'?mode=ro',uri=True)) as live:
        live.execute('PRAGMA query_only=ON'); live.execute('BEGIN')
        objects=list(live.execute("SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY name"))
        live.row_factory=sqlite3.Row
        catalog={r['id']:dict(r) for r in live.execute('SELECT * FROM films')}
        sources=[dict(r) for r in live.execute('SELECT * FROM film_sources')]
        media=[dict(r) for r in live.execute('SELECT * FROM media_info')]
    assert set(catalog)==set(source_manifest['allowed_catalog_movie_ids'])
    with tempfile.NamedTemporaryFile(prefix='cinema.building-',suffix='.sqlite3',dir=HERE,delete=False) as f: staging=Path(f.name)
    db=sqlite3.connect(staging)
    db.execute('PRAGMA journal_mode=OFF'); db.execute('PRAGMA synchronous=OFF')
    db.execute('PRAGMA foreign_keys=OFF'); db.execute('PRAGMA cache_size=-131072'); db.execute('PRAGMA temp_store=FILE')
    for kind,name,sql in objects:
        if kind=='table': db.execute(sql)
    db.executescript(ENGAGEMENT_SCHEMA)
    index_sql={name:sql for kind,name,sql in objects if kind=='index'}
    for name,sql in list(db.execute("SELECT name,sql FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL")):
        index_sql[name]=sql; db.execute('DROP INDEX "'+name+'"')
    films={}; film_columns=[r[1] for r in db.execute('PRAGMA table_info(films)')]
    mapping=defaultdict(list)
    for row in rows('movie_mapping'): mapping[int(row['movie_id'])].append(int(row['source_movie_id']))
    for row in rows('movies'):
        mid=int(row['movie_id']); original=catalog[mid]
        data=dict(id=mid,title=row['title'],description=row['description'],genres=json.loads(row['genres']),
                  year=int(row['year']),country=', '.join(json.loads(row['countries'])),rating=float(row['imdb_rating']),
                  duration_min=int(row['duration_min']),age_rating=row['age_rating'],is_hidden=row['is_hidden']=='1',kind=row['catalog_kind'])
        art=json.loads(original['art']); actors=json.loads(row['actors']); directors=json.loads(row['directors'])
        art.update(original=row['original_title'],tagline=row['tagline'],actors=[a['name'] for a in actors],
                   director=', '.join(d['name'] for d in directors),cast=actors,directors=directors,
                   imdb=row['imdb_rating'],imdb_votes=int(row['imdb_vote_count']),kp=row['kinopoisk_rating'],kp_votes=int(row['kinopoisk_vote_count']),
                   tmdb_rating=float(row['tmdb_rating']),tmdb_votes=int(row['tmdb_vote_count']),
                   content_type=row['content_type'],countries=json.loads(row['countries']),country_codes=json.loads(row['country_codes']),
                   original_language=row['original_language'],spoken_languages=json.loads(row['spoken_languages']),
                   collection_id=int(row['collection_id']) if row['collection_id'] else None,collection_name=row['collection_name'],
                   movielens_ids=mapping[mid])
        original.update(data=jd(data),art=jd(art))
        films[mid]=original
    insert(db,'films',film_columns,([f[c] for c in film_columns] for f in films.values()))
    for table,items in [('film_sources',sources),('media_info',media)]:
        cols=[r[1] for r in db.execute('PRAGMA table_info('+table+')')]
        insert(db,table,cols,([r[c] for c in cols] for r in items))

    users={uid(r['user_id']):r for r in rows('users')}; names={u:r['display_name'] for u,r in users.items()}
    activity={uid(r['user_id']):r for r in rows('user_activity')}; first_user=min(users)
    password_hash=generate_password_hash(TEST_PASSWORD)
    insert(db,'users',['id','email','username','password_hash','role','active','session_version','created_at','last_seen','plan','plan_until'],
           ([u,r['email'],r['display_name'],password_hash,'ADMIN' if u==first_user else 'USER',1,1,iso(r['registered_at']),
             max(float(activity[u]['last_rating_at']),seconds(activity[u]['last_event_at_ms'])),'free',None] for u,r in users.items()))
    insert(db,'user_preferences',['user_id','genres','content_types','languages','duration_min','actor_ids','director_ids','locale','timezone','updated_at'],
           ([u,r['preferred_genres'],r['preferred_content_types'],r['preferred_languages'],r['preferred_duration_min'],r['preferred_actor_ids'],
             r['preferred_director_ids'],r['locale'],r['timezone'],float(r['registered_at'])] for u,r in users.items()))
    states={u:dict(watched=[],ratings={},watchlist=[],clips=[],settings={'name':r['display_name']},rooms={},friends=[]) for u,r in users.items()}
    for table,key in [('watched','watched'),('watchlist','watchlist')]:
        for row in rows(table): states[uid(row['user_id'])][key].append(int(row['movie_id']))
    for row in rows('clips'):
        mid=int(row['movie_id']); art=json.loads(films[mid]['art'])
        states[uid(row['user_id'])]['clips'].append(dict(id=row['clip_id'],movieId=mid,movieTitle=json.loads(films[mid]['data'])['title'],
            title=row['title'],image=art.get('tmdb_backdrop_url') or art.get('tmdb_poster_url') or '',start=float(row['start_seconds']),end=float(row['end_seconds']),
            format=row['format'],pinned=row['pinned']=='1',createdAt=datetime.fromtimestamp(seconds(row['created_at_ms']),timezone.utc).isoformat().replace('+00:00','Z')))
    rating_hash=hashlib.sha256(); batch=[]; state_batch=[]; previous=None; group=[]; count=0
    rating_insert='INSERT INTO ratings(user_id,movie_id,score,updated_at) VALUES(?,?,?,?)'
    def flush_ratings(user,group):
        if user is None: return
        state=states.pop(user)
        for mid,score,at in sorted(group):
            batch.append((user,mid,score,at)); state['ratings'][str(mid)]=score
            rating_hash.update(struct.pack('<iiid',user,mid,score,at))
        state_batch.append((user,jd(state)))
        if len(batch)>=20000:
            db.executemany(rating_insert,batch); batch.clear()
            db.executemany('INSERT INTO user_state VALUES(?,?)',state_batch); state_batch.clear()
    db.execute('BEGIN')
    with gzip.open(HERE/'rating_sources.csv.gz','wt',encoding='utf-8',newline='') as extra:
        audit=csv.writer(extra,lineterminator='\n'); audit.writerow(['user_id','movie_id','source_record_id','source_movie_id','split'])
        for row in rows('ratings'):
            user=uid(row['user_id'])
            if user!=previous:
                flush_ratings(previous,group); previous=user; group=[]
            mid=int(row['movie_id']); group.append((mid,int(row['score']),float(row['rated_at'])))
            audit.writerow([user,mid,row['rating_id'],row['source_movie_id'],row['split']]); count+=1
            if count%1000000==0: print(f'Adapted ratings and JSON profiles: {count:,}',flush=True)
        flush_ratings(previous,group)
    if batch: db.executemany(rating_insert,batch)
    if state_batch: db.executemany('INSERT INTO user_state VALUES(?,?)',state_batch)
    db.commit(); assert not states
    TABLE_COUNTS.update(ratings=count,user_state=len(users))
    episode_files={}
    def episode_rows():
        for r in rows('episodes'):
            file_id=int(r['source_file_id']) if r['source_file_id'] else None
            episode_files[r['episode_id']]=file_id
            yield [r['episode_id'],int(r['movie_id']),int(r['season']),int(r['episode']),r['title'],float(r['duration_seconds']),file_id]
    insert(db,'episodes',['id','movie_id','season','episode','title','duration','file_id'],episode_rows())
    insert(db,'reviews',['id','user_id','movie_id','score','text','created_at','updated_at'],
           ([number(r['review_id']),uid(r['user_id']),int(r['movie_id']),int(r['score']),r['text'],float(r['created_at']),float(r['created_at'])] for r in rows('reviews')))
    insert(db,'progress',['user_id','movie_id','position','duration','completed','watched_seconds','updated_at','hidden','file_id'],
           ([uid(r['user_id']),int(r['movie_id']),float(r['position']),float(r['duration']),int(r['completed']),float(r['watched_seconds']),seconds(r['updated_at_ms']),0,episode_files.get(r['episode_id'])] for r in rows('progress')))
    insert(db,'episode_progress',['user_id','movie_id','file_id','position','duration','completed','updated_at'],
           ([uid(r['user_id']),int(r['movie_id']),episode_files[r['episode_id']],float(r['position']),float(r['duration']),int(r['completed']),seconds(r['updated_at_ms'])]
            for r in rows('episode_progress') if episode_files[r['episode_id']] is not None))
    insert(db,'not_interested',['user_id','movie_id','is_active','reason','updated_at'],
           ([uid(r['user_id']),int(r['movie_id']),int(r['is_active']),r['reason'],seconds(r['updated_at_ms'])] for r in rows('not_interested')))
    settings=jd(dict(control='host',wait_for_all=True,chat=True,reactions=True,locked=False))
    insert(db,'rooms',['code','movie_id','host_id','title','settings','state','created_at','ended_at'],
           ([room_code(r['room_id']),int(r['movie_id']),uid(r['host_user_id']),r['title'],settings,
             jd(dict(playing=False,position=0.0,updated_at=seconds(r['ended_at_ms']),rate=1.0,by=uid(r['host_user_id']),episode=episode_files.get(r['episode_id']),catalog_episode_id=r['episode_id'] or None)),
             seconds(r['created_at_ms']),seconds(r['ended_at_ms'])] for r in rows('rooms')))
    insert(db,'room_members',['code','user_id','joined_at','last_seen'],
           ([room_code(r['room_id']),uid(r['user_id']),seconds(r['joined_at_ms']),seconds(r['left_at_ms'])] for r in rows('room_members')))
    insert(db,'recommendation_requests',['id','user_id','created_at','surface','policy'],
           ([r['request_id'],uid(r['user_id']),seconds(r['shown_at_ms']),r['surface'],r['policy']] for r in rows('recommendation_requests')))
    session_columns=['id','view_id','user_id','movie_id','episode_id','room_code','request_id','started_at','ended_at','duration','start_position','end_position','watched_seconds','paused_seconds','buffered_seconds','completed','start_kind','view_number','stop_reason']
    insert(db,'views',['id','user_id','movie_id','room_code','started_at'],
           ([number(r['session_id']),uid(r['user_id']),int(r['movie_id']),room_code(r['room_id']) if r['room_id'] else None,seconds(r['started_at_ms'])] for r in rows('watch_sessions')))
    insert(db,'watch_sessions',session_columns,
           ([r['session_id'],number(r['session_id']),uid(r['user_id']),int(r['movie_id']),r['episode_id'] or None,room_code(r['room_id']) if r['room_id'] else None,r['request_id'] or None,
             seconds(r['started_at_ms']),seconds(r['ended_at_ms']),float(r['duration_seconds']),float(r['start_position']),float(r['end_position']),float(r['watched_seconds']),float(r['paused_seconds']),float(r['buffered_seconds']),int(r['completed']),r['start_kind'],int(r['view_number']),r['stop_reason']] for r in rows('watch_sessions')))
    insert(db,'impressions',['id','request_id','user_id','movie_id','rank','shown_at','clicked'],
           ([r['impression_id'],r['request_id'],uid(r['user_id']),int(r['movie_id']),int(r['rank']),seconds(r['shown_at_ms']),int(r['clicked'])] for r in rows('impressions')))
    insert(db,'clicks',['id','impression_id','request_id','user_id','movie_id','clicked_at'],
           ([r['click_id'],r['impression_id'],r['request_id'],uid(r['user_id']),int(r['movie_id']),seconds(r['clicked_at_ms'])] for r in rows('clicks')))
    insert(db,'searches',['id','user_id','query','query_type','searched_at','selected_movie_id'],
           ([r['search_id'],uid(r['user_id']),r['query'],r['query_type'],seconds(r['searched_at_ms']),int(r['selected_movie_id'])] for r in rows('searches')))
    insert(db,'bookmark_events',['id','user_id','movie_id','action','created_at','collection'],
           ([r['bookmark_event_id'],uid(r['user_id']),int(r['movie_id']),r['action'],seconds(r['event_at_ms']),r['collection']] for r in rows('bookmark_events')))
    insert(db,'tags',['id','user_id','movie_id','tag','created_at'],
           ([r['tag_id'],uid(r['user_id']),int(r['movie_id']),r['tag'],float(r['created_at'])] for r in rows('tags')))
    messages=[]
    def event_rows():
        for r in rows('events'):
            payload=json.loads(r['payload']); user=uid(r['user_id'])
            if payload.get('room_id'): payload['room_code']=room_code(payload.pop('room_id'))
            if payload.get('review_id'): payload['review_id']=number(payload['review_id'])
            payload.pop('rating_id',None)
            if r['event_type'] in ('room_create','room_join','room_leave'):
                verbs={'room_create':'создал(а) комнату','room_join':'присоединился(-ась)','room_leave':'покинул(а) комнату'}
                messages.append((payload['room_code'],user,names[user],'system',names[user]+' '+verbs[r['event_type']],seconds(r['event_at_ms'])))
            yield [r['event_id'],user,int(r['movie_id']) if r['movie_id'] else None,r['event_type'],seconds(r['event_at_ms']),r['session_id'] or None,r['request_id'] or None,jd(payload)]
    insert(db,'events',['id','user_id','movie_id','event_type','created_at','session_id','request_id','payload'],event_rows())
    insert(db,'room_messages',['code','user_id','name','kind','text','created_at'],messages)
    for n,(name,sql) in enumerate(index_sql.items(),1):
        db.execute(sql)
        if n%10==0: print(f'Built project indexes: {n}/{len(index_sql)}',flush=True)
    db.commit(); db.execute('ANALYZE'); db.execute('PRAGMA user_version=3'); db.commit()
    db.execute('PRAGMA journal_mode=DELETE'); db.execute('PRAGMA synchronous=FULL')
    signature=schema_signature(db)
    (HERE/'schema_signature.json').write_text(json.dumps(signature,ensure_ascii=False,indent=2),encoding='utf-8')
    ddls=[r[0]+';' for r in db.execute("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name")]
    (HERE/'schema.sql').write_text('\n\n'.join(ddls)+'\n',encoding='utf-8')
    db.close(); staging.replace(target)
    manifest=dict(version=3,file=target.name,users=len(users),films=len(films),counts=TABLE_COUNTS,as_of=source_manifest['as_of'],
                  source_manifest_sha256=sha(SOURCE/'manifest.json'),expected_ratings_sha256=rating_hash.hexdigest(),
                   credential=dict(email=users[first_user]['email'],user_id=first_user),
                  elapsed_seconds=round(time.monotonic()-start,2),source_episode_progress_rows=source_manifest['files']['episode_progress']['rows'])
    (HERE/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (HERE/'TEST_LOGIN.md').write_text('# Вход в отдельный тестовый экземпляр\n\nАдминистратор: `'+users[first_user]['email']+'`\n\nПароль: `'+TEST_PASSWORD+'`\n\nЭто общие тестовые реквизиты набора; реальные аккаунты проекта в него не переносились.\n',encoding='utf-8')
    print(jd({'built':str(target),'users':len(users),'ratings':count,'tables':sum(v['kind']=='table' for v in signature.values()),'seconds':manifest['elapsed_seconds']}),flush=True)


if __name__=='__main__': main()
