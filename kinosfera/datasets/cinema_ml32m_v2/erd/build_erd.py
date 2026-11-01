"""Generate documentation-only ERD assets from the standalone dataset schema."""
from collections import defaultdict
import heapq
import html
import json
from pathlib import Path
import xml.etree.ElementTree as ET

OUT=Path(__file__).resolve().parent
DATASET=OUT.parent
SCHEMA=json.loads((DATASET/'schema.json').read_text(encoding='utf-8'))['files']
MANIFEST=json.loads((DATASET/'manifest.json').read_text(encoding='utf-8'))
PK={
 'movies':['movie_id'],'users':['user_id'],'episodes':['episode_id'],'movie_mapping':['source_movie_id'],
 'ratings':['rating_id'],'tags':['tag_id'],'reviews':['review_id'],
 'recommendation_requests':['request_id'],'impressions':['impression_id'],'clicks':['click_id'],'searches':['search_id'],
 'watch_sessions':['session_id'],'rooms':['room_id'],'room_members':['room_id','user_id'],'clips':['clip_id'],
 'progress':['user_id','movie_id'],'episode_progress':['user_id','movie_id','episode_id'],
 'watched':['user_id','movie_id'],'watchlist':['user_id','movie_id'],'not_interested':['user_id','movie_id'],
 'bookmark_events':['bookmark_event_id'],'events':['event_id'],'movie_stats':['movie_id'],'user_activity':['user_id']}
UNIQUE={
 'movies':[['imdb_id'],['catalog_kind','tmdb_id']], 'users':[['source_user_id']],
 'episodes':[['movie_id','season','episode']], 'ratings':[['user_id','movie_id']],
 'reviews':[['rating_id'],['user_id','movie_id']],
 'impressions':[['request_id','rank'],['request_id','movie_id']], 'clicks':[['impression_id']],
 'room_members':[['session_id']]}
NULL={
 'movies':{'imdb_id','tmdb_id','collection_id','collection_name'},'episodes':{'source_file_id'},
 'watch_sessions':{'episode_id','room_id','request_id'},'rooms':{'episode_id'},'clips':{'episode_id'},
 'progress':{'episode_id'},'events':{'movie_id','session_id','request_id'},
 'ratings':{'source_movie_id','split'},'movie_stats':{'rating_mean'}}
JSON_FIELDS={'genres','countries','country_codes','spoken_languages','actors','directors',
 'preferred_genres','preferred_content_types','preferred_languages','preferred_duration_min',
 'preferred_actor_ids','preferred_director_ids','payload'}
BOOL_FIELDS={'has_video_source','is_hidden',
 'completed','pinned','clicked','is_active','is_host'}
DECIMAL_FIELDS={'tmdb_rating','imdb_rating','kinopoisk_rating','rating_mean',
 'position','duration','start_position','end_position','watched_seconds',
 'total_watched_seconds','start_seconds','end_seconds','days_since_last_event'}
INT_FIELDS={'movie_id','selected_movie_id','source_movie_id','source_user_id','tmdb_id','collection_id','source_file_id','year','season',
 'episode','duration_min','duration_seconds','score','rank','view_number','paused_seconds','buffered_seconds',
 'episodes_completed','episode_count','rating_count','event_count',
 'tmdb_vote_count','imdb_vote_count','kinopoisk_vote_count','catalog_view_count','impressions','clicks','sessions',
 'completed_sessions','active_watchlist_users','active_not_interested_users'}
FKS=[]


def fk(child,column,parent,target=None,optional=False,unique=False,total=False):
    FKS.append(dict(child=child,column=column,parent=parent,target=target or PK[parent][0],
                    optional=optional,unique=unique,total=total,identifying=column in PK[child]))


fk('episodes','movie_id','movies'); fk('movie_mapping','movie_id','movies')
fk('movie_stats','movie_id','movies',unique=True,total=True)
fk('user_activity','user_id','users',unique=True,total=True)
for table in ('ratings','tags','reviews','watched','watchlist','not_interested','bookmark_events',
              'impressions','clicks','watch_sessions','progress','episode_progress','room_members','clips','events'):
    fk(table,'user_id','users')
for table in ('ratings','tags','reviews','watched','watchlist','not_interested','bookmark_events',
              'impressions','clicks','watch_sessions','progress','episode_progress','clips','events','rooms'):
    fk(table,'movie_id','movies',optional=table=='events')
