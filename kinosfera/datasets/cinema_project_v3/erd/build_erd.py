"""Render the actual schema shared by the project and its compatible fixture."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET

import renderer as r

HERE=Path(__file__).resolve().parent
DATA=HERE.parent
GROUPS={
 'catalog':('Каталог и источники','#0f766e',['films','episodes','film_sources','media_info']),
 'accounts':('Аккаунты и профиль','#6d28d9',['users','user_state','user_preferences','login_attempts','password_resets','payments']),
 'feedback':('Оценки и предпочтения','#1d4ed8',['ratings','reviews','tags','not_interested']),
 'discovery':('Выдача и события','#b45309',['recommendation_requests','impressions','clicks','searches','bookmark_events','events']),
 'playback':('Просмотры и прогресс','#be123c',['views','progress','episode_progress','watch_sessions']),
 'social':('Комнаты и чат','#4338ca',['rooms','room_members','room_messages']),
 'imports':('Импорт и кеш','#475569',['tmdb_jobs','tmdb_rows','tmdb_cache'])}
DOMAIN={t:g for g,(_,_,ts) in GROUPS.items() for t in ts}
LOGICAL=[('ratings','movie_id','films','id'),('reviews','movie_id','films','id'),('progress','movie_id','films','id'),
 ('episode_progress','movie_id','films','id'),('film_sources','movie_id','films','id'),('views','movie_id','films','id'),
 ('views','user_id','users','id'),('views','room_code','rooms','code'),('rooms','movie_id','films','id'),('room_messages','user_id','users','id')]
PAGES=[
 dict(id='overview',title='Общая схема проекта и тестовой БД',subtitle='Одна схема: 30 таблиц. Обзор сокращён; все поля и связи — на подробных страницах.',full=[],compact=True,
      positions={'users':(40,150),'films':(40,650),'recommendation_requests':(600,120),'impressions':(1140,120),'clicks':(1680,120),
                 'ratings':(600,450),'reviews':(1140,450),'episodes':(600,800),'watch_sessions':(1140,800),
                 'rooms':(1680,800),'room_members':(1680,1180),'user_state':(600,1180)},
      selected=[('users','recommendation_requests'),('recommendation_requests','impressions'),('impressions','clicks'),('users','ratings'),
                ('users','reviews'),('films','episodes'),('episodes','watch_sessions'),('rooms','watch_sessions'),('rooms','room_members'),('users','user_state')]),
 dict(id='catalog',title='Каталог: действующий формат films.data / films.art',subtitle='films.episodes — реальные файлы плеера; episodes — метаданные серий, file_id может отсутствовать.',
      full=GROUPS['catalog'][2],positions={'films':(580,140),'episodes':(40,170),'film_sources':(1120,170),'media_info':(1120,600)}),
 dict(id='accounts',title='Аккаунты и профиль',subtitle='Коллекции, клипы и зеркало оценок — JSON user_state.data. Предпочтения — user_preferences.',
      full=GROUPS['accounts'][2],positions={'users':(40,140),'user_state':(600,140),'user_preferences':(600,420),'password_resets':(1140,140),'payments':(1140,480),'login_attempts':(40,620)}),
 dict(id='feedback',title='Оценки, отзывы, теги и «не интересно»',subtitle='ratings: исходный составной PK (user_id, movie_id). Серые связи — логические ссылки без SQL FK.',
      full=GROUPS['feedback'][2],positions={'users':(40,140),'films':(40,500),'ratings':(600,140),'reviews':(1140,140),'tags':(600,500),'not_interested':(1140,580)}),
 dict(id='discovery',title='Показы, клики, поиски и журнал',subtitle='Даты используют Unix seconds, как в проекте. events.payload хранит дополнительные поля действия в JSON.',
      full=GROUPS['discovery'][2],positions={'users':(40,140),'films':(40,460),'watch_sessions':(40,780),'recommendation_requests':(600,140),
      'impressions':(1140,140),'clicks':(1680,140),'searches':(600,600),'bookmark_events':(1680,600),'events':(1140,770)}),
 dict(id='playback',title='Штатные просмотры и история сеансов',subtitle='Один watch_sessions связан с одной views. Все серии доступны через производное представление episode_activity.',
      full=GROUPS['playback'][2],positions={'users':(40,140),'films':(40,460),'episodes':(40,760),'rooms':(580,140),'recommendation_requests':(580,400),
      'views':(580,680),'progress':(580,1010),'watch_sessions':(1140,140),'episode_progress':(1140,900)}),
 dict(id='social',title='Штатные комнаты, участники и сообщения',subtitle='Ключ rooms.code сохранён. Настройки и состояние комнаты — существующие JSON-поля.',
      full=GROUPS['social'][2],positions={'users':(40,140),'films':(40,440),'rooms':(600,140),'room_members':(1140,140),'room_messages':(600,650)}),
 dict(id='imports',title='Служебные таблицы импорта TMDB',subtitle='Схема совпадает с проектом; в отдельной тестовой базе задачи, строки импорта и кеш пусты.',
      full=GROUPS['imports'][2],positions={'tmdb_jobs':(40,150),'tmdb_rows':(600,150),'tmdb_cache':(1140,150)})]


def main():
    data={'title':'Общая схема проекта и совместимой тестовой базы','kind':'Physical SQLite schema plus explicitly marked unenforced logical links','tables':{},'foreign_keys':[]}
    with closing(sqlite3.connect((DATA/'cinema_test.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        names=[t[0] for t in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        assert set(names)==set(DOMAIN)
        for table in names:
            columns=list(db.execute('SELECT * FROM pragma_table_info(?)',(table,)))
            pk=[c[1] for c in sorted(columns,key=lambda c:c[5]) if c[5]]
            unique=[]
            for index in db.execute('SELECT * FROM pragma_index_list(?)',(table,)):
                if index[2] and index[3]!='pk':
                    unique.append([c[2] for c in db.execute('SELECT * FROM pragma_index_info(?)',(index[1],))])
            fields=[dict(name=c[1],type=c[2].lower(),nullable=not(c[3] or c[5]),keys='',default=c[4]) for c in columns]
            data['tables'][table]=dict(domain=DOMAIN[table],row_count=db.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0],primary_key=pk,unique_keys=unique,fields=fields)
            for fk in db.execute('SELECT * FROM pragma_foreign_key_list(?)',(table,)):
                child_col=next(f for f in fields if f['name']==fk[3])
                data['foreign_keys'].append(dict(child=table,column=fk[3],parent=fk[2],target=fk[4],optional=child_col['nullable'],
                    unique=[fk[3]]==pk or [fk[3]] in unique,total=False,identifying=fk[3] in pk,enforced=True))
        data['views']={name:sql for name,sql in db.execute("SELECT name,sql FROM sqlite_schema WHERE type='view'")}
    for child,column,parent,target in LOGICAL:
        info=data['tables'][child]; f=next(f for f in info['fields'] if f['name']==column)
        data['foreign_keys'].append(dict(child=child,column=column,parent=parent,target=target,optional=f['nullable'],
             unique=False,total=False,identifying=column in info['primary_key'],enforced=False))
    for table,info in data['tables'].items():
        for f in info['fields']:
            flags=[]
            if f['name'] in info['primary_key']: flags.append('PK')
            if any(e['child']==table and e['column']==f['name'] and e['enforced'] for e in data['foreign_keys']): flags.append('FK')
            if any(e['child']==table and e['column']==f['name'] and not e['enforced'] for e in data['foreign_keys']): flags.append('LK')
            if [f['name']] in info['unique_keys'] and f['name'] not in info['primary_key']: flags.append('UK')
            f['keys']=','.join(flags)
    r.GROUPS=GROUPS;r.DOMAIN=DOMAIN;r.FKS=data['foreign_keys'];r.PK={t:v['primary_key'] for t,v in data['tables'].items()}
    graph=ET.Element('mxfile',host='app.diagrams.net',type='device',compressed='false')
    pages=[]; covered=set();links=set()
    for n,page in enumerate(PAGES):
        nodes=r.nodes_for(page,data)
        edges=r.page_edges(page)
        assert all(e['parent'] in nodes and e['child'] in nodes for e in edges),page['id']
        routes=r.routes_for(edges,nodes)
        for a,box in nodes.items():
            for b,other in nodes.items():
                if a>=b:continue
                assert not(box['x']<other['x']+other['w'] and other['x']<box['x']+box['w'] and box['y']<other['y']+other['h'] and other['y']<box['y']+box['h']),(page['id'],a,b)
        svg=r.svg_page(page,data,nodes,edges,routes)
        root=ET.fromstring(svg);ns={'s':'http://www.w3.org/2000/svg'}
        for edge in edges:
            if edge['enforced']: continue
            title=f"{edge['child']}.{edge['column']} → {edge['parent']}.{edge['target']}"
            for group in root.findall('s:g',ns):
                name=group.find('s:title',ns)
                if name is not None and name.text==title:
                    group.find('s:polyline',ns).set('style','stroke:#64748b;stroke-opacity:.8;stroke-dasharray:2 5')
        svg=ET.tostring(root,encoding='unicode').replace('ns0:','').replace(':ns0','')
        # Native SVG syntax stays standalone without a generated namespace prefix.
        svg=svg.replace('xmlns="http://www.w3.org/2000/svg"','xmlns="http://www.w3.org/2000/svg"')
        svg=svg.replace('PK — первичный ключ','LK — логическая ссылка без SQL FK · PK — первичный ключ')
        (HERE/f'{n:02}_{page["id"]}.svg').write_text(svg,encoding='utf-8')
        diagram=r.drawio_page(graph,page,data,nodes,edges,routes)
        for i,edge in enumerate(edges):
            if not edge['enforced']:
                cell=diagram.find(f'.//mxCell[@id="{page["id"]}_fk_{i}"]')
                style=cell.get('style');style+='strokeColor=#64748b;dashed=1;dashPattern=2 5;'
                cell.set('style',style)
        pages.append(dict(id=page['id'],title=page['title'],svg=svg));covered.update(page['full'])
        if n:links.update((e['child'],e['column'],e['parent'],e['target']) for e in edges)
    assert covered==set(names) and len(links)==len(data['foreign_keys'])
    ET.ElementTree(graph).write(HERE/'cinema_test.drawio',encoding='utf-8',xml_declaration=True)
    r.build_viewer(pages)
    html=(HERE/'index.html').read_text(encoding='utf-8').replace('24 таблицы','30 таблиц').replace('Логические ключи CSV; это документация, а не изменение рабочей базы.','Фактическая схема проекта и совместимой тестовой БД. LK — логические связи без SQL FK.')
    (HERE/'index.html').write_text(html,encoding='utf-8')
    (HERE/'erd_model.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    # DBML/Mermaid declare only actual SQL foreign keys, never the grey logical links.
    lines=["Project cinema_project { database_type: 'SQLite' }",'']
    mmd=['erDiagram','    direction LR']
    for table,info in data['tables'].items():
        lines.append('Table '+table+' {');mmd.append('    '+table+' {')
        for f in info['fields']:
            flags=[]
            if len(info['primary_key'])==1 and f['name'] in info['primary_key']:flags.append('pk')
            if not f['nullable']:flags.append('not null')
            if [f['name']] in info['unique_keys']: flags.append('unique')
            lines.append(f'  {f["name"]} {f["type"]}'+(' ['+', '.join(flags)+']' if flags else ''))
            flags=[v for v in f['keys'].split(',') if v and v!='LK']
            mmd.append(f'        {f["type"]} {f["name"]}'+(' '+', '.join(flags) if flags else ''))
        composites=[(info['primary_key'],'pk')] if len(info['primary_key'])>1 else []
        composites +=[(cols,'unique') for cols in info['unique_keys'] if len(cols)>1]
        if composites:
            lines.append('  indexes {')
            for cols,kind in composites: lines.append('    ('+', '.join(cols)+') ['+kind+']')
            lines.append('  }')
        lines+=['}',''];mmd.append('    }')
    for e in data['foreign_keys']:
        if not e['enforced']:continue
        lines.append(f'Ref: {e["parent"]}.{e["target"]} '+('-' if e['unique'] else '<')+f' {e["child"]}.{e["column"]}')
        mmd.append(f'    {e["parent"]} '+('|o' if e['optional'] else '||')+('--' if e['identifying'] else '..')+('o|' if e['unique'] else 'o{')+f' {e["child"]} : "{e["column"]}"')
    (HERE/'cinema_test.dbml').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (HERE/'cinema_test_full.mmd').write_text('\n'.join(mmd)+'\n',encoding='utf-8')
    print(json.dumps({'tables':len(names),'fields':sum(len(t['fields']) for t in data['tables'].values()),
          'declared_foreign_keys':sum(e['enforced'] for e in data['foreign_keys']),'logical_links':len(LOGICAL),'pages':len(pages)}))


if __name__=='__main__':main()
