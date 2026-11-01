"""Full streaming integrity audit of the standalone dataset, without modifying the site."""
from collections import Counter, defaultdict
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

HERE=Path(__file__).resolve().parent
DATA=HERE/'data'


def read(name):
    with gzip.open(DATA/(name+'.csv.gz'),'rt',encoding='utf-8',newline='') as f:
        yield from csv.DictReader(f)


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while block:=f.read(1024*1024): h.update(block)
    return h.hexdigest()


def check(condition,message):
    if not condition: raise AssertionError(message)


def main():
    parser=argparse.ArgumentParser(description='Validate the isolated MovieLens/catalog fixture')
    parser.add_argument('--offline',action='store_true',help='Validate against the catalog snapshot manifest without opening the application database')
    args=parser.parse_args()
    manifest=json.loads((HERE/'manifest.json').read_text(encoding='utf-8'))
    counts=Counter(); checks=[]
    users={r['user_id']:r for r in read('users')}
    movies={int(r['movie_id']):r for r in read('movies')}
    episodes={r['episode_id']:r for r in read('episodes')}
    check(len(users)==manifest['users'],'User count mismatch')
    check(set(movies)==set(manifest['allowed_catalog_movie_ids']),'Unexpected or missing catalog titles')
    for user,row in users.items():
        check(user.startswith('ml32m_') and row['email'].endswith('@example.invalid'),'Users are not isolated anonymous fixtures')
        check(row['is_synthetic_profile']=='1','Generated profile not marked')
    for mid,row in movies.items():
        for field in ('genres','description','tagline','countries','country_codes','original_language','spoken_languages','actors','directors','age_rating'):
            check(bool(row[field]),f'Missing movie field {mid}:{field}')
        check(row['content_type'] in ('movie','series','miniseries'),'Bad content type')
        check(row['has_video_source'] in ('0','1') and row['simulated_playback_available']=='1','Video availability conflated')
        provenance=json.loads(row['metadata_provenance'])
        for field in json.loads(row['synthetic_fields']): check(provenance.get(field)=='synthetic','Unmarked synthetic metadata')
        check(provenance['kinopoisk_rating']=='synthetic','Fabricated Kinopoisk value appears real')
    for eid,row in episodes.items():
        mid=int(row['movie_id'])
        check(mid in movies and movies[mid]['catalog_kind']=='series','Episode not linked to a catalog series')
        check(float(row['duration_seconds'])>0,'Invalid episode duration')
    checks.append('catalog-only movie IDs, complete fields, field provenance, isolated anonymous users')

    requests={r['request_id']:(r['user_id'],int(r['shown_at_ms'])) for r in read('recommendation_requests')}
    request_masks=Counter(); clicked_impressions={}; movie_stats=defaultdict(Counter)
    for n,row in enumerate(read('impressions'),1):
        user,mid,request=row['user_id'],int(row['movie_id']),row['request_id']; rank=int(row['rank'])
        check(user in users and mid in movies and request in requests,'Bad impression foreign key')
        check(requests[request][0]==user and int(row['shown_at_ms'])==requests[request][1],'Impression request mismatch')
        check(movies[mid]['is_hidden']=='0','Hidden movie recommended')
        check(1<=rank<=10,'Bad recommendation rank')
        mask=1<<(rank-1); check(not request_masks[request]&mask,'Duplicate position in recommendation list')
        request_masks[request]|=mask; movie_stats[mid]['impressions']+=1
        if row['clicked']=='1': clicked_impressions[row['impression_id']]=(request,user,mid,int(row['shown_at_ms']))
    check(all(mask==1023 for mask in request_masks.values()),'Incomplete 10-item recommendation list')
    check(len(request_masks)==len(requests),'Empty recommendation request')
    for row in read('clicks'):
        expected=clicked_impressions.pop(row['impression_id'],None)
        check(expected is not None,'Click missing a clicked impression or duplicated')
        request,user,mid,at=expected
        check((row['request_id'],row['user_id'],int(row['movie_id']))==(request,user,mid),'Click linkage mismatch')
        check(int(row['clicked_at_ms'])>=at,'Click predates impression')
        movie_stats[mid]['clicks']+=1
    check(not clicked_impressions,'Clicked flags without click rows')
    checks.append('10 ranked impressions per request; clicks correspond to impressions; hidden movies excluded')

    sessions={}; totals=Counter(); ep_totals=Counter(); latest={}; latest_ep={}; completed=defaultdict(set)
    user_sessions=defaultdict(list); user_stats=defaultdict(Counter); view_numbers=Counter(); start_kinds=Counter(); reasons=Counter()
    for row in read('watch_sessions'):
        sid,user,mid,eid=row['session_id'],row['user_id'],int(row['movie_id']),row['episode_id']
        check(user in users and mid in movies,'Session foreign key missing')
        if eid: check(eid in episodes and int(episodes[eid]['movie_id'])==mid,'Session episode mismatch')
        check(sid not in sessions,'Duplicate session ID')
        start,end=int(row['started_at_ms']),int(row['ended_at_ms']); duration=float(row['duration_seconds'])
        a,b=float(row['start_position']),float(row['end_position']); watched=float(row['watched_seconds'])
        check(0<=a<=b<=duration+.001 and watched>0,'Invalid playback positions')
        elapsed=watched+float(row['paused_seconds'])+float(row['buffered_seconds'])
        check(abs((end-start)/1000-elapsed)<.003,'Playback wall time does not reconcile')
        done=int(row['completed']); check(done==int(b>=duration*.93),'Incorrect completion threshold')
        check(row['playback_source']=='synthetic_player' and row['is_synthetic']=='1','Synthetic viewing is mislabeled')
        if row['request_id']: check(row['request_id'] in requests and requests[row['request_id']][0]==user,'Session request mismatch')
        sessions[sid]=(user,mid,eid,start,end,row['room_id']); key=(user,mid)
        totals[key]+=watched; ep_totals[(user,mid,eid)]+=watched
        latest[key]=(end,b,duration,eid)
        if eid: latest_ep[(user,mid,eid)]=(end,b,duration)
        if done: completed[key].add(eid)
        view_numbers[(user,mid,eid)]+=1
        check(int(row['view_number'])==view_numbers[(user,mid,eid)],'Session repeat counter mismatch')
        user_sessions[user].append((start,end))
        movie_stats[mid].update(sessions=1,completed_sessions=done,total_watched_seconds=watched)
        user_stats[user].update(sessions=1,completed_sessions=done,watched_seconds=watched)
        start_kinds[row['start_kind']]+=1; reasons[row['stop_reason']]+=1
    for user,intervals in user_sessions.items():
        times=sorted(intervals)
        check(all(a[1]<=b[0] for a,b in zip(times,times[1:])),f'Overlapping sessions for {user}')
    ep_counts=Counter(int(r['movie_id']) for r in episodes.values())
    seen_progress=set()
    for row in read('progress'):
        user,mid=row['user_id'],int(row['movie_id']); key=user,mid; seen_progress.add(key)
        check(key in latest,'Progress without session')
        end,pos,duration,eid=latest[key]
        check(abs(float(row['watched_seconds'])-totals[key])<.03,'Accumulated watched seconds mismatch')
        check((int(row['updated_at_ms']),row['episode_id'])==(end,eid) and abs(float(row['position'])-pos)<.002,'Progress is not latest session snapshot')
        expected_done=int(len(completed[key])==ep_counts[mid]) if ep_counts[mid] else int('' in completed[key])
        check(int(row['completed'])==expected_done,'Series/movie completion semantics mismatch')
    check(seen_progress==set(totals),'Missing progress snapshots')
    seen_ep=set()
    for row in read('episode_progress'):
        key=row['user_id'],int(row['movie_id']),row['episode_id']; seen_ep.add(key)
        check(key in latest_ep,'Episode progress without session')
        check(abs(float(row['watched_seconds'])-ep_totals[key])<.03,'Episode watched time mismatch')
    check(seen_ep==set(latest_ep),'Missing episode progress')
    checks.append('session timing, positions, completion, no user overlaps, episode links and accumulated progress')

    rooms={r['room_id']:r for r in read('rooms')}; hosts=Counter(); membership=set()
    for row in read('room_members'):
        room=rooms.get(row['room_id']); user=row['user_id']; key=row['room_id'],user
        check(room is not None and user in users and key not in membership,'Room member linkage/duplicate')
        membership.add(key); s=sessions[row['session_id']]
        check(s[0]==user and s[1]==int(room['movie_id']) and s[2]==room['episode_id'] and s[5]==row['room_id'],'Room session mismatch')
        check(int(room['created_at_ms'])<=int(row['joined_at_ms'])<=s[3]<=s[4]==int(row['left_at_ms'])<=int(room['ended_at_ms']),'Room timing inconsistent')
        if row['is_host']=='1':
            check(room['host_user_id']==user,'Wrong room host'); hosts[row['room_id']]+=1
    check(all(hosts[rid]==1 for rid in rooms),'Missing or duplicate room host')
    for sid,s in sessions.items():
        if s[5]: check((s[5],s[0]) in membership,'Room viewing without membership')
    checks.append('joint-viewing rooms, members, hosts and session ownership')

    active_bookmarks={}; last_bookmark_times={}
    for row in read('bookmark_events'):
        key=row['user_id'],int(row['movie_id']); at=int(row['event_at_ms'])
        check(key[0] in users and key[1] in movies,'Bookmark foreign key')
        check(at>=last_bookmark_times.get(key,0),'Bookmark actions out of time order')
        last_bookmark_times[key]=at
        if row['action']=='add': check(key not in active_bookmarks,'Double bookmark addition'); active_bookmarks[key]=at
        else: check(key in active_bookmarks,'Removing absent bookmark'); active_bookmarks.pop(key)
    snapshot={(r['user_id'],int(r['movie_id'])):int(r['added_at_ms']) for r in read('watchlist')}
    check(snapshot==active_bookmarks,'Watchlist snapshot does not replay from actions')
    for row in read('not_interested'):
        if row['is_active']=='1': check((row['user_id'],int(row['movie_id'])) not in snapshot,'Active bookmark and active dismissal conflict')
    for row in read('clips'):
        s=sessions.get(row['session_id'])
        check(s is not None and (row['user_id'],int(row['movie_id']),row['episode_id'])==s[:3],'Clip session mismatch')
        check(0<=float(row['start_seconds'])<float(row['end_seconds']) and float(row['end_seconds'])-float(row['start_seconds'])<=60.002,'Invalid clip boundaries')
        duration=float(episodes[row['episode_id']]['duration_seconds']) if row['episode_id'] else int(movies[int(row['movie_id'])]['duration_min'])*60
        check(float(row['end_seconds'])<=duration+.002,'Clip exceeds content duration')
    checks.append('bookmark event replay, consistent active preferences, valid clip lengths and owners')

    review_refs={r['rating_id']:(r['user_id'],int(r['movie_id']),int(r['score'])) for r in read('reviews')}
    user=None; rated=set(); prev_time=0; prev_split=0; split_order={'train':0,'validation':1,'test':2}; splits=Counter()
    for row in read('ratings'):
        uid,mid=row['user_id'],int(row['movie_id'])
        check(uid in users and mid in movies and row['is_synthetic']=='0' and row['source']=='movielens_32m','Real rating provenance/foreign key')
        if uid!=user: user=uid; rated=set(); prev_time=0; prev_split=0
        check(mid not in rated,'Duplicated real user/movie rating'); rated.add(mid)
        score=int(row['score']); at=int(row['rated_at']); split=split_order[row['split']]
        check(score==float(row['source_rating_0_5_to_5'])*2 and 1<=score<=10,'Rating scale mismatch')
        check(at>=prev_time and split>=prev_split,'Real ratings/splits are not chronological')
        check(at>=int(users[uid]['registered_at']),'User registration after source rating')
        prev_time,prev_split=at,split; splits[row['split']]+=1
        movie_stats[mid].update(real_rating_count=1,real_rating_sum=score)
        if row['rating_id'] in review_refs:
            check(review_refs.pop(row['rating_id'])==(uid,mid,score) and row['split']=='train','Synthetic review used wrong/non-training rating')
    check(not review_refs,'Review refers to missing real rating')
    for row in read('ratings_synthetic'):
        mid=int(row['movie_id']); movie_stats[mid].update(synthetic_rating_count=1,synthetic_rating_sum=int(row['score']))
    checks.append('real rating scales, uniqueness, chronological train/validation/test and review derivation from train only')

    last_event=Counter(); event_types=Counter()
    for n,row in enumerate(read('events'),1):
        check(row['event_id']==f'ev_{n}','Event ID duplication/gap')
        user=row['user_id']; mid=int(row['movie_id']) if row['movie_id'] else None; at=int(row['event_at_ms'])
        check(user in users and (mid is None or mid in movies),'Event refers to outside user/catalog')
        check(int(users[user]['registered_at'])*1000<=at<=manifest['as_of']*1000,'Event outside user lifetime/as-of')
        check(row['source']=='synthetic_v1' and row['is_synthetic']=='1','Synthetic event mislabeled')
        sid=row['session_id']
        if sid:
            s=sessions.get(sid); check(s and (user,mid)==s[:2],'Event/session owner mismatch')
            if row['event_type']=='play_start': check(at==s[3],'Wrong play start timestamp')
            elif row['event_type']=='play_end': check(at==s[4],'Wrong play end timestamp')
        if row['request_id']: check(row['request_id'] in requests and requests[row['request_id']][0]==user,'Event/request owner mismatch')
        last_event[user]=max(last_event[user],at); user_stats[user]['events']+=1; event_types[row['event_type']]+=1
    required={'onboarding_preferences','recommendation_impression','card_click','search','bookmark_add','bookmark_remove',
        'not_interested','watched_mark','play_start','play_end','pause','resume','seek','buffer_start','buffer_end',
        'rating_set','review_create','clip_create','clip_pin','room_create','room_join','room_leave'}
    check(required<=set(event_types),'Missing required synthetic behavior types')
    for row in read('user_activity'):
        user=row['user_id']; s=user_stats[user]
        check(int(row['last_event_at_ms'])==last_event[user],'User recency snapshot mismatch')
        check(int(row['synthetic_event_count'])==s['events'] and int(row['sessions'])==s['sessions'],'User aggregates mismatch')
        check(abs(float(row['watched_seconds'])-s['watched_seconds'])<.03,'User watch time aggregate mismatch')
    for row in read('movie_stats'):
        mid=int(row['movie_id']); s=movie_stats[mid]
        for key in ('real_rating_count','synthetic_rating_count','impressions','clicks','sessions','completed_sessions'):
            check(int(row[key])==s[key],f'Movie aggregate mismatch {mid}:{key}')
        check(abs(float(row['total_watched_seconds'])-s['total_watched_seconds'])<.03,'Movie watched time mismatch')
    checks.append('all required event types, dates, recency and reconciled user/movie aggregates')

    for name in ('tags','watched','ratings_synthetic','not_interested','movie_mapping'):
        for row in read(name):
            check(int(row['movie_id']) in movies,'Out-of-catalog movie reference in '+name)
            if 'user_id' in row: check(row['user_id'] in users,'Unknown user in '+name)
            if name=='tags': check(row['is_synthetic']=='0' and row['source']=='movielens_32m','Real tag mislabeled')
            if name=='ratings_synthetic': check(row['is_synthetic']=='1' and 1<=int(row['score'])<=10,'Synthetic rating mislabeled')
    for row in read('searches'):
        check(row['user_id'] in users and int(row['selected_movie_id']) in movies,'Search target/user not in dataset')
    checks.append('all remaining tag, preference, mapping and search foreign keys; real/synthetic separation')

    for name,info in manifest['files'].items():
        path=HERE/info['file']; check(sha(path)==info['sha256'],'File checksum mismatch: '+name)
        with gzip.open(path,'rt',encoding='utf-8',newline='') as f:
            reader=csv.reader(f); check(next(reader)==info['columns'],'Schema/header mismatch: '+name)
            counts[name]=sum(1 for _ in reader)
        check(counts[name]==info['rows'],'Manifest row count mismatch: '+name)
        print(f'Validated {name}: {counts[name]:,} rows',flush=True)
    if not args.offline:
        db_path=Path(manifest['database'])
        with sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            live_ids={r[0] for r in db.execute('SELECT id FROM films')}
        check(set(movies)==live_ids,'Catalog IDs changed since dataset snapshot')
        check(sha(db_path)==manifest['database_sha256_before'],'Application main database changed')
        checks.append('all file checksums/schema/row counts and unchanged read-only source application database')
    else:
        checks.append('all file checksums/schema/row counts; live application database comparison explicitly skipped')
    report=dict(passed=True,checks=checks,counts=dict(counts),event_types=dict(event_types),
                start_kinds=dict(start_kinds),stop_reasons=dict(reasons),real_splits=dict(splits),database_unchanged=None if args.offline else True)
    destination=HERE/('validation_offline.json' if args.offline else 'validation.json')
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'PASS':True,'tables':len(counts),'rows':sum(counts.values()),'database_unchanged':None if args.offline else True}),flush=True)


if __name__=='__main__': main()