for table in ('ratings','tags'): fk(table,'source_movie_id','movie_mapping',optional=table=='ratings')
fk('reviews','rating_id','ratings',unique=True)
fk('recommendation_requests','user_id','users')
fk('impressions','request_id','recommendation_requests')
fk('clicks','request_id','recommendation_requests')
fk('clicks','impression_id','impressions',unique=True)
fk('searches','user_id','users'); fk('searches','selected_movie_id','movies')
for table in ('watch_sessions','progress','episode_progress','rooms','clips'):
    fk(table,'episode_id','episodes',optional=table!='episode_progress')
fk('watch_sessions','room_id','rooms',optional=True)
fk('watch_sessions','request_id','recommendation_requests',optional=True)
fk('rooms','host_user_id','users')
fk('room_members','room_id','rooms')
fk('room_members','session_id','watch_sessions',unique=True)
fk('clips','session_id','watch_sessions')
fk('events','session_id','watch_sessions',optional=True)
fk('events','request_id','recommendation_requests',optional=True)

GROUPS={
 'catalog':('Каталог','#0f766e',['movies','episodes','movie_mapping','movie_stats']),
 'profile':('Профили и предпочтения','#6d28d9',['users','user_activity','watched','watchlist','not_interested','bookmark_events']),
 'feedback':('Оценки и отзывы','#1d4ed8',['ratings','tags','reviews']),
 'discovery':('Выдача и события','#b45309',['recommendation_requests','impressions','clicks','searches','events']),
 'playback':('Воспроизведение','#be123c',['watch_sessions','progress','episode_progress']),
 'social':('Комнаты и клипы','#4338ca',['rooms','room_members','clips'])}
DOMAIN={table:key for key,(_,_,tables) in GROUPS.items() for table in tables}
NOTES={
 'movies':'Коллекции, персоны, жанры и языки хранятся в полях/JSON, а не в отдельных таблицах.',
 'users':'Отдельные обезличенные ID MovieLens; это не аккаунты сайта.',
 'ratings':'Единая оценка пары user/movie по шкале 1–10. Исходная оценка имеет приоритет; дополняются только отсутствующие пары.',
 'reviews':'Отзыв ссылается на единую таблицу ratings; оценка и владелец должны совпадать.',
 'events':'Общая лента. Ссылки внутри payload полиморфны и не являются обычными FK.',
 'watch_sessions':'episode_id, room_id и request_id необязательны. Одна строка — один сеанс.',
 'room_members':'Связующая таблица N:M. Один участник комнаты связан с одним модельным сеансом.',
 'watchlist':'Текущее состояние. Восстанавливается из bookmark_events, прямого FK к истории нет.',
 'not_interested':'Одна последняя запись состояния на пару user/movie.',
 'progress':'Последний снимок user/movie; episode_id — текущая серия при наличии.',
 'episode_progress':'Последний снимок user/movie/episode; накопленное время может превышать длительность.',
 'movie_stats':'Расчётная сводка 1:1; не независимые взаимодействия.',
 'user_activity':'Расчётная сводка 1:1; даты в разных единицах согласно именам столбцов.',
 'episodes':'source_file_id — внешняя ссылка на источник каталога, без отдельной таблицы в датасете.'}


def dtype(field):
    if field in JSON_FIELDS: return 'json'
    if field in BOOL_FIELDS: return 'boolean'
    if field in DECIMAL_FIELDS: return 'decimal(18,6)'
    if field in INT_FIELDS or field.endswith(('_at','_at_ms')) or field=='event_at_ms': return 'bigint'
    return 'text'


def keys(table,field):
    flags=[]
    if field in PK[table]: flags.append('PK')
    if any(f['child']==table and f['column']==field for f in FKS): flags.append('FK')
    if [field] in UNIQUE.get(table,[]) and field not in PK[table]: flags.append('UK')
    return ','.join(flags)


def model():
    assert set(PK)==set(SCHEMA)==set(DOMAIN)
    for fk_ in FKS:
        assert fk_['column'] in SCHEMA[fk_['child']] and fk_['target'] in SCHEMA[fk_['parent']]
        assert dtype(fk_['column'])==dtype(fk_['target']), f"Incompatible FK: {fk_}"
        assert fk_['optional']==(fk_['column'] in NULL.get(fk_['child'],set()))
    return dict(title='КиноСфера · тестовый датасет',kind='Logical ERD of CSV tables; constraints are documented, not executed',
        tables={t:dict(domain=DOMAIN[t],row_count=MANIFEST['files'][t]['rows'],primary_key=PK[t],unique_keys=UNIQUE.get(t,[]),
            fields=[dict(name=f,type=dtype(f),nullable=f in NULL.get(t,set()),keys=keys(t,f)) for f in fields],note=NOTES.get(t,'')) for t,fields in SCHEMA.items()},
        foreign_keys=FKS)


