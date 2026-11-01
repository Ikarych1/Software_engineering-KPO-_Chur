"""Validate the unified standalone CSV dataset without opening the site database."""
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
COUNTS=Counter()
SCHEMA=json.loads((HERE/'schema.json').read_text(encoding='utf-8'))['files']
NULL={
    'movies':{'imdb_id','tmdb_id','collection_id','collection_name'},
    'episodes':{'source_file_id'},'ratings':{'source_movie_id','split'},
    'watch_sessions':{'episode_id','room_id','request_id'},'progress':{'episode_id'},
    'rooms':{'episode_id'},'clips':{'episode_id'},'events':{'movie_id','session_id','request_id'},
    'movie_stats':{'rating_mean'}}


def check(condition,message):
    if not condition: raise AssertionError(message)


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while block:=f.read(1024*1024): h.update(block)
    return h.hexdigest()


def read(name):
    check(name not in COUNTS,'A table must be audited in one complete pass: '+name)
    COUNTS[name]=0
    with gzip.open(HERE/'data'/(name+'.csv.gz'),'rt',encoding='utf-8',newline='') as f:
        rows=csv.DictReader(f)
        check(rows.fieldnames==SCHEMA[name],'Header mismatch: '+name)
        nullable=NULL.get(name,set())
        for row in rows:
            COUNTS[name]+=1
            check(all(value!='' or col in nullable for col,value in row.items()),'Unexpected missing value: '+name)
            yield row


