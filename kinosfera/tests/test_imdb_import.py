"""IMDb CSV parsing, official dataset matching, atomic writes and admin access."""
from dataclasses import asdict
import gzip
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from imdb_import import Cancelled, DATASETS, DatasetCache, EXAMPLE, ImportJobs, read_list, run_import

BASICS = (
    'tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\tendYear\truntimeMinutes\tgenres\n'
    'tt0816692\tmovie\tInterstellar\tInterstellar\t0\t2014\t\\N\t169\tAdventure,Drama,Sci-Fi\n'
    'tt3659388\tmovie\tThe Martian\tThe Martian\t0\t2015\t\\N\t144\tAdventure,Drama,Sci-Fi\n'
    'tt1000001\tmovie\tSame Name\tSame Name\t0\t2000\t\\N\t90\tDrama\n'
    'tt1000002\tmovie\tSame Name\tSame Name\t0\t2010\t\\N\t100\tComedy\n'
    'tt1000003\ttvEpisode\tPilot\tPilot\t0\t2010\t\\N\t40\tDrama\n'
    'tt1000004\tmovie\tNo Runtime\tNo Runtime\t0\t2026\t\\N\t\\N\tDrama\n'
    'tt1000005\ttvSeries\tA Series\tA Series\t0\t2020\t2025\t45\tDrama\n'
)
RATINGS = 'tconst\taverageRating\tnumVotes\ntt0816692\t8.7\t2000000\ntt3659388\t8.0\t500000\n'


def fixtures(root):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    texts = dict(basics=BASICS, ratings=RATINGS,
        crew='tconst\tdirectors\twriters\ntt3659388\tnm1000001\t\\N\n',
        principals='tconst\tordering\tnconst\tcategory\tjob\tcharacters\ntt3659388\t1\tnm1000002\tactor\t\\N\t[]\n',
        names='nconst\tprimaryName\nbm0000000\tIgnored\nnm1000001\tTest Director\nnm1000002\tTest Actor\n',
        akas='titleId\tordering\ttitle\tregion\tlanguage\ntt3659388\t1\tМарсианин\tRU\tru\n')
    for key,text in texts.items():
        with gzip.open(root/DATASETS[key][0], 'wt', encoding='utf-8') as file:
            file.write(text)
    return root


class ListTests(unittest.TestCase):
    def test_ids_urls_bom_and_semicolons(self):
        entries = read_list(('\ufeffimdb_id;title;year\nhttps://www.imdb.com/title/tt0816692/?ref_=x;Интерстеллар;2014\n').encode())
        self.assertEqual((entries[0].imdb_id,entries[0].title,entries[0].year), ('tt0816692','Интерстеллар',2014))
        self.assertEqual(read_list(b'title,year\nThe Martian,2015')[0].title, 'The Martian')

    def test_invalid_lists_fail_before_network_or_mutation(self):
        values = [b'',b'\xef\xbb\xbf',b'foo\nbar',b'imdb_id\nhttps://evil.test/title/tt0816692',
                  b'imdb_id\nnot-an-id',b'title,year\nMovie,NaN',b'title,year\nMovie,1000',
                  b'title,year\nA,2014,extra', b'title\n'+b'A'*600000, b'\xff\xff']
        for value in values:
            with self.subTest(value=value[:30]), self.assertRaises(ValueError):
                read_list(value)
        with self.assertRaises(ValueError): read_list(b'title\n'+b'Movie\n'*501)

    def test_quoted_commas_and_imdb_export_headers(self):
        entry = read_list(b'Const,Title,Year\ntt1000001,"Name, subtitle",2000')[0]
        self.assertEqual(entry.title,'Name, subtitle')