def dbml(data):
    lines=['// Logical model of the 24 CSV tables. No SQL database is created.',
           '// Epoch timestamps are bigint. JSON collections remain in their existing columns.',
           'Project cinema_test_dataset {',"  database_type: 'PostgreSQL'","  Note: 'Documentation model only; data files remain CSV.gz.'",'}','']
    for table,info in data['tables'].items():
        lines.append(f'Table {table} {{')
        for field in info['fields']:
            flags=[]
            if len(PK[table])==1 and field['name'] in PK[table]: flags.append('pk')
            if not field['nullable']: flags.append('not null')
            if [field['name']] in UNIQUE.get(table,[]): flags.append('unique')
            lines.append(f'  {field["name"]} {field["type"]}'+(' ['+', '.join(flags)+']' if flags else ''))
        if len(PK[table])>1 or any(len(v)>1 for v in UNIQUE.get(table,[])):
            lines.append('  indexes {')
            if len(PK[table])>1: lines.append('    ('+', '.join(PK[table])+') [pk]')
            for constraint in UNIQUE.get(table,[]):
                if len(constraint)>1: lines.append('    ('+', '.join(constraint)+') [unique]')
            lines.append('  }')
        note=info['note'].replace('\\','\\\\').replace("'","\\'")
        if note: lines.append(f"  Note: '{note}'")
        lines+=['}','']
    for edge in FKS:
        # DBML uses < for one-to-many and - for one-to-one.
        marker='-' if edge['unique'] else '<'
        lines.append(f'Ref: {edge["parent"]}.{edge["target"]} {marker} {edge["child"]}.{edge["column"]}')
    return '\n'.join(lines)+'\n'


def mermaid(data,tables=None,edges=None,compact=False):
    tables=list(tables or data['tables']); edges=FKS if edges is None else edges
    lines=['erDiagram','    direction LR']
    for table in tables:
        info=data['tables'][table]; lines.append(f'    {table} {{')
        fields=info['fields']
        if compact: fields=[f for f in fields if f['keys']]
        for field in fields:
            typ='decimal' if field['type'].startswith('decimal') else field['type']
            comment=' "nullable"' if field['nullable'] else ''
            lines.append(f'        {typ} {field["name"]}'+(' '+field['keys'] if field['keys'] else '')+comment)
        lines.append('    }')
    for edge in edges:
        if edge['parent'] not in tables or edge['child'] not in tables: continue
        left='|o' if edge['optional'] else '||'
        right='||' if edge['total'] else 'o|' if edge['unique'] else 'o{'
        middle='--' if edge['identifying'] else '..'
        lines.append(f'    {edge["parent"]} {left}{middle}{right} {edge["child"]} : "{edge["column"]}"')
    return '\n'.join(lines)+'\n'