def main():
    manifest=json.loads((HERE/'manifest.json').read_text(encoding='utf-8'))
    lineage=json.loads((HERE/'provenance.json').read_text(encoding='utf-8'))
    checks=[]
    check(len(SCHEMA)==24 and 'ratings_synthetic' not in SCHEMA,'Duplicate ratings table')
    check(all(not any(w in c for w in ('synthetic','simulated','real_rating','modeled'))
              for fields in SCHEMA.values() for c in fields),'Generation fields in business schema')
    users={r['user_id']:r for r in read('users')}
    movies={int(r['movie_id']):r for r in read('movies')}
    episodes={r['episode_id']:r for r in read('episodes')}
    mapping={r['source_movie_id']:int(r['movie_id']) for r in read('movie_mapping')}
    check(len(users)==manifest['users']==COUNTS['users'],'User keys/count')
    check(len(movies)==COUNTS['movies'] and set(movies)==set(manifest['allowed_catalog_movie_ids']),'Catalog keys/count')
    check(len(episodes)==COUNTS['episodes'] and len(mapping)==COUNTS['movie_mapping'],'Episode/mapping keys')
    check(set(mapping.values())<=set(movies),'Out-of-catalog mapping')
    check(all(r['email'].endswith('@example.invalid') for r in users.values()),'Non-test email')
    episode_keys=set(); ep_counts=Counter()
    for row in episodes.values():
        mid=int(row['movie_id']); key=(mid,row['season'],row['episode'])
        check(mid in movies and movies[mid]['catalog_kind']=='series' and key not in episode_keys,'Episode linkage/unique key')
        check(float(row['duration_seconds'])>0,'Episode duration')
        episode_keys.add(key); ep_counts[mid]+=1
    for row in movies.values():
        check(row['has_video_source'] in ('0','1'),'Video availability boolean')
        for col in ('genres','countries','country_codes','spoken_languages','actors','directors'):
            check(isinstance(json.loads(row[col]),list),'Invalid movie JSON: '+col)
        for col in ('imdb_rating','tmdb_rating','kinopoisk_rating'):
            check(0<=float(row[col])<=10,'Invalid movie rating')
    checks.append('24 tables; one value per feature; allowed catalog IDs; complete fields and optional external IDs')

    requests={r['request_id']:(r['user_id'],int(r['shown_at_ms'])) for r in read('recommendation_requests')}
    check(len(requests)==COUNTS['recommendation_requests'],'Duplicate request key')
    check(all(uid in users for uid,_ in requests.values()),'Unknown request user')
    request_masks=Counter(); clicked={}; movie_stats=defaultdict(Counter)
    current_request=None; finished=set(); request_movies=set()
    for row in read('impressions'):
        uid,mid,req=row['user_id'],int(row['movie_id']),row['request_id']; rank=int(row['rank'])
        check(uid in users and mid in movies and req in requests,'Impression FK')
        check(requests[req]==(uid,int(row['shown_at_ms'])),'Impression/request mismatch')
        if req!=current_request:
            check(req not in finished,'Interleaved request groups')
            finished.add(req); current_request=req; request_movies=set()
        check(mid not in request_movies,'Duplicate movie in request'); request_movies.add(mid)
        check(1<=rank<=10 and movies[mid]['is_hidden']=='0','Invalid impression rank/hidden movie')
        mask=1<<(rank-1); check(not request_masks[req]&mask,'Duplicate request rank')
        request_masks[req]|=mask; movie_stats[mid]['impressions']+=1
        if row['clicked']=='1': clicked[row['impression_id']]=(req,uid,mid,int(row['shown_at_ms']))
    check(len(request_masks)==len(requests) and all(v==1023 for v in request_masks.values()),'Incomplete request')
    for row in read('clicks'):
        target=clicked.pop(row['impression_id'],None)
        check(target is not None,'Unknown/duplicate click impression')
        req,uid,mid,at=target
        check((row['request_id'],row['user_id'],int(row['movie_id']))==(req,uid,mid),'Click owner/target/request')
        check(int(row['clicked_at_ms'])>=at,'Click before impression'); movie_stats[mid]['clicks']+=1
    check(not clicked,'Clicked impressions without clicks')
    for row in read('searches'):
        check(row['user_id'] in users and int(row['selected_movie_id']) in movies,'Search FK')
    checks.append('request/impression/click/search FKs, composite uniqueness, ranks, clicked flags and timestamps')

    sessions={}; user_stats=defaultdict(Counter); totals=Counter(); ep_totals=Counter()
    latest={}; latest_ep={}; completed=defaultdict(set); intervals=defaultdict(list); view_numbers=Counter()
    for row in read('watch_sessions'):
        sid,uid,mid,eid=row['session_id'],row['user_id'],int(row['movie_id']),row['episode_id']
        check(uid in users and mid in movies and sid not in sessions,'Session FK/key')
        if eid: check(eid in episodes and int(episodes[eid]['movie_id'])==mid,'Session episode/movie')
        start,end=int(row['started_at_ms']),int(row['ended_at_ms']); duration=float(row['duration_seconds'])
        a,b,watched=map(float,(row['start_position'],row['end_position'],row['watched_seconds']))
        done=int(row['completed'])
        check(0<=a<=b<=duration+.001 and watched>0,'Session positions')
        check(abs((end-start)/1000-watched-float(row['paused_seconds'])-float(row['buffered_seconds']))<.003,'Session timing')
        check(done==int(b>=duration*.93),'Completion threshold')
        if row['request_id']: check(row['request_id'] in requests and requests[row['request_id']][0]==uid,'Session request owner')
        sessions[sid]=(uid,mid,eid,start,end,row['room_id'],duration)
        key=uid,mid; totals[key]+=watched; ep_totals[(uid,mid,eid)]+=watched
        latest[key]=(end,b,duration,eid)
        if eid: latest_ep[(uid,mid,eid)]=(end,b,duration)
        if done: completed[key].add(eid)
        intervals[uid].append((start,end)); view_numbers[(uid,mid,eid)]+=1
        check(int(row['view_number'])==view_numbers[(uid,mid,eid)],'Repeat counter')
        movie_stats[mid].update(sessions=1,completed_sessions=done,total_watched_seconds=watched)
        user_stats[uid].update(sessions=1,completed_sessions=done,watched_seconds=watched)
    for values in intervals.values():
        ordered=sorted(values); check(all(a[1]<=b[0] for a,b in zip(ordered,ordered[1:])),'Overlapping user sessions')
    progress_keys=set()
    for row in read('progress'):
        key=row['user_id'],int(row['movie_id'])
        check(key in latest and key not in progress_keys,'Progress key/FK'); progress_keys.add(key)
        end,pos,duration,eid=latest[key]
        check((int(row['updated_at_ms']),row['episode_id'])==(end,eid) and abs(float(row['position'])-pos)<.002,'Latest progress snapshot')
        check(abs(float(row['watched_seconds'])-totals[key])<.03 and abs(float(row['duration'])-duration)<.002,'Progress time/duration')
        expected=int(len(completed[key])==ep_counts[key[1]]) if ep_counts[key[1]] else int('' in completed[key])
        check(int(row['completed'])==expected and int(row['episode_count'])==ep_counts[key[1]],'Progress completion/episode count')
    check(progress_keys==set(totals),'Missing progress')
    seen_ep=set()
    for row in read('episode_progress'):
        key=row['user_id'],int(row['movie_id']),row['episode_id']
        check(key in latest_ep and key not in seen_ep,'Episode progress FK/key'); seen_ep.add(key)
        end,pos,duration=latest_ep[key]
        check(abs(float(row['watched_seconds'])-ep_totals[key])<.03,'Episode accumulated time')
        check(int(row['updated_at_ms'])==end and abs(float(row['position'])-pos)<.002 and abs(float(row['duration'])-duration)<.002,'Episode snapshot')
    check(seen_ep==set(latest_ep),'Missing episode progress')
    checks.append('session positions, timing, repeats, no overlaps, episode FKs and reconciled progress')

    rooms={r['room_id']:r for r in read('rooms')}; memberships=set(); member_sessions=set(); hosts=Counter()
    check(len(rooms)==COUNTS['rooms'],'Room key')
    for room in rooms.values():
        mid=int(room['movie_id']); eid=room['episode_id']
        check(mid in movies and room['host_user_id'] in users,'Room movie/host FK')
        if eid: check(eid in episodes and int(episodes[eid]['movie_id'])==mid,'Room episode FK')
    for row in read('room_members'):
        room=rooms.get(row['room_id']); uid=row['user_id']; key=row['room_id'],uid
        check(room is not None and uid in users and key not in memberships,'Room member FK/key')
        s=sessions.get(row['session_id']); check(s is not None and row['session_id'] not in member_sessions,'Member session FK/UK')
        memberships.add(key); member_sessions.add(row['session_id'])
        check((s[0],s[1],s[2],s[5])==(uid,int(room['movie_id']),room['episode_id'],row['room_id']),'Room/session consistency')
        check(int(room['created_at_ms'])<=int(row['joined_at_ms'])<=s[3]<=s[4]==int(row['left_at_ms'])<=int(room['ended_at_ms']),'Room timing')
        if row['is_host']=='1': check(room['host_user_id']==uid,'Room host'); hosts[row['room_id']]+=1
    check(all(hosts[rid]==1 for rid in rooms),'Room host membership')
    for s in sessions.values():
        if s[5]: check((s[5],s[0]) in memberships,'Session without room membership')
    for row in read('clips'):
        s=sessions.get(row['session_id'])
        check(s is not None and (row['user_id'],int(row['movie_id']),row['episode_id'])==s[:3],'Clip FKs/owner')
        a,b=float(row['start_seconds']),float(row['end_seconds'])
        check(0<=a<b<=s[6]+.002 and b-a<=60.002,'Clip boundaries')
    checks.append('room/host/member/session links, membership uniqueness and clip ownership/bounds')

    active={}; last_bookmark={}
    for row in read('bookmark_events'):
        key=row['user_id'],int(row['movie_id']); at=int(row['event_at_ms'])
        check(key[0] in users and key[1] in movies and at>=last_bookmark.get(key,0),'Bookmark FK/time')
        last_bookmark[key]=at
        if row['action']=='add': check(key not in active,'Double bookmark'); active[key]=at
        else: check(row['action']=='remove' and key in active,'Remove absent bookmark'); active.pop(key)
    watchlist={}
    for row in read('watchlist'):
        key=row['user_id'],int(row['movie_id']); check(key not in watchlist,'Duplicate watchlist pair')
        watchlist[key]=int(row['added_at_ms']); movie_stats[key[1]]['active_watchlist_users']+=1
    check(watchlist==active,'Bookmark history/snapshot')
    for table in ('watched','not_interested'):
        seen=set()
        for row in read(table):
            key=row['user_id'],int(row['movie_id'])
            check(key[0] in users and key[1] in movies and key not in seen,'State FK/key: '+table); seen.add(key)
            if table=='not_interested' and row['is_active']=='1':
                check(key not in active,'Conflicting active preferences'); movie_stats[key[1]]['active_not_interested_users']+=1
    checks.append('bookmark history replay and unique user/movie states')

    reviews={r['rating_id']:(r['user_id'],int(r['movie_id']),int(r['score'])) for r in read('reviews')}
    check(len(reviews)==COUNTS['reviews'],'Review/rating UK')
    digest=hashlib.sha256(); original_count=0; added_count=0; splits=Counter()
    user_ratings=Counter(); last_rating=Counter(); first_activity={}; filled={}
    def activity(uid,at): first_activity[uid]=min(first_activity.get(uid,at),at)
    current=None; rated=set(); seen_users=set(); previous_time=0; previous_split=-1
    split_order={'train':0,'validation':1,'test':2}
    for row in read('ratings'):
        uid,mid,score,at=row['user_id'],int(row['movie_id']),int(row['score']),int(row['rated_at'])
        check(uid in users and mid in movies and 1<=score<=10,'Rating FK/score')
        if uid!=current:
            check(uid not in seen_users,'Interleaved rating groups'); seen_users.add(uid)
            current=uid; rated=set(); previous_time=0; previous_split=-1
        check(mid not in rated,'Duplicate unified rating pair'); rated.add(mid)
        check(at>=previous_time and at>=int(users[uid]['registered_at']),'Rating chronology')
        previous_time=at
        user_ratings[uid]+=1; last_rating[uid]=max(last_rating[uid],at); activity(uid,at*1000)
        movie_stats[mid].update(rating_count=1,rating_sum=score)
        rid=row['rating_id']
        if rid.startswith('r_'):
            check(row['source_movie_id'] in mapping and mapping[row['source_movie_id']]==mid,'Original rating mapping')
            check(row['split'] in split_order and split_order[row['split']]>=previous_split,'Original rating split')
            previous_split=split_order[row['split']]; splits[row['split']]+=1; original_count+=1
            values=[row[c] for c in lineage['original_rating_projection_columns']]
            digest.update((json.dumps(values,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8'))
        else:
            check(rid.startswith('sr_') and not row['source_movie_id'] and not row['split'],'Invented source ID/split')
            check(rid not in filled,'Duplicate added rating ID'); filled[rid]=(uid,mid,score); added_count+=1
        if rid in reviews:
            check(reviews.pop(rid)==(uid,mid,score) and row['split']=='train','Review/rating consistency')
    check(not reviews,'Review without rating')
    check(digest.hexdigest()==lineage['original_rating_projection_sha256'],'An original rating value changed')
    check(original_count==lineage['original_ratings_preserved'] and added_count==lineage['filled_rating_pairs'],'Preservation/fill count')
    for uid,row in users.items(): check(int(row['rating_count'])==user_ratings[uid],'Profile rating count')
    for row in read('tags'):
        uid,mid=row['user_id'],int(row['movie_id'])
        check(uid in users and mid in movies and mapping.get(row['source_movie_id'])==mid,'Tag mapping/FKs')
        activity(uid,int(row['created_at'])*1000)
    checks.append('all original ratings preserved by value hash; only missing pairs added; one score per pair; reviews/mapping consistent')

    event_types=Counter(); last_event=Counter(); rating_events=set()
    for n,row in enumerate(read('events'),1):
        uid=row['user_id']; mid=int(row['movie_id']) if row['movie_id'] else None; at=int(row['event_at_ms'])
        check(row['event_id']==f'ev_{n}' and uid in users and (mid is None or mid in movies),'Event PK/FKs')
        check(int(users[uid]['registered_at'])*1000<=at<=manifest['as_of']*1000,'Event outside lifetime/snapshot')
        payload=json.loads(row['payload']); check(isinstance(payload,dict),'Event payload JSON')
        sid=row['session_id']
        if sid:
            s=sessions.get(sid); check(s is not None and (uid,mid)==s[:2],'Event/session owner')
            if row['event_type']=='play_start': check(at==s[3],'Start event time')
            if row['event_type']=='play_end': check(at==s[4],'End event time')
        if row['request_id']: check(requests.get(row['request_id'],(None,))[0]==uid,'Event request owner')
        if row['event_type']=='rating_set':
            rid=payload.get('rating_id')
            check(rid in filled and filled[rid]==(uid,mid,int(payload['score'])) and rid not in rating_events,'Conflicting/obsolete rating event')
            rating_events.add(rid)
        activity(uid,at); last_event[uid]=max(last_event[uid],at)
        user_stats[uid]['event_count']+=1; event_types[row['event_type']]+=1
    check(rating_events==set(filled),'Added rating without matching event')
    check(dict(event_types)==manifest['event_types'],'Event type counts')
    for row in read('user_activity'):
        uid=row['user_id']; s=user_stats[uid]
        check(int(row['first_activity_at'])==first_activity[uid]//1000,'First activity')
        check(int(row['last_rating_at'])==last_rating[uid] and int(row['rating_count'])==user_ratings[uid],'Rating recency/count')
        check(int(row['last_event_at_ms'])==last_event[uid] and int(row['event_count'])==s['event_count'],'Event recency/count')
        check(abs(float(row['days_since_last_event'])-(manifest['as_of']*1000-last_event[uid])/86400000)<.000001,'Recency days')
        for col in ('sessions','completed_sessions'): check(int(row[col])==s[col],'User session aggregate')
        check(abs(float(row['watched_seconds'])-s['watched_seconds'])<.03,'User watch time')
    for row in read('movie_stats'):
        mid=int(row['movie_id']); s=movie_stats[mid]
        for col in ('rating_count','impressions','clicks','sessions','completed_sessions','active_watchlist_users','active_not_interested_users'):
            check(int(row[col])==s[col],f'Movie aggregate: {mid}:{col}')
        if s['rating_count']: check(abs(float(row['rating_mean'])-s['rating_sum']/s['rating_count'])<.000051,'Unified rating mean')
        else: check(row['rating_mean']=='','Mean without ratings')
        check(abs(float(row['total_watched_seconds'])-s['total_watched_seconds'])<.03,'Movie watch time')
    checks.append('canonical rating events, event dates, user recency/counts and unified movie/user aggregates')

    original=HERE.parent/'cinema_ml32m_v1'
    metadata_preserved=None
    if original.is_dir():
        check(sha(original/'manifest.json')==lineage['source_manifest_sha256'],'Original manifest changed')
        with gzip.open(original/'data/movies.csv.gz','rt',encoding='utf-8',newline='') as f:
            for row in csv.DictReader(f):
                actual=movies[int(row['movie_id'])]
                check(all(actual[c]==row[c] for c in actual),'Existing movie value overwritten')
        metadata_preserved=True
        checks.append('every retained movie field equals the source snapshot; no metadata overwritten')
    check(set(COUNTS)==set(SCHEMA)==set(manifest['files']),'Missing audited tables')
    for name,info in manifest['files'].items():
        check(COUNTS[name]==info['rows'] and SCHEMA[name]==info['columns'],'Manifest count/schema: '+name)
        check(sha(HERE/info['file'])==info['sha256'],'Checksum: '+name)
    checks.append('all 24 file headers, row counts and SHA-256 checksums; application database never opened')
    report=dict(passed=True,checks=checks,counts=dict(COUNTS),fields=sum(map(len,SCHEMA.values())),
                original_ratings_preserved=original_count,filled_rating_pairs=added_count,
                original_rating_values_unchanged=True,source_metadata_unchanged=metadata_preserved,
                event_types=dict(event_types),original_splits=dict(splits),application_database_accessed=False)
    (HERE/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'tables':len(COUNTS),'rows':sum(COUNTS.values()),
                      'original_ratings_preserved':original_count,'filled_rating_pairs':added_count,
                      'application_database_accessed':False}),flush=True)


if __name__=='__main__': main()