class ImportTests(unittest.TestCase):
    def setUp(self):
        from database import Store
        import storage
        from ui_data import ART
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'catalog.sqlite3')
        self.store.seed(storage.CATALOG, ART)
        self.cache = DatasetCache(fixtures(Path(self.temp.name)/'cache'))

    def tearDown(self):
        self.temp.cleanup()

    def run_csv(self, text, **options):
        return run_import(self.store,read_list(text.encode()),self.cache,**options)

    def test_import_links_existing_film_and_adds_new_without_duplicates(self):
        previous = self.store.film(1,include_hidden=True)
        self.store.set_source(1,b'original-source','source.torrent',[],7,'original-hash')
        result = self.run_csv(EXAMPLE.replace('tt2582802,Одержимость,2014\n',''))
        self.assertEqual([r['status'] for r in result], ['updated','added'])
        self.assertEqual(len(self.store.films(True)),13)
        first = self.store.film(1,True)
        self.assertEqual(first['film'].description,previous['film'].description)
        self.assertEqual(first['art']['slug'],previous['art']['slug'])
        self.assertEqual((first['torrent'],first['file_id'],first['info_hash']), (b'original-source',7,'original-hash'))
        self.assertEqual(first['art']['imdb'],'8.7')
        created = self.store.film(13)
        self.assertEqual(created['film'].title,'Марсианин')
        self.assertEqual(created['film'].genres,['Приключения','Драма','Фантастика'])
        self.assertEqual(created['imdb_id'],'tt3659388')
        self.assertEqual(self.store.films(True)[-1]['imdb_id'],'tt3659388')
        self.assertEqual(created['art']['imdb_votes'],500000)
        self.assertEqual(created['film'].age_rating,'—')
        self.assertFalse(created['torrent'])
        self.run_csv('imdb_id\ntt3659388\ntt3659388\n',publish=False)
        self.assertEqual(len(self.store.films(True)),13)
        self.assertEqual(self.store.film(13)['film'].title,'Марсианин')

    def test_names_year_ambiguity_missing_and_episode_filtering(self):
        result = self.run_csv('title,year\nSame Name,\nSame Name,2010\nMissing,2015\nPilot,2010\nNo Runtime,2026\n')
        self.assertEqual([r['status'] for r in result],['ambiguous','added','not_found','not_found','incomplete'])
        self.assertEqual(len(result[0]['candidates']),2)
        self.assertEqual(len(self.store.films(True)),13)

    def test_russian_names_use_official_akas_dataset(self):
        result = self.run_csv('title,year\nМарсианин,2015\n')
        self.assertEqual(result[0]['status'],'added')
        self.assertEqual(result[0]['imdb_id'],'tt3659388')
        self.assertEqual(self.store.film(result[0]['movie_id'])['film'].title,'Марсианин')

    def test_id_year_mismatch_is_not_silently_accepted(self):
        result = self.run_csv('imdb_id,year\ntt3659388,2010\n')
        self.assertEqual(result[0]['status'],'mismatch')
        self.assertEqual(len(self.store.films(True)),12)

    def test_optional_credits_and_draft_are_saved(self):
        result = self.run_csv('imdb_id\ntt3659388\n',credits=True,publish=False)
        row = self.store.film(result[0]['movie_id'],True)
        self.assertTrue(row['film'].is_hidden)
        self.assertEqual(row['art']['director'],'Test Director')
        self.assertEqual(row['art']['actors'],['Test Actor'])
        self.assertIsNone(self.store.film(row['film'].id))

    def test_series_metadata_is_imported_without_fabricated_episodes(self):
        result = self.run_csv('imdb_id\ntt1000005\n')
        row = self.store.film(result[0]['movie_id'])
        self.assertEqual(row['film'].kind,'series')
        self.assertEqual(row['episodes'],[])

    def test_failure_rolls_back_all_catalog_writes(self):
        original = self.store.import_imdb
        count = 0
        def fail(db,*args,**kwargs):
            nonlocal count
            count += 1
            if count == 2: raise ValueError('test failure')
            return original(db,*args,**kwargs)
        with patch.object(self.store,'import_imdb',side_effect=fail), self.assertRaises(ValueError):
            self.run_csv('imdb_id\ntt3659388\ntt1000001\n')
        self.assertEqual(len(self.store.films(True)),12)

    def test_cancel_does_not_write_catalog(self):
        cache = DatasetCache(self.cache.root,cancelled=lambda:True)
        with self.assertRaises(Cancelled): run_import(self.store,read_list(b'imdb_id\ntt3659388'),cache)
        self.assertEqual(len(self.store.films(True)),12)

    def test_cached_files_do_not_call_network(self):
        with patch('imdb_import.urlopen',side_effect=AssertionError('network not expected')):
            self.run_csv('imdb_id\ntt3659388\n')

    def test_downloader_rejects_truncated_or_wrong_dataset_and_removes_partial(self):
        class FakeResponse(io.BytesIO):
            headers = {'Content-Length':'1000'}
            def geturl(self): return 'https://datasets.imdbws.com/title.basics.tsv.gz'
        folder = Path(self.temp.name)/'empty'
        cache = DatasetCache(folder)
        with patch('imdb_import.urlopen',return_value=FakeResponse(b'bad')), self.assertRaises(ValueError): cache.ensure('basics')
        self.assertEqual(list(folder.iterdir()),[])


import support
from support import AppTestCase


class AdminImportTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.previous_importer = self.app.extensions['imdb_importer']
        self.jobs = ImportJobs(fixtures(Path(self.temp.name)/'imdb'))
        self.app.extensions['imdb_importer'] = self.jobs

    def tearDown(self):
        self.app.extensions['imdb_importer'] = self.previous_importer
        super().tearDown()

    def test_only_admin_can_upload_read_reports_or_cancel(self):
        self.accounts()
        self.assertEqual(self.viewer.get('/admin/imdb').status_code,403)
        self.assertEqual(self.viewer.get('/admin/imdb/example.csv').status_code,403)
        self.assertEqual(self.post(self.viewer,'/admin/imdb/import').status_code,403)
        self.assertEqual(self.admin.post('/admin/imdb/import',data={}).status_code,400)
        self.assertEqual(self.admin.get('/admin/imdb').status_code,200)

    def test_admin_can_import_and_download_report(self):
        self.accounts()
        response = self.post(self.admin,'/admin/imdb/import',data={'list':(io.BytesIO(b'imdb_id,title,year\ntt3659388,The Martian,2015'),'movies.csv'),'publish':'on'})
        self.assertEqual(response.status_code,202)
        token = response.json['job']
        deadline = time.monotonic()+3
        while self.jobs.status(token)['state'] == 'running' and time.monotonic()<deadline: time.sleep(.01)
        job = self.admin.get(f'/admin/imdb/jobs/{token}').json
        self.assertEqual(job['state'],'done',job)
        self.assertEqual(job['counts']['added'],1)
        self.assertEqual(self.viewer.get('/movie/13').status_code,200)
        report = self.admin.get(f'/admin/imdb/jobs/{token}/report.csv')
        self.assertEqual(report.status_code,200)
        self.assertIn('tt3659388',report.text)
        self.assertEqual(self.viewer.get(f'/admin/imdb/jobs/{token}').status_code,403)
        self.assertEqual(self.post(self.viewer,f'/admin/imdb/jobs/{token}/cancel').status_code,403)

    def test_invalid_upload_and_unknown_job_are_clear_errors(self):
        self.accounts()
        response = self.post(self.admin,'/admin/imdb/import',data={'list':(io.BytesIO(b'bad'),'movies.csv')})
        self.assertEqual(response.status_code,400)
        response = self.admin.get('/admin/imdb/jobs/not-a-job')
        self.assertEqual(response.status_code,404)
        self.assertIn('error',response.json)