# Positions are domain-specific; row attachment points and orthogonal routing
# are generated from the same entities for SVG and diagrams.net.
PAGES=[
 dict(id='overview',title='Обзор ядра модели',subtitle='Основные цепочки связей. Все 24 таблицы и полные FK — на следующих шести страницах.',
  full=[],compact=True,positions={'users':(40,150),'movies':(40,600),'recommendation_requests':(600,100),'impressions':(1140,100),
   'clicks':(1680,100),'ratings':(600,430),'reviews':(1140,430),'episodes':(600,760),'watch_sessions':(1140,760),
   'rooms':(1680,760),'room_members':(1680,1120),'clips':(1140,1120)},
  selected=[('users','recommendation_requests'),('recommendation_requests','impressions'),('impressions','clicks'),
   ('users','ratings'),('ratings','reviews'),('movies','episodes'),('users','watch_sessions'),('episodes','watch_sessions'),
   ('movies','rooms'),('users','rooms'),('rooms','room_members'),('watch_sessions','room_members'),('watch_sessions','clips')]),
 dict(id='catalog',title='Каталог и внешние идентификаторы',subtitle='movies — общая сущность для фильма, сериала и мини-сериала. JSON-поля сохранены без нормализации.',
  full=GROUPS['catalog'][2],positions={'movies':(580,140),'episodes':(40,170),'movie_mapping':(1120,150),'movie_stats':(1120,480)}),
 dict(id='profile',title='Профили и предпочтения',subtitle='watched/watchlist/not_interested — состояния; bookmark_events — история. user_activity — расчётная сводка.',
  full=GROUPS['profile'][2],positions={'users':(40,140),'movies':(40,700),'watchlist':(600,130),'watched':(1140,130),
   'not_interested':(600,430),'bookmark_events':(1140,460),'user_activity':(600,820)}),
 dict(id='feedback',title='Оценки, теги и отзывы',subtitle='Одна таблица ratings и одно поле score на пару пользователь–произведение. reviews.rating_id → ratings.rating_id.',
  full=GROUPS['feedback'][2],positions={'users':(40,150),'movies':(40,500),'movie_mapping':(40,850),
   'ratings':(600,130),'tags':(1140,130),'reviews':(600,660)}),
 dict(id='discovery',title='Показы, клики, поиск и события',subtitle='У показов уникальны (request_id, rank) и (request_id, movie_id). Один показ имеет не более одного клика.',
  full=GROUPS['discovery'][2],positions={'users':(40,140),'movies':(40,500),'watch_sessions':(40,850),
   'recommendation_requests':(600,130),'impressions':(1140,130),'clicks':(1680,130),
   'searches':(600,600),'events':(1140,660)}),
 dict(id='playback',title='Сеансы и прогресс просмотра',subtitle='У сеанса могут отсутствовать episode_id, room_id и request_id. Прогресс — отдельные снимки состояния.',
  full=GROUPS['playback'][2],positions={'users':(40,140),'movies':(40,460),'episodes':(40,780),
   'rooms':(580,140),'recommendation_requests':(580,450),'watch_sessions':(1140,130),
   'progress':(580,760),'episode_progress':(1140,820)}),
 dict(id='social',title='Комнаты, участники и клипы',subtitle='room_members связывает пользователей и комнаты (N:M) и указывает соответствующий сеанс просмотра.',
  full=GROUPS['social'][2],positions={'users':(40,140),'movies':(40,440),'episodes':(40,740),'watch_sessions':(40,1020),
   'rooms':(600,130),'room_members':(1140,130),'clips':(600,650)})
]

W=460; ROW=24; HEADER=48


def nodes_for(page,data):
    nodes={}
    for table,(x,y) in page['positions'].items():
        info=data['tables'][table]; full=table in page['full']; fields=info['fields']
        if not full:
            needed=set(PK[table])
            if page.get('compact'): needed|={e['column'] for e in FKS if e['child']==table}
            fields=[f for f in fields if f['name'] in needed]
        nodes[table]=dict(x=x,y=y,w=W,h=HEADER+ROW*len(fields)+10,fields=fields,full=full)
    return nodes


def page_edges(page):
    if 'selected' in page:
        wanted=set(tuple(pair) for pair in page['selected'])
        return [e for e in FKS if (e['parent'],e['child']) in wanted]
    return [e for e in FKS if e['child'] in page['full']]


