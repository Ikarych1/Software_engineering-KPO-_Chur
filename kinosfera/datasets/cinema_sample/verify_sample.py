"""Verify a received sample with Python's standard library; full source is optional."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from build_sample import qid, schema_objects, select, validate

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,help='Optional original full SQLite fixture for row-by-row provenance checks')
    args = parser.parse_args()
    target = HERE/'cinema_sample.sqlite3'
    with closing(sqlite3.connect(':memory:')) as expected:
        expected.row_factory=sqlite3.Row
        expected.executescript((HERE/'schema.sql').read_text(encoding='utf-8'))
        expected_schema=schema_objects(expected)
    with closing(sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        tables={row[0]:select(db,row[0]) for row in db.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        report=validate(db,expected_schema,tables)
        checked_queries=0
        statement=''
        for line in (HERE/'example_queries.sql').read_text(encoding='utf-8').splitlines(keepends=True):
            statement+=line
            if sqlite3.complete_statement(statement):
                db.execute(statement).fetchall()
                checked_queries+=1
                statement=''
        report['example_queries_passed']=checked_queries
        if args.source:
            source=args.source.resolve()
            before=(source.stat().st_size,source.stat().st_mtime_ns)
            db.execute('ATTACH DATABASE ? AS original',(source.as_uri()+'?mode=ro',))
            unchanged=0
            for name,records in tables.items():
                if name=='user_state' or not records:
                    continue
                columns=list(records[0])
                keys=[row['name'] for row in db.execute('PRAGMA table_info('+qid(name)+')') if row['pk']]
                assert keys,name
                join=' AND '.join('d.'+qid(c)+' IS s.'+qid(c) for c in keys)
                difference=' OR '.join('d.'+qid(c)+' IS NOT s.'+qid(c) for c in columns)
                sql='SELECT COUNT(*) FROM main.'+qid(name)+' d LEFT JOIN original.'+qid(name)+' s ON '+join
                sql+=' WHERE s.'+qid(keys[0])+' IS NULL OR ('+difference+')'
                assert db.execute(sql).fetchone()[0]==0,('Source values changed',name)
                unchanged+=len(records)
            users={r['id'] for r in tables['users']};films={r['id'] for r in tables['films']};rooms={r['code'] for r in tables['rooms']}
            for row in tables['user_state']:
                original=json.loads(db.execute('SELECT data FROM original.user_state WHERE user_id=?',(row['user_id'],)).fetchone()[0])
                for key in ('watched','watchlist'):
                    original[key]=[mid for mid in original[key] if mid in films]
                original['ratings']={mid:score for mid,score in original['ratings'].items() if int(mid) in films}
                original['clips']=[clip for clip in original['clips'] if clip['movieId'] in films]
                original['rooms']={code:value for code,value in original['rooms'].items() if code in rooms}
                original['friends']=[uid for uid in original['friends'] if uid in users]
                assert original==json.loads(row['data']),('Profile values changed',row['user_id'])
            for name in ('room_members','room_messages'):
                count=db.execute('SELECT COUNT(*) FROM original.'+qid(name)+' s JOIN main.rooms r ON r.code=s.code').fetchone()[0]
                assert count==len(tables[name]),('Incomplete room',name)
            report.update(source_rows_verified=unchanged,filtered_profiles_verified=len(tables['user_state']),complete_rooms_verified=len(rooms))
            db.execute('DETACH DATABASE original')
            assert before==(source.stat().st_size,source.stat().st_mtime_ns)
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    assert digest==(HERE/'cinema_sample.sqlite3.sha256').read_text(encoding='ascii').split()[0]
    report['sha256_matches']=True
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__':
    main()
