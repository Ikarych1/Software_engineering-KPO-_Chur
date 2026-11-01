"""Back up an application database and install additive engagement storage."""
from contextlib import closing
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from engagement_schema import SCHEMA

ROOT=Path(__file__).resolve().parent


def table_digest(db,table):
    h=hashlib.sha256()
    for row in db.execute('SELECT * FROM "'+table.replace('"','""')+'" ORDER BY rowid'):
        for value in row:
            data=value if isinstance(value,bytes) else repr(value).encode('utf-8')
            h.update(type(value).__name__.encode()+b':'+str(len(data)).encode()+b':'+data+b'\n')
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=ROOT/'.local/cinema.sqlite3')
    parser.add_argument('--backup',type=Path)
    args=parser.parse_args(); path=args.database.resolve()
    assert path.is_file(),'Database must exist'
    backup=args.backup or ROOT/'.local/backups'/('before-engagement-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'.sqlite3')
    backup=backup.resolve(); backup.parent.mkdir(parents=True,exist_ok=True)
    if backup.exists(): raise FileExistsError(backup)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(backup)) as out:
        source.backup(out)
    with closing(sqlite3.connect(path,timeout=30)) as db:
        db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN IMMEDIATE')
        try:
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            before={t:table_digest(db,t) for t in tables}
            columns={t:list(db.execute('SELECT * FROM pragma_table_info(?)',(t,))) for t in tables}
            for statement in SCHEMA.split(';'):
                if statement.strip(): db.execute(statement)
            assert before=={t:table_digest(db,t) for t in tables},'Existing row values changed'
            assert columns=={t:list(db.execute('SELECT * FROM pragma_table_info(?)',(t,))) for t in tables},'Existing columns changed'
            assert not list(db.execute('PRAGMA foreign_key_check'))
            after=[r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            db.commit()
        except BaseException:
            db.rollback(); raise
    report=dict(database=str(path),backup=str(backup),tables_before=len(tables),tables_after=len(after),
                added_tables=sorted(set(after)-set(tables)),existing_data_unchanged=True,existing_columns_unchanged=True)
    destination=ROOT/'.local/engagement_migration.json'
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__': main()
