"""Materialize the standalone CSV dataset as a local, queryable SQLite file.

Only reads v2 dataset files. Never opens or modifies the site's database.
"""
from collections import defaultdict
from contextlib import closing
import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import time

HERE=Path(__file__).resolve().parent
DATASET=HERE.parent
BATCH=20_000


def q(name):
    return '"'+name.replace('"','""')+'"'


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while block:=f.read(1024*1024): h.update(block)
    return h.hexdigest()


def sql_type(field):
    typ=field['type']
    if typ in ('bigint','boolean'): return 'INTEGER'
    if typ.startswith('decimal'): return 'REAL'
    if typ in ('text','json'): return 'TEXT'
    raise ValueError('Unknown logical type: '+typ)


def ddl(model):
    children=defaultdict(list)
    for fk in model['foreign_keys']: children[fk['child']].append(fk)
    statements=[]
    for name,table in model['tables'].items():
        parts=[]
        for field in table['fields']:
            col=field['name']
            declaration=q(col)+' '+sql_type(field)
            if not field['nullable']: declaration+=' NOT NULL'
            if field['type']=='json': declaration+=f' CHECK(json_valid({q(col)}))'
            if field['type']=='boolean': declaration+=f' CHECK({q(col)} IN (0,1))'
            if col=='score': declaration+=f' CHECK({q(col)} BETWEEN 1 AND 10)'
            parts.append(declaration)
        parts.append('PRIMARY KEY ('+', '.join(map(q,table['primary_key']))+')')
        for fk in children[name]:
            parts.append(f'FOREIGN KEY ({q(fk["column"])}) REFERENCES {q(fk["parent"])} ({q(fk["target"])})')
        integer_pk=len(table['primary_key'])==1 and next(f['type'] for f in table['fields'] if f['name']==table['primary_key'][0])=='bigint'
        options='STRICT' if integer_pk else 'WITHOUT ROWID, STRICT'
        statements.append('CREATE TABLE '+q(name)+' (\n  '+',\n  '.join(parts)+'\n) '+options+';')
    return statements


def index_ddl(model):
    statements=[]; covered=defaultdict(list)
    for table,info in model['tables'].items():
        covered[table].append(info['primary_key'])
        for fields in info['unique_keys']:
            name='ux_'+table+'_'+'_'.join(fields)
            statements.append(f'CREATE UNIQUE INDEX {q(name)} ON {q(table)} ('+', '.join(map(q,fields))+');')
            covered[table].append(fields)
    # Composite indexes serve both common exploration queries and their leading FKs.
    exploration={
        'ratings':[['movie_id','score']],
        'events':[['user_id','event_at_ms'],['movie_id','event_at_ms'],['event_type','event_at_ms']],
        'watch_sessions':[['user_id','movie_id','started_at_ms']],
        'bookmark_events':[['user_id','movie_id','event_at_ms']],
        'impressions':[['user_id','shown_at_ms']],
        'clicks':[['user_id','clicked_at_ms']],
        'searches':[['user_id','searched_at_ms']]}
    for table,groups in exploration.items():
        for fields in groups:
            name='idx_'+table+'_'+'_'.join(fields)
            statements.append(f'CREATE INDEX {q(name)} ON {q(table)} ('+', '.join(map(q,fields))+');')
            covered[table].append(fields)
    for fk in model['foreign_keys']:
        table,col=fk['child'],fk['column']
        if any(fields[0]==col for fields in covered[table]): continue
        where=f' WHERE {q(col)} IS NOT NULL' if fk['optional'] else ''
        name='idx_'+table+'_'+col
        statements.append(f'CREATE INDEX {q(name)} ON {q(table)} ({q(col)}){where};')
        covered[table].append([col])
    return statements


def convert(raw,fields):
    values=[]
    for text,field in zip(raw,fields):
        if text=='':
            if not field['nullable']: raise ValueError('Unexpected empty value: '+field['name'])
            value=None
        elif field['type'] in ('bigint','boolean'): value=int(text)
        elif field['type'].startswith('decimal'):
            value=float(text)
            if not math.isfinite(value): raise ValueError('Nonfinite number')
        else: value=text
        values.append(value)
    return tuple(values)


