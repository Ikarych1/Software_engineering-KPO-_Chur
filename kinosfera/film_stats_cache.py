"""Cache generations maintained by SQLite, including writes from other connections."""

SCHEMA = '''
CREATE TABLE IF NOT EXISTS cache_versions(name TEXT PRIMARY KEY,version INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO cache_versions(name,version) VALUES('film_stats',0);
'''
for table in ('ratings', 'views', 'reviews'):
    for action in ('INSERT', 'DELETE', 'UPDATE'):
        condition = ''
        if action == 'UPDATE':
            condition = ' WHEN OLD.movie_id IS NOT NEW.movie_id'
            if table == 'ratings':
                condition += ' OR OLD.score IS NOT NEW.score'
        SCHEMA += f'''
CREATE TRIGGER IF NOT EXISTS {table}_stats_{action.lower()} AFTER {action} ON {table}{condition}
BEGIN UPDATE cache_versions SET version=version+1 WHERE name='film_stats'; END;
'''