def route(edge,nodes,used=()):
    p,c=nodes[edge['parent']],nodes[edge['child']]
    def py(node,field):
        index=next(i for i,f in enumerate(node['fields']) if f['name']==field)
        return node['y']+HEADER+ROW*index+ROW/2
    sy,ty=py(p,edge['target']),py(c,edge['column'])
    # Choose facing ports, or use the right gutter for vertically aligned cards.
    if p['x']<c['x']: ps,cs=1,-1
    elif p['x']>c['x']: ps,cs=-1,1
    else: ps,cs=1,1
    start=(p['x']+(p['w'] if ps==1 else 0),sy); finish=(c['x']+(c['w'] if cs==1 else 0),ty)
    a=(start[0]+ps*28,sy); b=(finish[0]+cs*28,ty)
    obstacles=[(n['x']-13,n['y']-13,n['x']+n['w']+13,n['y']+n['h']+13) for n in nodes.values()]
    xs=sorted({a[0],b[0],10.0,max(n['x']+n['w'] for n in nodes.values())+65,
               *[v for n in nodes.values() for gap in (18,28,38,48,58)
                 for v in (n['x']-gap,n['x']+n['w']+gap)]})
    ys=sorted({sy,ty,110.0,max(n['y']+n['h'] for n in nodes.values())+45,*[v for n in nodes.values() for v in (n['y']-28,n['y']+n['h']+28)]})
    def clear(u,v):
        x1,y1=u; x2,y2=v
        for left,top,right,bottom in obstacles:
            if x1==x2 and left<x1<right and max(min(y1,y2),top)<min(max(y1,y2),bottom): return False
            if y1==y2 and top<y1<bottom and max(min(x1,x2),left)<min(max(x1,x2),right): return False
        return True
    def overlap_cost(u,v):
        penalty=0
        for earlier,points in used:
            # Shared trunks are meaningful only for the same parent field.
            if (earlier['parent'],earlier['target'])==(edge['parent'],edge['target']): continue
            for s,t in zip(points,points[1:]):
                if u[0]==v[0]==s[0]==t[0]:
                    penalty+=max(0,min(max(u[1],v[1]),max(s[1],t[1]))-max(min(u[1],v[1]),min(s[1],t[1])))*4
                elif u[1]==v[1]==s[1]==t[1]:
                    penalty+=max(0,min(max(u[0],v[0]),max(s[0],t[0]))-max(min(u[0],v[0]),min(s[0],t[0])))*4
        return penalty
    initial=(xs.index(a[0]),ys.index(a[1]),'x'); target=(xs.index(b[0]),ys.index(b[1]))
    costs={initial:0}; previous={}; queue=[(0,initial)]; found=None
    while queue:
        cost,state=heapq.heappop(queue)
        if cost!=costs[state]: continue
        ix,iy,direction=state
        if (ix,iy)==target: found=state; break
        for nx,ny,axis in ((ix-1,iy,'x'),(ix+1,iy,'x'),(ix,iy-1,'y'),(ix,iy+1,'y')):
            if not 0<=nx<len(xs) or not 0<=ny<len(ys): continue
            u,v=(xs[ix],ys[iy]),(xs[nx],ys[ny])
            if not clear(u,v): continue
            new=cost+abs(u[0]-v[0])+abs(u[1]-v[1])+(22 if direction!=axis else 0)+overlap_cost(u,v)
            ns=(nx,ny,axis)
            if new<costs.get(ns,float('inf')):
                costs[ns]=new; previous[ns]=state; heapq.heappush(queue,(new,ns))
    if found is None: raise ValueError('Cannot route '+str(edge))
    points=[]; s=found
    while True:
        points.append((xs[s[0]],ys[s[1]]))
        if s==initial: break
        s=previous[s]
    points=list(reversed(points)); points=[start]+points+[finish]
    compact=[]
    for point in points:
        if len(compact)>=2 and (compact[-2][0]==compact[-1][0]==point[0] or compact[-2][1]==compact[-1][1]==point[1]): compact[-1]=point
        else: compact.append(point)
    return dict(points=compact,parent_side=ps,child_side=cs)


def routes_for(edges,nodes):
    routes=[]; used=[]
    for edge in edges:
        result=route(edge,nodes,used)
        routes.append(result); used.append((edge,result['points']))
    return routes