def main():
    started=time.monotonic()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=HERE/'cinema_test.sqlite3')
    args=parser.parse_args()
    target=args.output.resolve()
    if target.exists(): raise FileExistsError('Choose a new output file; existing databases are never overwritten: '+str(target))
    target.parent.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((DATASET/'manifest.json').read_text(encoding='utf-8'))
    model=json.loads((DATASET/'erd/erd_model.json').read_text(encoding='utf-8'))
    assert len(model['tables'])==24 and len(model['foreign_keys'])==56
    assert set(model['tables'])==set(manifest['files'])
    for table,info in model['tables'].items():
        assert [f['name'] for f in info['fields']]==manifest['files'][table]['columns']
        source=DATASET/manifest['files'][table]['file']
        assert sha(source)==manifest['files'][table]['sha256'],'Source checksum changed: '+table
    create=ddl(model); indexes=index_ddl(model)
    # Validate all DDL before writing a large file.
    with closing(sqlite3.connect(':memory:')) as preflight:
        for statement in create+indexes: preflight.execute(statement)
    (HERE/'schema.sql').write_text('-- SQLite schema of the standalone v2 fixture.\nPRAGMA foreign_keys = ON;\n\n'+'\n\n'.join(create+indexes)+'\n',encoding='utf-8')
    with tempfile.NamedTemporaryFile(prefix=target.stem+'.building-',suffix='.sqlite3',dir=target.parent,delete=False) as f:
        staging=Path(f.name)
    print('Building staging database: '+staging.name,flush=True)
    counts={}
    db=sqlite3.connect(staging)
    try:
        # Reproducible staging only: no live database is opened. Final mode is DELETE/FULL.
        db.execute('PRAGMA journal_mode=OFF')
        db.execute('PRAGMA synchronous=OFF')
        db.execute('PRAGMA cache_size=-131072')
        db.execute('PRAGMA temp_store=FILE')
        db.execute('PRAGMA foreign_keys=OFF')
        for statement in create: db.execute(statement)
        for table,info in model['tables'].items():
            fields=info['fields']; cols=[f['name'] for f in fields]
            insert=f'INSERT INTO {q(table)} ('+', '.join(map(q,cols))+') VALUES ('+', '.join('?' for _ in cols)+')'
            count=0; batch=[]
            with gzip.open(DATASET/manifest['files'][table]['file'],'rt',encoding='utf-8',newline='') as f:
                reader=csv.reader(f)
                assert next(reader)==cols
                db.execute('BEGIN')
                for row in reader:
                    assert len(row)==len(fields)
                    batch.append(convert(row,fields)); count+=1
                    if len(batch)==BATCH:
                        db.executemany(insert,batch); batch.clear()
                        if count%1_000_000==0: print(f'Imported {table}: {count:,}',flush=True)
                if batch: db.executemany(insert,batch)
                db.commit()
            assert count==manifest['files'][table]['rows']
            counts[table]=count
            print(f'Complete {table}: {count:,}',flush=True)
        for i,statement in enumerate(indexes,1):
            db.execute(statement)
            if i%10==0 or i==len(indexes): print(f'Built indexes: {i}/{len(indexes)}',flush=True)
        db.commit()
        db.execute('PRAGMA foreign_keys=ON')
        print('Checking all foreign keys and database integrity...',flush=True)
        violations=list(db.execute('PRAGMA foreign_key_check'))
        assert not violations,violations[:10]
        integrity=[r[0] for r in db.execute('PRAGMA integrity_check')]
        assert integrity==['ok'],integrity[:10]
        actual={table:db.execute('SELECT COUNT(*) FROM '+q(table)).fetchone()[0] for table in counts}
        assert actual==counts
        fks=sum(len(list(db.execute('PRAGMA foreign_key_list('+q(t)+')'))) for t in counts)
        assert fks==56
        db.execute('ANALYZE')
        db.execute('PRAGMA user_version=2')
        db.commit()
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('PRAGMA synchronous=FULL')
    finally:
        db.close()
    # Reopen independently, execute every bundled SELECT/PRAGMA, then publish the file.
    query_report=[]
    with closing(sqlite3.connect(staging.as_uri()+'?mode=ro',uri=True)) as check_db:
        check_db.execute('PRAGMA query_only=ON')
        check_db.execute('PRAGMA foreign_keys=ON')
        query_file=HERE/'example_queries.sql'
        if query_file.exists():
            for chunk in query_file.read_text(encoding='utf-8').split(';'):
                statement=chunk.strip()
                if not statement: continue
                begin=time.monotonic()
                result=check_db.execute(statement).fetchall()
                query_report.append(dict(rows=len(result),seconds=round(time.monotonic()-begin,4)))
        assert check_db.execute('PRAGMA user_version').fetchone()[0]==2
    if target.exists(): raise FileExistsError('Output was created concurrently; staging file preserved')
    staging.replace(target)
    checksum=sha(target)
    target.with_suffix(target.suffix+'.sha256').write_text(checksum+'  '+target.name+'\n',encoding='utf-8')
    report=dict(passed=True,file=target.name,bytes=target.stat().st_size,sha256=checksum,
                tables=len(counts),fields=sum(len(v['fields']) for v in model['tables'].values()),
                foreign_keys=fks,explicit_indexes=len(indexes),rows=sum(counts.values()),counts=counts,
                integrity_check=integrity,foreign_key_violations=0,example_queries=query_report,
                source_manifest_sha256=sha(DATASET/'manifest.json'),sqlite_version=sqlite3.sqlite_version,
                seconds=round(time.monotonic()-started,2),application_database_accessed=False)
    (HERE/'sqlite_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('counts','example_queries')},ensure_ascii=False),flush=True)


if __name__=='__main__': main()
