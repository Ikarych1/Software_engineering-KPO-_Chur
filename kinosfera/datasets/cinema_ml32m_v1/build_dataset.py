"""Offline catalog-scoped MovieLens adapter. Never opens the application DB for writing.

Real ratings/tags and synthetic UI/player interactions are deliberately separated.
Run with --stage prepare, --stage simulate, or --stage all (default).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import random
import sqlite3
import time
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = HERE/'data'
SEED = 20261031
AS_OF = int(datetime(2026,10,31,12,tzinfo=timezone.utc).timestamp())
EXPECTED_MD5 = {'links.csv':'8f033867bcb4e6be8792b21468b4fa6e',
                'movies.csv':'0df90835c19151f9d819d0822e190797',
                'ratings.csv':'cf12b74f9ad4b94a011f079e26d4270a',
                'tags.csv':'963bf4fa4de6b8901868fddd3eb54567'}
FILES = {}
MOVIE_FIELDS = ['movie_id','imdb_id','tmdb_id','catalog_kind','content_type','title','original_title','genres',
    'description','tagline','year','countries','country_codes','original_language','spoken_languages','duration_min',
    'age_rating','actors','directors','collection_id','collection_name','tmdb_rating','tmdb_vote_count',
    'imdb_rating','imdb_vote_count','kinopoisk_rating','kinopoisk_vote_count','catalog_view_count',
    'has_video_source','simulated_playback_available','is_hidden','metadata_provenance','synthetic_fields']
RATING_FIELDS = ['rating_id','user_id','movie_id','score','source_rating_0_5_to_5','rated_at','source_movie_id','split','source','is_synthetic']


def jd(value): return json.dumps(value,ensure_ascii=False,separators=(',',':'))
def load(name): return json.loads((HERE/name).read_text(encoding='utf-8'))
def dump(name,value): (HERE/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while block:=f.read(1024*1024): h.update(block)
    return h.hexdigest()


@contextmanager
def writer(name,fields):
    """Deterministic gzip stream; JSON arrays/objects are UTF-8 CSV cell values."""
    DATA.mkdir(parents=True,exist_ok=True)
    path=DATA/(name+'.csv.gz')
    if path.exists(): raise FileExistsError(path)
    with path.open('xb') as raw,gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0,compresslevel=4) as gz,io.TextIOWrapper(gz,encoding='utf-8',newline='') as text:
        output=csv.writer(text,lineterminator='\n')
        output.writerow(fields)
        count=0
        def write(row):
            nonlocal count
            output.writerow([jd(v) if isinstance(v,(dict,list)) else '' if v is None else v for v in row])
            count+=1
        yield write
    FILES[name]={'file':'data/'+path.name,'rows':count,'columns':fields,'bytes':path.stat().st_size,'sha256':digest(path)}


def read(name):
    with gzip.open(DATA/(name+'.csv.gz'),'rt',encoding='utf-8',newline='') as f:
        yield from csv.DictReader(f)


def archive_rows(archive,name):
    with archive.open('ml-32m/'+name) as f,io.TextIOWrapper(f,encoding='utf-8',newline='') as text:
        yield from csv.DictReader(text)


def numeric(value):
    try:
        n=float(value)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError): return None


def catalog(db_path):
    db=sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
    sources={r[0] for r in db.execute('SELECT DISTINCT movie_id FROM film_sources')}
    views=dict(db.execute('SELECT movie_id,COUNT(*) FROM views GROUP BY movie_id'))
    items={}
    for row in db.execute('SELECT id,data,art,imdb_id,tmdb_id,tmdb_kind,episodes,torrent IS NOT NULL has_source FROM films'):
        item=dict(row); item['data']=json.loads(item['data']); item['art']=json.loads(item['art'])
        item['episodes']=json.loads(item['episodes'] or '[]')
        item['has_source']=int(bool(item['has_source'] or item['id'] in sources))
        item['view_count']=views.get(item['id'],0); item['cache']={}
        items[item['id']]=item
    wanted={(r['tmdb_kind'],r['tmdb_id']):r for r in items.values() if r['tmdb_id']}
    for row in db.execute("SELECT key,data FROM tmdb_cache WHERE key LIKE '/movie/%' OR key LIKE '/tv/%'"):
        key=row['key'].split('?',1)[0].split('/')
        if len(key)!=3 or not key[2].isdigit(): continue
        item=wanted.get(('series' if key[1]=='tv' else 'movie',int(key[2])))
        if item is None: continue
        value=json.loads(row['data'])
        # Prefer Russian responses containing credits; merge missing fallback fields.
        priority=int('language=ru-RU' in row['key'])+2*int('append_to_response=' in row['key'])
        if priority>=item.get('cache_priority',-1):
            previous=item['cache']; item['cache']=dict(previous,**{k:v for k,v in value.items() if v is not None and v!=''})
            item['cache_priority']=priority
    counts={t:db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('films','users','ratings','views','progress')}
    db.rollback(); db.close()
    return items,counts


def imdb_ratings(items):
    wanted={r['imdb_id'] for r in items.values() if r['imdb_id']}; out={}
    path=ROOT/'.local/imdb/title.ratings.tsv.gz'
    if path.is_file():
        with gzip.open(path,'rt',encoding='utf-8',newline='') as f:
            for row in csv.DictReader(f,delimiter='\t'):
                if row['tconst'] in wanted: out[row['tconst']]=(float(row['averageRating']),int(row['numVotes']))
    return out


def movie_metadata(items,imdb):
    episodes=[]; output=[]
    for mid,item in sorted(items.items()):
        d,a,c=item['data'],item['art'],item['cache']; rng=random.Random(SEED+mid)
        source={}; generated=[]
        def value(name,actual,fallback,origin='catalog'):
            if actual is None or actual=='' or actual==[] or actual=={} or actual=='—':
                actual=fallback; source[name]='synthetic'; generated.append(name)
            else: source[name]=origin
            return actual
        genres=value('genres',[str(g).strip().casefold() for g in d.get('genres',[]) if g and str(g).casefold()!='gfjestjdsmf'],['драма'])
        description=value('description',d.get('description') if d.get('description')!='Описание пока не добавлено.' else '',f'Тестовое описание произведения «{d["title"]}» в жанре {genres[0]}.')
        tagline=value('tagline',a.get('tagline'),f'Новая история: {d["title"]}.')
        countries=value('countries',[r['name'] for r in c.get('production_countries',[]) if r.get('name')] or ([d['country']] if d.get('country') else []),['США'],'tmdb_cache' if c.get('production_countries') else 'catalog')
        country_codes=value('country_codes',[r['iso_3166_1'] for r in c.get('production_countries',[]) if r.get('iso_3166_1')],['US'],'tmdb_cache')
        lang=value('original_language',c.get('original_language'),'en','tmdb_cache')
        spoken=value('spoken_languages',[r.get('iso_639_1') for r in c.get('spoken_languages',[]) if r.get('iso_639_1')],[lang],'tmdb_cache')
        age=value('age_rating',d.get('age_rating'),rng.choice(['6+','12+','16+','18+']))
        credits=c.get('aggregate_credits') or c.get('credits') or {}
        actors=[{'person_id':p['id'],'name':p['name']} for p in credits.get('cast',[]) if p.get('id') and p.get('name')][:30]
        if not actors: actors=[{'person_id':None,'name':name} for name in a.get('actors',[])]
        actors=value('actors',actors,[{'person_id':None,'name':f'Synthetic actor {mid}'}],'tmdb_cache' if credits.get('cast') else 'catalog')
        directors=[{'person_id':p['id'],'name':p['name']} for p in credits.get('crew',[]) if p.get('id') and p.get('name') and (p.get('job')=='Director' or any(j.get('job')=='Director' for j in p.get('jobs',[])))]
        if not directors and a.get('director'): directors=[{'person_id':None,'name':a['director']}]
        directors=value('directors',directors,[{'person_id':None,'name':f'Synthetic director {mid}'}],'tmdb_cache' if directors and directors[0]['person_id'] else 'catalog')
        collection=c.get('belongs_to_collection') or {}
        # An absent collection means none, not a missing value to invent.
        source['collection']='tmdb_cache' if 'belongs_to_collection' in c else 'not_applicable_or_unknown'
        tmdb=value('tmdb_rating',numeric(a.get('tmdb_rating')),round(rng.uniform(5,8.5),1),'tmdb_cache')
        tmdb_votes=value('tmdb_vote_count',a.get('tmdb_votes'),rng.randint(50,15000),'tmdb_cache')
        actual_imdb=imdb.get(item['imdb_id'])
        imr=value('imdb_rating',actual_imdb[0] if actual_imdb else None,round(min(9.5,max(2,tmdb+rng.gauss(.1,.5))),1),'imdb_official_snapshot')
        imv=value('imdb_vote_count',actual_imdb[1] if actual_imdb else None,rng.randint(100,80000),'imdb_official_snapshot')
        # Initial Kinopoisk fixture numbers are not verified observations.
        kp=round(min(9.8,max(1,imr+rng.gauss(0,.45))),1); kpv=rng.randint(50,500000)
        source.update(kinopoisk_rating='synthetic',kinopoisk_vote_count='synthetic'); generated+=['kinopoisk_rating','kinopoisk_vote_count']
        content_type='movie'
        if d.get('kind')=='series': content_type='miniseries' if 'mini' in str(c.get('type','')).casefold() else 'series'
        source['content_type']='tmdb_cache' if content_type=='miniseries' else 'catalog'
        row=dict(movie_id=mid,imdb_id=item['imdb_id'] or '',tmdb_id=item['tmdb_id'] or '',catalog_kind=d.get('kind','movie'),content_type=content_type,
                 title=d['title'],original_title=a.get('original',d['title']),genres=genres,description=description,tagline=tagline,year=d['year'],
                 countries=countries,country_codes=country_codes,original_language=lang,spoken_languages=spoken,duration_min=d['duration_min'],
                 age_rating=age,actors=actors,directors=directors,collection_id=collection.get('id'),collection_name=collection.get('name',''),
                 tmdb_rating=tmdb,tmdb_vote_count=tmdb_votes,imdb_rating=imr,imdb_vote_count=imv,kinopoisk_rating=kp,kinopoisk_vote_count=kpv,
                 catalog_view_count=item['view_count'],has_video_source=item['has_source'],simulated_playback_available=1,is_hidden=int(d.get('is_hidden',False)),
                 metadata_provenance=source,synthetic_fields=generated)
        output.append(row)
        if d.get('kind')=='series':
            modeled=item['episodes']
            if modeled:
                specs=[(e['season'],e['episode'],e.get('title') or f'Серия {e["episode"]}',e['file_id'],'catalog_source_list') for e in modeled]
            else:
                seasons=[s for s in c.get('seasons',[]) if s.get('season_number',0)>0][:3]
                if not seasons: seasons=[{'season_number':1,'episode_count':rng.randint(4,10)}]
                specs=[(s['season_number'],n,f'Серия {n}',None,'synthetic') for s in seasons for n in range(1,min(10,s.get('episode_count') or 8)+1)]
            seen=set()
            for season,number,title,file_id,origin in specs:
                eid=f'ep_{mid}_s{season:02}_e{number:03}'
                if eid in seen: continue
                seen.add(eid)
                episodes.append(dict(episode_id=eid,movie_id=mid,season=season,episode=number,title=title,
                    duration_seconds=int(d['duration_min']*60),source_file_id=file_id,source=origin,is_synthetic=int(origin=='synthetic')))
    return output,episodes


def prepare(args):
    if (HERE/'prepare_manifest.json').exists(): raise FileExistsError('Preparation already exists; choose a new output folder for a new snapshot.')
    sha_before=digest(args.database); items,db_counts=catalog(args.database)
    metadata,episodes=movie_metadata(items,imdb_ratings(items))
    with writer('movies',MOVIE_FIELDS) as write:
        for r in metadata: write([r.get(k) for k in MOVIE_FIELDS])
    ep_fields=['episode_id','movie_id','season','episode','title','duration_seconds','source_file_id','source','is_synthetic']
    with writer('episodes',ep_fields) as write:
        for r in episodes: write([r.get(k) for k in ep_fields])
    with zipfile.ZipFile(args.archive) as archive:
        # Verify the official checksums while streaming the ZIP members.
        for name,wanted in EXPECTED_MD5.items():
            h=hashlib.md5()
            with archive.open('ml-32m/'+name) as f:
                while block:=f.read(4*1024*1024): h.update(block)
            if h.hexdigest()!=wanted: raise ValueError(f'Official MovieLens checksum mismatch: {name}')
            print('Verified official checksum:',name,flush=True)
        (HERE/'MOVIELENS_SOURCE_README.txt').write_bytes(archive.read('ml-32m/README.txt'))
        by_imdb={r['imdb_id']:mid for mid,r in items.items() if r['imdb_id']}
        by_tmdb={r['tmdb_id']:mid for mid,r in items.items() if r['tmdb_id'] and r['tmdb_kind']=='movie'}
        mapping={}; conflicts=0
        for row in archive_rows(archive,'links.csv'):
            a=by_imdb.get('tt'+row['imdbId'].zfill(7)) if row['imdbId'] else None
            b=by_tmdb.get(int(row['tmdbId'])) if row['tmdbId'] else None
            if a and b and a!=b: conflicts+=1; continue
            if a or b: mapping[int(row['movieId'])]=a or b
        print(f'Matched {len(set(mapping.values()))}/{len(items)} catalog titles to MovieLens.',flush=True)
        counts=Counter()
        for n,row in enumerate(archive_rows(archive,'ratings.csv'),1):
            if int(row['movieId']) in mapping: counts[int(row['userId'])]+=1
            if n%8_000_000==0: print(f'Count pass: {n:,} real ratings scanned.',flush=True)
        eligible=sorted(uid for uid,n in counts.items() if n>=20)
        selected=set(random.Random(SEED).sample(eligible,min(args.users,len(eligible))))
        print(f'Selected {len(selected):,} users with >=20 matched source ratings.',flush=True)
        stats=Counter(); profiles={}; genre_map={r['movie_id']:r['genres'] for r in metadata}
        def uid(source): return f'ml32m_{source:06d}'
        with writer('ratings',RATING_FIELDS) as write:
            def flush(source_uid,group):
                if not group: return
                # Multiple source IDs can map to one catalog work; retain its latest real rating.
                latest={}
                for mid,source_mid,score,at in group:
                    if mid not in latest or (at,source_mid)>(latest[mid][3],latest[mid][1]): latest[mid]=(mid,source_mid,score,at)
                values=sorted(latest.values(),key=lambda v:(v[3],v[0]))
                stats['duplicate_mapped_ratings_removed']+=len(group)-len(values)
                n=len(values); train=max(1,int(n*.8)); valid=max(train+1,int(n*.9))
                genres=Counter(); fav=[]; first=values[0][3]; last=values[-1][3]
                for i,(mid,source_mid,score,at) in enumerate(values):
                    split='train' if i<train else 'validation' if i<valid else 'test'
                    stats['real_ratings']+=1; stats['split_'+split]+=1
                    write([f'r_{stats["real_ratings"]}',uid(source_uid),mid,int(score*2),score,at,source_mid,split,'movielens_32m',0])
                    if split=='train':
                        genres.update({g:max(0,score-2.5) for g in genre_map[mid]})
                        fav.append((score,at,mid))
                profiles[uid(source_uid)]=dict(source_user_id=source_uid,first=first,last=last,real_rating_count=n,
                    genres=[g for g,_ in genres.most_common(5)],favorites=[m for _,_,m in sorted(fav,reverse=True)[:12]])
            current=None; group=[]
            for n,row in enumerate(archive_rows(archive,'ratings.csv'),1):
                source_uid=int(row['userId'])
                if source_uid not in selected: continue
                if current is not None and current!=source_uid: flush(current,group); group=[]
                current=source_uid
                mid=mapping.get(int(row['movieId']))
                if mid: group.append((mid,int(row['movieId']),float(row['rating']),int(row['timestamp'])))
            if current is not None: flush(current,group)
        tag_fields=['tag_id','user_id','movie_id','tag','created_at','source_movie_id','source','is_synthetic']
        with writer('tags',tag_fields) as write:
            for row in archive_rows(archive,'tags.csv'):
                source_uid=int(row['userId']); mid=mapping.get(int(row['movieId']))
                if source_uid in selected and mid:
                    stats['real_tags']+=1
                    write([f't_{stats["real_tags"]}',uid(source_uid),mid,row['tag'],int(row['timestamp']),row['movieId'],'movielens_32m',0])
                    profiles[uid(source_uid)]['first']=min(profiles[uid(source_uid)]['first'],int(row['timestamp']))
    with writer('movie_mapping',['source_movie_id','movie_id','match_source']) as write:
        for source_mid,mid in sorted(mapping.items()): write([source_mid,mid,'imdb_or_movie_tmdb_id'])
    # Internal build state contains only anonymous MovieLens IDs and training-derived features.
    dump('_profiles.json',profiles)
    manifest=dict(seed=SEED,as_of=AS_OF,database=str(Path(args.database).resolve()),database_sha256_before=sha_before,
        database_sha256_after_prepare=digest(args.database),catalog_snapshot_counts=db_counts,
        users=len(profiles),eligible_source_users=len(eligible),matched_catalog_titles=len(set(mapping.values())),mapping_conflicts=conflicts,
        stats=dict(stats),files=FILES,source_url='https://files.grouplens.org/datasets/movielens/ml-32m.zip',
        source_checksums=EXPECTED_MD5,source_zip_sha256=digest(args.archive))
    assert manifest['database_sha256_before']==manifest['database_sha256_after_prepare'],'Application DB changed while preparing; inspect external writers.'
    dump('prepare_manifest.json',manifest)
    print(jd({k:v for k,v in manifest.items() if k in ('users','matched_catalog_titles','stats')}),flush=True)


TABLES = {
    'events':['event_id','user_id','movie_id','event_type','event_at_ms','session_id','request_id','payload','source','is_synthetic'],
    'users':['user_id','source_user_id','display_name','email','registered_at','locale','timezone','real_rating_count','preferred_genres','preferred_content_types','preferred_languages','preferred_duration_min','preferred_actor_ids','preferred_director_ids','profile_source','is_synthetic_profile'],
    'recommendation_requests':['request_id','user_id','shown_at_ms','surface','policy','source','is_synthetic'],
    'impressions':['impression_id','request_id','user_id','movie_id','rank','shown_at_ms','clicked','source','is_synthetic'],
    'clicks':['click_id','impression_id','request_id','user_id','movie_id','clicked_at_ms','source','is_synthetic'],
    'searches':['search_id','user_id','query','query_type','searched_at_ms','selected_movie_id','source','is_synthetic'],
    'watch_sessions':['session_id','user_id','movie_id','episode_id','room_id','request_id','started_at_ms','ended_at_ms','duration_seconds','start_position','end_position','watched_seconds','paused_seconds','buffered_seconds','completed','start_kind','view_number','stop_reason','playback_source','source','is_synthetic'],
    'bookmark_events':['bookmark_event_id','user_id','movie_id','action','event_at_ms','collection','source','is_synthetic'],
    'watchlist':['user_id','movie_id','added_at_ms','source','is_synthetic'],
    'not_interested':['user_id','movie_id','is_active','reason','updated_at_ms','source','is_synthetic'],
    'watched':['user_id','movie_id','marked_at_ms','reason','source','is_synthetic'],
    'progress':['user_id','movie_id','episode_id','position','duration','watched_seconds','completed','episodes_completed','episodes_modeled','updated_at_ms','source','is_synthetic'],
    'episode_progress':['user_id','movie_id','episode_id','position','duration','watched_seconds','completed','updated_at_ms','source','is_synthetic'],
    'ratings_synthetic':['rating_id','user_id','movie_id','score','rated_at','split','source','is_synthetic'],
    'reviews':['review_id','user_id','movie_id','rating_id','score','text','created_at','source','is_synthetic'],
    'clips':['clip_id','user_id','movie_id','episode_id','session_id','title','start_seconds','end_seconds','format','pinned','created_at_ms','updated_at_ms','source','is_synthetic'],
    'rooms':['room_id','movie_id','episode_id','host_user_id','title','created_at_ms','ended_at_ms','source','is_synthetic'],
    'room_members':['room_id','user_id','session_id','is_host','joined_at_ms','left_at_ms','source','is_synthetic'],
    'movie_stats':['movie_id','real_rating_count','real_rating_mean','synthetic_rating_count','synthetic_rating_mean','impressions','clicks','sessions','completed_sessions','total_watched_seconds','active_watchlist_users','active_not_interested_users','source']
    ,'user_activity':['user_id','first_source_activity_at','last_real_rating_at','last_event_at_ms','days_since_last_event','real_rating_count','synthetic_event_count','sessions','completed_sessions','watched_seconds','source']
}


class Simulator:
    def __init__(self,movies,episodes,profiles,outputs):
        self.movies,self.profiles,self.out=movies,profiles,outputs
        self.episodes=defaultdict(list)
        for ep in episodes: self.episodes[ep['movie_id']].append(ep)
        for eps in self.episodes.values(): eps.sort(key=lambda ep:(ep['season'],ep['episode']))
        self.ids=sorted(mid for mid,m in movies.items() if not m['is_hidden'])
        self.id_set=set(self.ids)
        self.by_genre=defaultdict(list)
        for mid in self.ids:
            for genre in movies[mid]['genres']: self.by_genre[genre].append(mid)
        self.sequence=Counter(); self.movie_stats=defaultdict(Counter)
        self.watchlist={}; self.dismissed={}; self.watched={}; self.progress={}; self.ep_progress={}; self.syn_ratings={}
        self.user_bookmarks=defaultdict(set)
        self.registered={}; self.view_numbers=Counter(); self.firstlast={}
        self.user_stats=defaultdict(Counter)

    def ident(self,kind):
        self.sequence[kind]+=1
        return f'{kind}_{self.sequence[kind]}'

    def emit(self,user,mid,kind,at,session='',request='',**payload):
        eid=self.ident('ev')
        self.out['events']([eid,user,mid,kind,int(at),session,request,payload,'synthetic_v1',1])
        self.firstlast[user]=max(self.firstlast.get(user,0),int(at))
        self.user_stats[user]['events']+=1
        return eid

    def choose(self,user,rng,used=()):
        profile=self.profiles[user]
        favorites=[mid for mid in profile['favorites'] if mid in self.id_set]
        if rng.random()<.3 and favorites: pool=favorites
        elif rng.random()<.75 and profile['genres']: pool=self.by_genre.get(rng.choice(profile['genres']),self.ids)
        else: pool=self.ids
        for _ in range(30):
            mid=rng.choice(pool)
            if mid not in used and not self.dismissed.get((user,mid),{}).get('active'): return mid
        return rng.choice([mid for mid in self.ids if mid not in used])

    def bookmark(self,user,mid,action,at):
        key=user,mid
        if action=='add':
            if key in self.watchlist: return
            self.watchlist[key]=at
            self.user_bookmarks[user].add(mid)
            if self.dismissed.get(key,{}).get('active'):
                self.dismissed[key].update(active=0,at=at)
                self.emit(user,mid,'not_interested_remove',at)
        else:
            if key not in self.watchlist: return
            del self.watchlist[key]
            self.user_bookmarks[user].discard(mid)
        bid=self.ident('bm')
        self.out['bookmark_events']([bid,user,mid,action,int(at),'watchlist','synthetic_v1',1])
        self.emit(user,mid,'bookmark_'+action,at,bookmark_event_id=bid)

    def dismiss(self,user,mid,at,rng):
        key=user,mid
        if key in self.watchlist: self.bookmark(user,mid,'remove',at)
        reason=rng.choice(['not_my_genre','not_in_mood','do_not_want_to_watch'])
        self.dismissed[key]=dict(active=1,reason=reason,at=at)
        self.emit(user,mid,'not_interested',at,reason=reason)

    def session(self,user,mid,start,rng,request='',room='',episode=None,force_rewatch=False):
        movie=self.movies[mid]; sid=self.ident('ws'); key=user,mid
        ep=None
        if self.episodes.get(mid):
            eps=self.episodes[mid]
            if episode: ep=episode
            else: ep=next((e for e in eps if not self.ep_progress.get((user,mid,e['episode_id']),{}).get('completed')),eps[0])
        eid=ep['episode_id'] if ep else ''
        duration=int(ep['duration_seconds'] if ep else movie['duration_min']*60)
        old=self.ep_progress.get((user,mid,eid),{}) if ep else self.progress.get(key,{})
        position=0 if force_rewatch or old.get('completed') else float(old.get('position',0))
        position=min(position,duration*.92)
        start_kind='rewatch' if force_rewatch or old.get('completed') else 'resume' if position>0 else 'first'
        number_key=user,mid,eid; self.view_numbers[number_key]+=1
        affinity=len(set(self.profiles[user]['genres'])&set(movie['genres']))
        mode=rng.random()
        technical=mode<.09
        finish=not technical and mode<(.62+.035*min(affinity,3))
        ratio=1.0 if finish else rng.uniform(.04,.35) if technical else rng.uniform(.06,.85)
        advance=(duration-position)*ratio
        # Forward seeks can change position without counting as watched seconds.
        seek=advance*rng.uniform(.03,.12) if rng.random()<.12 and advance>120 else 0
        watched=max(1.0,advance-seek)
        paused=rng.randint(5,100) if rng.random()<.23 else 0
        buffered=rng.randint(3,45) if technical or rng.random()<.10 else 0
        end=start+int((watched+paused+buffered)*1000)
        completed=int(position+advance>=duration*.93)
        reason='technical_problem' if technical else 'finished' if completed else 'lost_interest' if rng.random()<.8 else 'user_exit'
        self.emit(user,mid,'play_start',start,sid,request,episode_id=eid,room_id=room,start_position=round(position,3))
        if paused:
            self.emit(user,mid,'pause',start+1000,sid,request)
            self.emit(user,mid,'resume',start+1000+paused*1000,sid,request)
        if seek: self.emit(user,mid,'seek',start+min(int((end-start)/2),max(1000,paused*1000+2000)),sid,request,skipped_seconds=round(seek,3))
        if buffered:
            self.emit(user,mid,'buffer_start',end-buffered*1000,sid,request)
            self.emit(user,mid,'buffer_end',end,sid,request)
        self.emit(user,mid,'play_end',end,sid,request,stop_reason=reason,position=round(position+advance,3),completed=completed)
        self.out['watch_sessions']([sid,user,mid,eid,room,request,int(start),int(end),duration,round(position,3),round(position+advance,3),round(watched,3),paused,buffered,completed,start_kind,self.view_numbers[number_key],reason,'synthetic_player', 'synthetic_v1',1])
        previous=self.progress.get(key,{})
        total=previous.get('watched',0)+watched
        if ep:
            self.ep_progress[(user,mid,eid)]=dict(position=position+advance,duration=duration,watched=old.get('watched',0)+watched,
                completed=int(completed or old.get('completed',0)),at=end)
            eps_done=sum(self.ep_progress.get((user,mid,e['episode_id']),{}).get('completed',0) for e in self.episodes[mid])
            film_done=int(eps_done==len(self.episodes[mid]))
        else: eps_done=0; film_done=int(completed or previous.get('completed',0))
        self.progress[key]=dict(episode_id=eid,position=position+advance,duration=duration,watched=total,completed=film_done,
            episodes_completed=eps_done,episodes_modeled=len(self.episodes.get(mid,[])),at=end)
        if film_done:
            self.watched[key]=dict(at=end,reason='auto_completed')
            self.emit(user,mid,'watched_mark',end,reason='auto_completed')
        self.movie_stats[mid].update(sessions=1,completed_sessions=completed,total_watched_seconds=watched)
        self.user_stats[user].update(sessions=1,completed_sessions=completed,watched_seconds=watched)
        if completed and rng.random()<.35:
            score=max(1,min(10,round(6.3+.5*min(affinity,3)+rng.gauss(0,1.5))))
            rid=self.ident('sr'); self.syn_ratings[key]=dict(id=rid,score=score,at=int(end/1000)+1)
            self.emit(user,mid,'rating_set',end+1000,rating_id=rid,score=score)
        if watched>90 and rng.random()<.045:
            cid=self.ident('cl'); length=rng.randint(10,60)
            clip_end=max(length,min(duration,position+advance)); clip_start=max(0,clip_end-length)
            pinned=int(rng.random()<.45); created=end+2000
            self.out['clips']([cid,user,mid,eid,sid,f'Момент из «{movie["title"]}»',round(clip_start,3),round(clip_end,3),rng.choice(['landscape','portrait']),pinned,int(created),int(created+1000 if pinned else created),'synthetic_v1',1])
            self.emit(user,mid,'clip_create',created,sid,clip_id=cid)
            if pinned: self.emit(user,mid,'clip_pin',created+1000,sid,clip_id=cid)
        return end,sid,completed,ep

    def user(self,user,index):
        profile=self.profiles[user]; rng=random.Random(SEED+profile['source_user_id']*101)
        registered=profile['first']-rng.randint(1,30)*86400; self.registered[user]=registered
        favorites=profile['favorites'] or self.ids[:3]
        people=[p for mid in favorites[:2] for p in self.movies[mid]['actors'] if p.get('person_id')]
        directors=[p for mid in favorites[:2] for p in self.movies[mid]['directors'] if p.get('person_id')]
        types=['movie','series'] if rng.random()<.45 else ['movie']
        locale='ru-RU' if rng.random()<.85 else 'en-US'
        self.out['users']([user,profile['source_user_id'],f'Test viewer {index:05}',f'{user}@example.invalid',registered,locale,'UTC',profile['real_rating_count'],profile['genres'],types,['ru','en'] if locale=='ru-RU' else ['en'],[70,rng.choice([120,150,180,240])],list(dict.fromkeys(p['person_id'] for p in people))[:5],list(dict.fromkeys(p['person_id'] for p in directors))[:3],'synthetic_from_train_only',1])
        self.emit(user,None,'onboarding_preferences',registered*1000,genres=profile['genres'],content_types=types,derived_from='real_train_ratings_only')
        last_unfinished=None; last_finished=None
        for request_index in range(3):
            at=(AS_OF-(28-request_index*10)*86400+rng.randint(0,8*3600))*1000
            req=self.ident('rq'); used=set(); selected=[]
            for _ in range(10):
                mid=self.choose(user,rng,used); used.add(mid); selected.append(mid)
            clicked=set(rng.sample(selected,rng.randint(1,3)))
            self.out['recommendation_requests']([req,user,at,rng.choice(['home_for_you','genre_rail','catalog_related']),'synthetic_sampling_v1','synthetic_v1',1])
            clock=at+5000
            if request_index==0 or rng.random()<.2:
                target=rng.choice(selected); movie=self.movies[target]
                kind=rng.choice(['title','genre','actor'])
                query=movie['title'] if kind=='title' else rng.choice(movie['genres']) if kind=='genre' else movie['actors'][0]['name']
                qid=self.ident('sq'); self.out['searches']([qid,user,query,kind,at-5000,target,'synthetic_v1',1])
                self.emit(user,None,'search',at-5000,search_id=qid,query=query,selected_movie_id=target)
            for rank,mid in enumerate(selected,1):
                imp=self.ident('im'); did_click=mid in clicked
                self.out['impressions']([imp,req,user,mid,rank,at,int(did_click),'synthetic_v1',1])
                self.emit(user,mid,'recommendation_impression',at,request=req,impression_id=imp,rank=rank)
                self.movie_stats[mid]['impressions']+=1
                if did_click:
                    click=self.ident('ck'); self.out['clicks']([click,imp,req,user,mid,int(clock),'synthetic_v1',1])
                    self.emit(user,mid,'card_click',clock,request=req,impression_id=imp,click_id=click)
                    self.movie_stats[mid]['clicks']+=1
                    if rng.random()<.4: self.bookmark(user,mid,'add',clock+500)
                    if rng.random()<.72:
                        clock,sid,done,ep=self.session(user,mid,clock+2000,rng,request=req)
                        if done and not ep: last_finished=mid
                        elif not done: last_unfinished=(mid,ep)
                        clock+=5000
                    else: clock+=rng.randint(5,60)*1000
                elif rng.random()<.07: self.dismiss(user,mid,clock+500,rng)
                elif rng.random()<.08: self.bookmark(user,mid,'add',clock+1000)
            # Removing an earlier bookmark is a separate dated action.
            active=sorted(self.user_bookmarks[user])
            if active and rng.random()<.5: self.bookmark(user,rng.choice(active),'remove',clock+5000)
        extra=(AS_OF-6*86400+rng.randint(0,4*3600))*1000
        if last_unfinished and rng.random()<.25:
            self.session(user,last_unfinished[0],extra,rng,episode=last_unfinished[1])
        elif last_finished and rng.random()<.25:
            self.session(user,last_finished,extra,rng,force_rewatch=True)
        if favorites and rng.random()<.25:
            mid=rng.choice(favorites); mark=(AS_OF-5*86400)*1000
            self.watched[(user,mid)]=dict(at=mark,reason='manual_assumption_from_train_rating')
            self.emit(user,mid,'watched_mark',mark,reason='manual_assumption_from_train_rating')

    def rooms(self):
        rng=random.Random(SEED+777); users=sorted(self.profiles); rng.shuffle(users); offset=0
        for index in range(min(5000,len(users)//4)):
            size=rng.randint(3,5); members=users[offset:offset+size]; offset+=size
            if len(members)<2: break
            host=members[0]; mid=self.choose(host,rng); room=self.ident('rm')
            episode=self.episodes[mid][0] if self.episodes.get(mid) else None
            at=(AS_OF-3*86400+rng.randint(0,30*3600))*1000
            ends=[]
            self.emit(host,mid,'room_create',at,room_id=room)
            for number,user in enumerate(members):
                joined=at+number*1000
                self.emit(user,mid,'room_join',joined,room_id=room)
                end,sid,_,_=self.session(user,mid,joined+1000,rng,room=room,episode=episode)
                ends.append(end)
                self.out['room_members']([room,user,sid,int(user==host),joined,int(end),'synthetic_v1',1])
                self.emit(user,mid,'room_leave',end,room_id=room)
            self.out['rooms']([room,mid,episode['episode_id'] if episode else '',host,f'Тестовый совместный просмотр {index+1}',at,int(max(ends)),'synthetic_v1',1])

    def snapshots(self):
        for (user,mid),at in sorted(self.watchlist.items()):
            self.out['watchlist']([user,mid,int(at),'synthetic_v1',1]); self.movie_stats[mid]['active_watchlist_users']+=1
        for (user,mid),v in sorted(self.dismissed.items()):
            self.out['not_interested']([user,mid,v['active'],v['reason'],int(v['at']),'synthetic_v1',1])
            self.movie_stats[mid]['active_not_interested_users']+=v['active']
        for (user,mid),v in sorted(self.watched.items()): self.out['watched']([user,mid,int(v['at']),v['reason'],'synthetic_v1',1])
        for (user,mid),v in sorted(self.progress.items()): self.out['progress']([user,mid,v['episode_id'],round(v['position'],3),v['duration'],round(v['watched'],3),v['completed'],v['episodes_completed'],v['episodes_modeled'],int(v['at']),'synthetic_v1',1])
        for (user,mid,eid),v in sorted(self.ep_progress.items()): self.out['episode_progress']([user,mid,eid,round(v['position'],3),v['duration'],round(v['watched'],3),v['completed'],int(v['at']),'synthetic_v1',1])
        for (user,mid),v in sorted(self.syn_ratings.items()):
            self.out['ratings_synthetic']([v['id'],user,mid,v['score'],v['at'],'synthetic_only','synthetic_v1',1])
            self.movie_stats[mid].update(synthetic_rating_count=1,synthetic_rating_sum=v['score'])
        # Source ratings are real. Reviews below are generated text, not MovieLens reviews.
        review_counts=Counter()
        for row in read('ratings'):
            mid=int(row['movie_id']); score=int(row['score']); user=row['user_id']
            self.movie_stats[mid].update(real_rating_count=1,real_rating_sum=score)
            if row['split']=='train' and review_counts[user]<2 and (int(row['source_movie_id'])+self.profiles[user]['source_user_id'])%37==0:
                review_counts[user]+=1; rid=self.ident('rv'); at=int(row['rated_at'])+120
                text=(f'Тестовый отзыв: понравилась история «{self.movies[mid]["title"]}».' if score>=8 else
                      f'Тестовый отзыв: неоднозначные впечатления от «{self.movies[mid]["title"]}».' if score>=5 else
                      f'Тестовый отзыв: «{self.movies[mid]["title"]}» не совпадает с моими предпочтениями.')
                self.out['reviews']([rid,user,mid,row['rating_id'],score,text,at,'synthetic_template_from_real_train_rating',1])
                self.emit(user,mid,'review_create',at*1000,review_id=rid,rating_id=row['rating_id'])
        for mid in sorted(self.movies):
            s=self.movie_stats[mid]; real=s['real_rating_count']; synthetic=s['synthetic_rating_count']
            self.out['movie_stats']([mid,real,round(s['real_rating_sum']/real,4) if real else None,synthetic,round(s['synthetic_rating_sum']/synthetic,4) if synthetic else None,
                s['impressions'],s['clicks'],s['sessions'],s['completed_sessions'],round(s['total_watched_seconds'],3),s['active_watchlist_users'],s['active_not_interested_users'],'derived_from_this_dataset'])
        for user,p in sorted(self.profiles.items()):
            s=self.user_stats[user]; last=self.firstlast[user]
            self.out['user_activity']([user,p['first'],p['last'],last,round((AS_OF*1000-last)/86400000,6),p['real_rating_count'],s['events'],s['sessions'],s['completed_sessions'],round(s['watched_seconds'],3),'derived_from_this_dataset'])


def simulate(args):
    manifest=load('prepare_manifest.json'); FILES.update(manifest['files'])
    profiles=load('_profiles.json')
    movies={int(r['movie_id']):r for r in read('movies')}
    for movie in movies.values():
        for key in ('genres','actors','directors'): movie[key]=json.loads(movie[key])
        movie['duration_min']=int(movie['duration_min']); movie['is_hidden']=int(movie['is_hidden'])
    episodes=[]
    for row in read('episodes'):
        for key in ('movie_id','season','episode','duration_seconds'): row[key]=int(row[key])
        episodes.append(row)
    with ExitStack() as stack:
        outputs={name:stack.enter_context(writer(name,fields)) for name,fields in TABLES.items()}
        sim=Simulator(movies,episodes,profiles,outputs)
        for index,user in enumerate(sorted(profiles),1):
            sim.user(user,index)
            if index%5000==0: print(f'Simulated {index:,}/{len(profiles):,} users; {sim.sequence["ws"]:,} playback sessions.',flush=True)
        sim.rooms(); sim.snapshots()
    manifest.update(files=FILES,synthetic_counts=dict(sim.sequence),database_sha256_after_all=digest(args.database),
        synthetic_policy=dict(requests_per_user=3,impressions_per_request=10,seed=SEED,as_of_utc='2026-10-31T12:00:00Z',
        real_rating_split='per-user chronological 80/10/10; equal timestamps ordered by catalog movie ID',
        onboarding='generated from real training portion only',simulated_playback_available_for_all=True),
        allowed_catalog_movie_ids=sorted(movies),finalized=True)
    manifest['database_main_file_unchanged']=manifest['database_sha256_before']==manifest['database_sha256_after_all']
    assert manifest['database_main_file_unchanged'],'Application DB changed while building; inspect external writers.'
    dump('manifest.json',manifest)
    dump('schema.json',dict(format='UTF-8 comma-separated CSV compressed with gzip; nested values are JSON',
        times='Epoch seconds unless column ends with _ms; then milliseconds. All UTC.',
        files={name:info['columns'] for name,info in FILES.items()},
        sources=dict(movielens_32m='Real anonymized external users, ratings and tags',catalog='Read-only application movie snapshot',
            tmdb_cache='Existing official API response cache',imdb_official_snapshot='Existing official IMDb rating dataset',
            synthetic_v1='Seeded simulation; not observed behavior',synthetic_template_from_real_train_rating='Generated review text based on a real training rating'),
        keys=dict(movie_id='Existing films.id from the catalog snapshot',user_id='ml32m_ prefixed anonymous MovieLens ID; not an application account ID',
            episode_id='Dataset episode ID; source_file_id links to existing catalog episodes when available'),
        nulls='Empty scalar CSV cell is unknown or not applicable, not numeric zero. Empty collection is [].'))
    print(jd({'users':len(profiles),'movies':len(movies),'tables':len(FILES),'rows':sum(v['rows'] for v in FILES.values()),'compressed_mb':round(sum(v['bytes'] for v in FILES.values())/1024**2,1),'database_unchanged':manifest['database_main_file_unchanged']}),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',type=Path,default=ROOT/'.local/cinema.sqlite3')
    parser.add_argument('--archive',type=Path,default=ROOT/'.local/dataset-sources/ml-32m.zip')
    parser.add_argument('--users',type=int,default=50_000)
    parser.add_argument('--stage',choices=('prepare','simulate','all'),default='all')
    args=parser.parse_args()
    if args.stage in ('prepare','all'): prepare(args)
    if args.stage in ('simulate','all'): simulate(args)


if __name__=='__main__': main()