def svg_page(page,data,nodes,edges,routes):
    width=max(n['x']+n['w'] for n in nodes.values())+70
    height=max(n['y']+n['h'] for n in nodes.values())+100
    esc=html.escape
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(page["title"])}">',
       '<style>text{font-family:Segoe UI,Arial,sans-serif;fill:#17243b}.field{font-family:Consolas,monospace;font-size:13px}.type{fill:#65738a;font-size:12px}.key{font-size:10px;font-weight:700;fill:#1d4ed8}.edge{fill:none;stroke:#8290a5;stroke-width:1.25}.mark{fill:#fff;stroke:#64748b;stroke-width:1.4}</style>',
       f'<rect width="{width}" height="{height}" fill="#f7f9fc"/>',
       f'<text x="40" y="40" font-size="24" font-weight="700">{esc(page["title"])}</text>',
       f'<text x="40" y="69" font-size="13" fill="#596982">{esc(page["subtitle"])}</text>']
    def mark(x,y,side,kind):
        if kind=='many':
            return f'<path class="mark" d="M{x},{y-7}L{x+side*13},{y}L{x},{y+7} M{x},{y}L{x+side*13},{y}"/><circle class="mark" cx="{x+side*21}" cy="{y}" r="4"/>'
        if kind=='optional': return f'<path class="mark" d="M{x+side*7},{y-7}V{y+7}"/><circle class="mark" cx="{x+side*19}" cy="{y}" r="4"/>'
        return f'<path class="mark" d="M{x+side*7},{y-7}V{y+7} M{x+side*13},{y-7}V{y+7}"/>'
    for edge,r in zip(edges,routes):
        color=GROUPS[DOMAIN[edge['parent']]][1]
        dash='' if edge['identifying'] else ';stroke-dasharray:5 4'
        parts.append(f'<g data-parent="{edge["parent"]}" data-child="{edge["child"]}"><title>{edge["child"]}.{edge["column"]} → {edge["parent"]}.{edge["target"]}</title>')
        parts.append(f'<polyline class="edge" style="stroke:{color};stroke-opacity:.65{dash}" points="'+' '.join(f'{x},{y}' for x,y in r['points'])+'"/>')
        sx,sy=r['points'][0]; tx,ty=r['points'][-1]
        parts.append(mark(sx,sy,r['parent_side'],'optional' if edge['optional'] else 'one'))
        parts.append(mark(tx,ty,r['child_side'],'one' if edge['total'] else 'optional' if edge['unique'] else 'many'))
        parts.append('</g>')
    for table,n in nodes.items():
        color=GROUPS[DOMAIN[table]][1]; x,y,w,h=n['x'],n['y'],n['w'],n['h']
        parts+=[f'<g data-entity="{table}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="#fff" stroke="#cbd5e1"/>',
           f'<path d="M{x+7},{y}H{x+w-7}Q{x+w},{y} {x+w},{y+7}V{y+HEADER}H{x}V{y+7}Q{x},{y} {x+7},{y}" fill="{color}"/>',
           f'<text x="{x+15}" y="{y+23}" style="fill:white" font-size="17" font-weight="700">{table}</text>',
           f'<text x="{x+w-14}" y="{y+23}" text-anchor="end" style="fill:#fff" font-size="11">{data["tables"][table]["row_count"]:,}</text>',
           f'<text x="{x+15}" y="{y+40}" style="fill:#eef2ff" font-size="10">{GROUPS[DOMAIN[table]][0]}'+(' · ссылка' if not n['full'] else '')+'</text>']
        for i,field in enumerate(n['fields']):
            ry=y+HEADER+ROW*i
            if i%2==0: parts.append(f'<rect x="{x+1}" y="{ry}" width="{w-2}" height="{ROW}" fill="#f5f7fa"/>')
            label=field['name']+(' ?' if field['nullable'] else '')
            parts+=[f'<text class="key" x="{x+11}" y="{ry+16}">{esc(field["keys"].replace(",","/"))}</text>',
                    f'<text class="field" x="{x+73}" y="{ry+16}">{esc(label)}</text>',
                    f'<text class="type" x="{x+w-10}" y="{ry+16}" text-anchor="end">{field["type"].split("(")[0]}</text>']
        parts.append('</g>')
    parts.append(f'<text x="40" y="{height-30}" font-size="12">PK — первичный ключ · FK — внешний ключ · UK — уникальность · ? — NULL · ○ — 0 · || — 1 · лапка — много · пунктир — FK вне PK</text>')
    parts.append('</svg>')
    return '\n'.join(parts)


def drawio_page(root,page,data,nodes,edges,routes):
    diagram=ET.SubElement(root,'diagram',id=page['id'],name=page['title'])
    w=max(n['x']+n['w'] for n in nodes.values())+100; h=max(n['y']+n['h'] for n in nodes.values())+100
    model=ET.SubElement(diagram,'mxGraphModel',dx=str(w),dy=str(h),grid='1',gridSize='10',page='1',pageScale='1',pageWidth=str(w),pageHeight=str(h))
    canvas=ET.SubElement(model,'root'); ET.SubElement(canvas,'mxCell',id='0'); ET.SubElement(canvas,'mxCell',id='1',parent='0')
    title=ET.SubElement(canvas,'mxCell',id=page['id']+'_title',value=page['title']+'<br><font style="font-size:12px">'+page['subtitle']+'</font>',style='text;html=1;align=left;verticalAlign=top;fontSize=23;fontStyle=1;',vertex='1',parent='1')
    ET.SubElement(title,'mxGeometry',x='40',y='20',width=str(w-80),height='65',attrib={'as':'geometry'})
    for table,n in nodes.items():
        color=GROUPS[DOMAIN[table]][1]; prefix=page['id']+'_'+table
        box=ET.SubElement(canvas,'mxCell',id=prefix,value=table+f'<br><font style="font-size:11px">{data["tables"][table]["row_count"]:,} записей'+(' · ссылка' if not n['full'] else '')+'</font>',
            style=f'swimlane;html=1;startSize={HEADER};horizontal=1;collapsible=0;rounded=1;arcSize=4;fillColor={color};swimlaneFillColor=#ffffff;strokeColor=#cbd5e1;fontColor=#ffffff;fontSize=16;fontStyle=1;align=left;spacingLeft=14;',vertex='1',parent='1')
        ET.SubElement(box,'mxGeometry',x=str(n['x']),y=str(n['y']),width=str(n['w']),height=str(n['h']),attrib={'as':'geometry'})
        for i,f in enumerate(n['fields']):
            field=ET.SubElement(canvas,'mxCell',id=prefix+'_'+f['name'],value=html.escape((f['keys']+' ' if f['keys'] else '')+f['name']+(' ?' if f['nullable'] else ''))+f'<font color="#64748b"> : {f["type"]}</font>',
                style='text;html=1;align=left;verticalAlign=middle;fontSize=12;fontFamily=Consolas;spacingLeft=8;fillColor='+('#f5f7fa' if i%2==0 else '#ffffff')+';strokeColor=none;',vertex='1',parent=prefix)
            ET.SubElement(field,'mxGeometry',x='0',y=str(HEADER+ROW*i),width=str(n['w']),height=str(ROW),attrib={'as':'geometry'})
    for i,(edge,r) in enumerate(zip(edges,routes)):
        p,c=nodes[edge['parent']],nodes[edge['child']]
        def anchor(node,column,side):
            index=next(i for i,f in enumerate(node['fields']) if f['name']==column)
            return ('1' if side==1 else '0'),(HEADER+ROW*index+ROW/2)/node['h']
        px,py=anchor(p,edge['target'],r['parent_side']); cx,cy=anchor(c,edge['column'],r['child_side'])
        start='ERzeroToOne' if edge['optional'] else 'ERmandOne'
        finish='ERmandOne' if edge['total'] else 'ERzeroToOne' if edge['unique'] else 'ERzeroToMany'
        color=GROUPS[DOMAIN[edge['parent']]][1]
        dash='' if edge['identifying'] else 'dashed=1;dashPattern=5 4;'
        style=f'edgeStyle=none;rounded=0;html=1;strokeColor={color};strokeWidth=1.2;startArrow={start};endArrow={finish};startFill=0;endFill=0;exitX={px};exitY={py};entryX={cx};entryY={cy};exitPerimeter=0;entryPerimeter=0;fontSize=10;{dash}'
        cell=ET.SubElement(canvas,'mxCell',id=page['id']+'_fk_'+str(i),value='',style=style,edge='1',parent='1',source=page['id']+'_'+edge['parent'],target=page['id']+'_'+edge['child'])
        geom=ET.SubElement(cell,'mxGeometry',relative='1',attrib={'as':'geometry'})
        points=ET.SubElement(geom,'Array',attrib={'as':'points'})
        for x,y in r['points'][1:-1]: ET.SubElement(points,'mxPoint',x=str(x),y=str(y))
    return diagram


def main():
    OUT.mkdir(parents=True,exist_ok=True); data=model()
    (OUT/'erd_model.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'cinema_test.dbml').write_text(dbml(data),encoding='utf-8')
    (OUT/'cinema_test_full.mmd').write_text(mermaid(data),encoding='utf-8')
    graph=ET.Element('mxfile',host='app.diagrams.net',version='24.7.17',type='device',compressed='false')
    pages=[]; complete=set(); rendered_fks=set()
    for i,page in enumerate(PAGES):
        nodes=nodes_for(page,data); edges=page_edges(page)
        for edge in edges: assert edge['parent'] in nodes and edge['child'] in nodes
        routes=routes_for(edges,nodes)
        svg=svg_page(page,data,nodes,edges,routes)
        name=f'{i:02}_{page["id"]}.svg'; (OUT/name).write_text(svg,encoding='utf-8')
        drawio_page(graph,page,data,nodes,edges,routes)
        (OUT/f'{i:02}_{page["id"]}.mmd').write_text(mermaid(data,list(nodes),edges,compact=page.get('compact',False)),encoding='utf-8')
        pages.append(dict(id=page['id'],title=page['title'],svg=svg))
        complete.update(page['full'])
        if i:
            rendered_fks.update((e['child'],e['column'],e['parent'],e['target']) for e in edges)
    assert complete==set(SCHEMA),'Some tables have no detailed page'
    assert len(rendered_fks)==len(FKS),'Some foreign keys are not rendered'
    ET.ElementTree(graph).write(OUT/'cinema_test.drawio',encoding='utf-8',xml_declaration=True)
    build_viewer(pages)
    print(json.dumps({'tables':len(data['tables']),'fields':sum(len(t['fields']) for t in data['tables'].values()),'foreign_keys':len(FKS),'pages':len(pages),'all_tables_and_fks_rendered':True}))


def build_viewer(pages):
    buttons=''.join(f'<button type="button" data-page="{p["id"]}" aria-selected="{str(i==0).lower()}">{i+1}. {html.escape(p["title"])}</button>' for i,p in enumerate(pages))
    body=''.join(f'<section class="sheet" id="{p["id"]}" {"hidden" if i else ""}>{p["svg"]}</section>' for i,p in enumerate(pages))
    source='''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ERD · Тестовый датасет КиноСферы</title>
<style>*{box-sizing:border-box}body{margin:0;background:#eef2f7;color:#17243b;font-family:Segoe UI,Arial,sans-serif}header{background:white;padding:20px 28px;border-bottom:1px solid #dce3ed}h1{font-size:23px;margin:0 0 8px}p{margin:7px 0;line-height:1.6;font-size:13px;color:#526179}nav{display:flex;flex-wrap:wrap;gap:7px;margin-top:15px}button{font:inherit;font-size:12px;border:1px solid #d2dbe8;border-radius:5px;padding:8px 11px;background:white;color:#34445e;cursor:pointer}button[aria-selected=true]{background:#17243b;color:white;border-color:#17243b}.tools{display:flex;align-items:center;gap:10px;margin-top:12px}.tools span{font-size:12px;color:#526179}main{padding:16px;overflow:auto}.sheet{background:#f7f9fc;border:1px solid #d8e1ee;border-radius:7px;padding:0;width:max-content;min-width:100%}.sheet[hidden]{display:none}svg{display:block}.hint{font-size:12px;margin-left:auto}a{color:#1d4ed8}footer{padding:12px 28px 24px;font-size:12px;color:#526179}.tools input{max-width:130px}@media print{header,footer{display:none}main{padding:0;overflow:visible}.sheet,.sheet[hidden]{display:block;break-after:page;width:100%;border:0;border-radius:0}svg{width:100%!important;height:260mm!important;object-fit:contain}@page{size:A3 landscape;margin:10mm}}@media(max-width:700px){header{padding:16px}.tools{flex-wrap:wrap}.hint{margin-left:0}main{padding:8px}}</style></head><body><header><h1>ERD тестового датасета КиноСферы</h1><p>25 таблиц · 50 000 пользователей · 3 054 произведения. Логические ключи CSV; это документация, а не изменение рабочей базы.</p><nav role="tablist" aria-label="Разделы модели">__BUTTONS__</nav><div class="tools"><button id="fit" type="button">По ширине</button><button id="minus" type="button" aria-label="Уменьшить">−</button><span id="zoom-label"></span><button id="plus" type="button" aria-label="Увеличить">+</button><button id="print" type="button">Печать всех страниц</button><span class="hint">PK — первичный · FK — внешний · UK — уникальный · ? — необязательное поле</span></div></header><main>__BODY__</main><footer>У обзора сокращённый набор связей. Подробные страницы содержат все таблицы и FK. Составные ключи и ограничения согласованности описаны в <a href="ERD.md">ERD.md</a>. Исходник для редактирования: <a href="cinema_test.drawio">diagrams.net</a> · <a href="cinema_test.dbml">DBML</a>.</footer>
<script>let zoom=1;const sheets=[...document.querySelectorAll('.sheet')];const label=document.getElementById('zoom-label');function active(){return sheets.find(s=>!s.hidden)}function apply(){for(const s of sheets){const svg=s.querySelector('svg');svg.style.width=(Number(svg.getAttribute('width'))*zoom)+'px';svg.style.height='auto'}label.textContent=Math.round(zoom*100)+'%'}function fit(){zoom=Math.min(1,(document.querySelector('main').clientWidth-36)/Number(active().querySelector('svg').getAttribute('width')));apply()}document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>{sheets.forEach(s=>s.hidden=s.id!==b.dataset.page);document.querySelectorAll('[data-page]').forEach(v=>v.setAttribute('aria-selected',String(v===b)));fit()});document.getElementById('fit').onclick=fit;document.getElementById('plus').onclick=()=>{zoom=Math.min(2,zoom+.15);apply()};document.getElementById('minus').onclick=()=>{zoom=Math.max(.2,zoom-.15);apply()};document.getElementById('print').onclick=()=>window.print();fit();</script></body></html>'''
    (OUT/'index.html').write_text(source.replace('25 таблиц','24 таблицы').replace('__BUTTONS__',buttons).replace('__BODY__',body),encoding='utf-8')


if __name__=='__main__': main()
