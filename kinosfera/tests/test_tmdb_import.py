"""TMDB matching, safe updates, durable jobs, API failures and admin access."""
import copy
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from database import Store
from tmdb_import import APIError, CatalogWriter, Client, Entry, ImportJobs, Settings, make_record, read_list, resolve, worker_lock

MOVIE = dict(id=286217, title='Марсианин', original_title='The Martian', release_date='2015-09-30',
    overview='История астронавта, оставшегося на Марсе.', tagline='Верните его домой.', runtime=144,
    genres=[dict(id=878,name='Фантастика'),dict(id=18,name='Драма')], production_countries=[dict(name='США')],
    vote_average=7.7,vote_count=5000,poster_path='/poster.jpg',backdrop_path='/backdrop.jpg',
    imdb_id='tt3659388',external_ids=dict(imdb_id='tt3659388'),
    credits=dict(cast=[dict(name='Matt Damon')],crew=[dict(name='Ridley Scott',job='Director')]),
    release_dates=dict(results=[dict(iso_3166_1='RU',release_dates=[dict(certification='16+')])]))


class FakeClient:
    calls = []
    fail = False

    def __init__(self,*args,**kwargs):
        self.checkpoint = kwargs.get('checkpoint',lambda:None)

    def get(self,path,**params):
        self.checkpoint()
        type(self).calls.append((path,params))
        if type(self).fail:
            raise APIError('Сервис недоступен; можно продолжить.')
        if path == '/find/tt3659388':
            return dict(movie_results=[dict(id=286217)])
        if path.startswith('/find/'):
            return dict(movie_results=[])
        if path == '/movie/286217':
            return copy.deepcopy(MOVIE)
        if path == '/search/multi':
            return dict(total_pages=1,results=[dict(MOVIE,media_type='movie')])
        raise AssertionError(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'test.sqlite3')
        self.settings = Settings(self.root/'tmdb-settings.json')
        self.env = patch.dict(os.environ,{'TMDB_READ_TOKEN':'','TMDB_API_KEY':''})
        self.env.start()
        self.settings.save('test-token')
        FakeClient.calls, FakeClient.fail = [], False
        self.jobs = ImportJobs(self.store,self.settings,FakeClient)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_csv(self, text='imdb_id\ntt3659388', **options):
        token = self.jobs.start(read_list(text.encode()),background=False,**options)
        return token,self.jobs.status(token)


class ListTests(unittest.TestCase):
    def test_old_csv_bom_and_tmdb_urls(self):
        e = read_list('\ufeffimdb_id;title;year\ntt3659388;Марсианин;2015'.encode())[0]
        self.assertEqual((e.imdb_id,e.year),('tt3659388',2015))
        e = read_list(b'tmdb_url\nhttps://www.themoviedb.org/tv/1399-game-of-thrones')[0]
        self.assertEqual((e.tmdb_id,e.kind),(1399,'series'))
        self.assertEqual(read_list(b'tmdb_id,type\n286217,movie')[0].tmdb_id,286217)

    def test_twenty_thousand_rows_and_limits(self):
        data = b'imdb_id,title\n'+b'tt3659388,The Martian\n'*20000
        self.assertEqual(len(read_list(data)),20000)
        for invalid in (data+b'tt3659388,The Martian\n',b'tmdb_id\n286217',b'tmdb_id,type\n0,movie',
                        b'imdb_id\nhttps://evil.test/title/tt3659388',b'title\n'+b'A'*180,
                        b'title,year\nA,NaN',b'title\n\xff',b'title\nA\x00',b'title\n'+b'A'*11_000_000):
            with self.subTest(value=invalid[:50]),self.assertRaises(ValueError): read_list(invalid)


class ImportTests(Base):
    def test_all_metadata_and_separate_ratings(self):
        token,job = self.run_csv()
        self.assertEqual(job['state'],'done',job)
        row = self.store.film(1)
        self.assertEqual(row['film'].title,'Марсианин')
        self.assertEqual((row['film'].country,row['film'].age_rating,row['film'].duration_min),('США','16+',144))
        self.assertEqual(row['film'].description,MOVIE['overview'])
        self.assertEqual(row['art']['director'],'Ridley Scott')
        self.assertEqual(row['art']['actors'],['Matt Damon'])
        self.assertEqual(row['art']['tmdb_rating'],7.7)
        self.assertEqual(row['art']['imdb'],'—')
        self.assertTrue(row['art']['tmdb_poster_url'].startswith('https://image.tmdb.org/'))
        self.assertEqual(row['imdb_id'],'tt3659388')
        self.assertEqual(self.jobs.status(token)['counts'],{'added':1})

    def test_repeat_refreshes_owned_fields_but_keeps_manual_edits_sources_and_ratings(self):
        self.run_csv()
        previous = self.store.film(1,True)
        data, art = asdict(previous['film']),previous['art']
        data.update(description='Моё собственное описание фильма.',is_hidden=True,rating=8.0)
        art.update(director='Мой режиссёр',poster='uploads/manual.jpg',imdb='8.0',kp='8.1')
        self.store.save_film(data,art,movie_id=1,torrent=b'private-source',file_id=4,info_hash='source-hash')
        revision = self.store.film(1,True)['revision']
        with patch.dict(MOVIE,tagline='Новый слоган.',overview='Новое описание из TMDB.',vote_average=7.9):
            _,job = self.run_csv()
        row = self.store.film(1,True)
        self.assertEqual(job['counts'],{'updated':1})
        self.assertEqual(row['film'].description,'Моё собственное описание фильма.')
        self.assertTrue(row['film'].is_hidden)
        self.assertEqual(row['film'].rating,8.0)
        self.assertEqual((row['art']['imdb'],row['art']['kp']),('8.0','8.1'))
        self.assertEqual(row['art']['tmdb_rating'],7.9)
        self.assertEqual(row['art']['director'],'Мой режиссёр')
        self.assertEqual(row['art']['poster'],'uploads/manual.jpg')
        self.assertEqual(row['art']['tagline'],'Новый слоган.')
        self.assertEqual((row['torrent'],row['file_id'],row['revision']),(b'private-source',4,revision))
        self.assertEqual(len(self.store.films(True)),1)

    def test_existing_imdb_import_gets_missing_description_and_poster(self):
        from imdb_import import basic_record
        record = dict(imdb_id='tt3659388',title='The Martian',original='The Martian',display_title='Марсианин',
                      kind='movie',year=2015,duration=144,genres=['Драма'],rating=8.0,votes=1,directors=[],actors=[])
        with self.store.connect() as db: self.store.import_imdb(db,record)
        _,job = self.run_csv()
        row = self.store.film(1)
        self.assertEqual(job['counts'],{'updated':1})
        self.assertEqual(row['film'].description,MOVIE['overview'])
        self.assertEqual(row['film'].rating,8.0)
        self.assertEqual(row['art']['imdb'],'8.0')

    def test_movie_and_tv_numeric_ids_do_not_collide(self):
        record = make_record(MOVIE,'movie',Entry(2))
        writer = CatalogWriter(self.store)
        with self.store.connect() as db:
            writer.save(db,record,True)
            other = dict(record,kind='series',imdb_id='',title='Сериал',original='TV Show')
            writer.save(db,other,True)
        self.assertEqual(len(self.store.films()),2)

    def test_ambiguous_search_and_wrong_year_are_skipped(self):
        entry = Entry(2,title='Марсианин')
        with patch.object(FakeClient,'get',return_value=dict(total_pages=2,results=[])):
            result,record = resolve(entry,FakeClient())
        self.assertIsNone(record); self.assertEqual(result['status'],'ambiguous')
        _,job = self.run_csv('imdb_id,year\ntt3659388,2014')
        self.assertEqual(job['results'][0]['status'],'mismatch')
        self.assertEqual(self.store.films(True),[])

    def test_missing_runtime_skips_and_mpaa_is_not_converted(self):
        with patch.dict(MOVIE,runtime=None):
            _,job = self.run_csv()
        self.assertEqual(job['results'][0]['status'],'incomplete')
        record = make_record(dict(MOVIE,release_dates={'results':[]},adult=True),'movie',Entry(2))
        self.assertEqual(record['age_rating'],'—')

    def test_missing_russian_plot_uses_english_without_losing_russian_title(self):
        def get(path,**params):
            if params.get('language') == 'en-US':
                return dict(overview='An astronaut stranded on Mars.',tagline='Bring him home.')
            return dict(MOVIE,overview='',tagline='')
        with patch.object(FakeClient,'get',side_effect=get):
            result,record = resolve(Entry(2,tmdb_id=286217,kind='movie'),FakeClient())
        self.assertEqual(record['title'],'Марсианин')
        self.assertEqual(record['description'],'An astronaut stranded on Mars.')
        self.assertEqual(record['tagline'],'Bring him home.')

    def test_conflicting_external_ids_are_not_merged(self):
        self.run_csv()
        with patch.dict(MOVIE,id=286218,imdb_id='tt1234567',external_ids={'imdb_id':'tt1234567'}):
            record = make_record(MOVIE,'movie',Entry(2))
        with self.store.connect() as db:
            movie_id,action = CatalogWriter(self.store).save(db,record,True)
        self.assertEqual((movie_id,action),(None,'conflict'))
        self.assertEqual(len(self.store.films(True)),1)

    def test_series_runtime_and_aggregate_directors(self):
        data = dict(id=1,name='Сериал',original_name='A Show',first_air_date='2020-01-01',episode_run_time=[],
                    last_episode_to_air={'runtime':47},aggregate_credits=dict(cast=[dict(name='Actor')],
                    crew=[dict(name='Director',jobs=[dict(job='Director')])]),content_ratings={'results':[dict(iso_3166_1='RU',rating='18+')]})
        record = make_record(data,'series',Entry(2))
        self.assertEqual((record['duration'],record['director'],record['age_rating']),(47,'Director','18+'))

    def test_network_error_then_resume_after_restart(self):
        FakeClient.fail = True
        token,job = self.run_csv()
        self.assertEqual((job['state'],job['completed']),('error',0))
        restarted = ImportJobs(self.store,self.settings,FakeClient)
        FakeClient.fail = False
        restarted.resume(token,background=False)
        self.assertEqual(restarted.status(token)['counts'],{'added':1})
        restarted_again = ImportJobs(self.store,self.settings,FakeClient)
        self.assertEqual(restarted_again.status(token)['state'],'done')
        with self.assertRaises(ValueError): restarted_again.resume(token,background=False)

    def test_restart_recovers_running_and_keeps_completed_rows(self):
        token,job = self.run_csv('imdb_id\ntt3659388\ntt9999999')
        with self.store.connect() as db:
            db.execute("UPDATE tmdb_jobs SET state='running' WHERE id=?",(token,))
            db.execute("UPDATE tmdb_rows SET status='pending',result=NULL WHERE job_id=? AND position=1",(token,))
        restarted = ImportJobs(self.store,self.settings,FakeClient)
        self.assertEqual(restarted.status(token)['state'],'interrupted')
        restarted.resume(token,background=False)
        self.assertEqual(restarted.status(token)['counts'],{'added':1,'not_found':1})
        self.assertEqual(len(self.store.films(True)),1)

    def test_atomic_checkpoint_rolls_back_catalog_when_row_save_fails(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON tmdb_rows BEGIN SELECT RAISE(ABORT,'test checkpoint failure'); END")
        _,job = self.run_csv()
        self.assertEqual((job['state'],job['completed']),('error',0))
        self.assertEqual(self.store.films(True),[])

    def test_cancel_keeps_already_saved_card_and_can_resume(self):
        original = CatalogWriter.save
        def save(writer,db,record,publish):
            result = original(writer,db,record,publish)
            db.execute('UPDATE tmdb_jobs SET cancel=1')
            return result
        with patch.object(CatalogWriter,'save',save):
            token,job = self.run_csv('imdb_id\ntt3659388\ntt9999999')
        self.assertEqual((job['state'],job['completed']),('cancelled',1))
        self.assertEqual(len(self.store.films(True)),1)
        self.jobs.resume(token,background=False)
        self.assertEqual(self.jobs.status(token)['completed'],2)

    def test_process_lock_blocks_another_importer_and_recovery(self):
        with worker_lock(self.jobs.lock_path):
            another = ImportJobs(self.store,self.settings,FakeClient)
            with self.assertRaises(ValueError): another.start([Entry(2,imdb_id='tt3659388')],background=False)

    def test_pagination_is_bounded_and_report_contains_every_row(self):
        token,job = self.run_csv('imdb_id\n'+'tt9999999\n'*205)
        self.assertEqual((len(job['results']),job['pages']),(100,3))
        self.assertEqual(len(self.jobs.status(token,3)['results']),5)
        self.assertEqual(len(list(self.jobs.report(token))),205)


class HTTPTests(Base):
    class Response:
        def __init__(self,data): self.data = data
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,size): return json.dumps(self.data).encode()

    def test_bearer_and_cache_do_not_store_credential(self):
        client = Client(self.store,'private-token')
        with patch.object(client.opener,'open',return_value=self.Response(MOVIE)) as call:
            self.assertEqual(client.get('/movie/286217')['id'],286217)
            client.get('/movie/286217')
        self.assertEqual(call.call_count,1)
        req = call.call_args.args[0]
        self.assertNotIn('private-token',req.full_url)
        self.assertEqual(req.get_header('Authorization'),'Bearer private-token')
        with self.store.connect() as db:
            self.assertNotIn('private-token',str([tuple(r) for r in db.execute('SELECT * FROM tmdb_cache')]))

    def test_429_retries_and_auth_errors_are_sanitized(self):
        client = Client(self.store,'private-token')
        error = HTTPError('https://api.themoviedb.org?api_key=secret',429,'rate',{'Retry-After':'1'},None)
        with patch.object(client.opener,'open',side_effect=[error,self.Response(MOVIE)]) as call,patch.object(client,'wait'):
            client.get('/movie/286217')
        self.assertEqual(call.call_count,2)
        with patch.object(client.opener,'open',side_effect=HTTPError('secret',401,'bad',{},None)):
            with self.assertRaises(APIError) as caught: client.get('/authentication')
        self.assertNotIn('secret',str(caught.exception))

    def test_bad_json_network_failure_and_image_urls(self):
        client = Client(self.store,'private-token')
        with patch.object(client.opener,'open',side_effect=URLError('private-token')),patch.object(client,'wait'):
            with self.assertRaises(APIError) as caught: client.get('/movie/1')
        self.assertNotIn('private-token',str(caught.exception))
        from tmdb_import import image_url
        self.assertEqual(image_url('//evil.test/image.jpg','w500'),'')
        self.assertEqual(image_url('/../file.jpg','w500'),'')

    def test_long_retry_after_stops_instead_of_retrying_early(self):
        client = Client(self.store,'private-token')
        error = HTTPError('secret',429,'rate',{'Retry-After':'600'},io.BytesIO())
        with patch.object(client.opener,'open',side_effect=error) as call,patch.object(client,'wait'):
            with self.assertRaises(APIError): client.get('/movie/1')
        self.assertEqual(call.call_count,1)

    def test_invalid_json_and_old_api_key(self):
        client = Client(self.store,'a'*32)
        with patch.object(client.opener,'open',return_value=self.Response([])):
            with self.assertRaises(APIError): client.get('/movie/1')
        with patch.object(client.opener,'open',return_value=self.Response(MOVIE)) as call:
            client.get('/movie/286217')
        self.assertIn('api_key='+'a'*32,call.call_args.args[0].full_url)


import support
from support import AppTestCase


class AdminTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.old_jobs = self.app.extensions['tmdb_importer']
        settings = Settings(Path(self.temp.name)/'settings.json')
        self.env = patch.dict(os.environ,{'TMDB_READ_TOKEN':'','TMDB_API_KEY':''}); self.env.start()
        settings.save('test-token')
        FakeClient.fail,FakeClient.calls = False,[]
        self.jobs = ImportJobs(self.store,settings,FakeClient)
        self.app.extensions['tmdb_importer'] = self.jobs

    def tearDown(self):
        self.app.extensions['tmdb_importer'] = self.old_jobs
        self.env.stop()
        super().tearDown()

    def test_admin_access_csrf_and_no_token_disclosure(self):
        self.accounts()
        self.assertEqual(self.viewer.get('/admin/tmdb').status_code,403)
        self.assertEqual(self.post(self.viewer,'/admin/tmdb/import').status_code,403)
        self.assertEqual(self.admin.post('/admin/tmdb/settings',data={'credential':'test-token'}).status_code,400)
        response = self.admin.get('/admin/tmdb')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('test-token',response.text)
        self.assertIn('20 000',response.text)
        self.assertEqual(self.admin.get('/about/data').status_code,200)

    def test_import_card_images_ratings_and_report(self):
        self.accounts()
        response = self.post(self.admin,'/admin/tmdb/import',data={'list':(io.BytesIO(b'imdb_id\ntt3659388'),'movies.csv'),'publish':'on'})
        self.assertEqual(response.status_code,202,response.text)
        token = response.json['job']; deadline = time.monotonic()+5
        while self.jobs.status(token)['state'] == 'running' and time.monotonic()<deadline: time.sleep(.01)
        job = self.admin.get(f'/admin/tmdb/jobs/{token}').json
        self.assertEqual(job['state'],'done',job)
        movie_id = job['results'][0]['movie_id']
        response = self.admin.get(f'/movie/{movie_id}')
        self.assertEqual(response.status_code,200)
        self.assertIn('image.tmdb.org/t/p/w500/poster.jpg',response.text)
        self.assertIn('image.tmdb.org/t/p/w1280/backdrop.jpg',response.text)
        self.assertIn('<span>TMDB</span>',response.text)
        self.assertIn('Ridley Scott',response.text)
        report = self.admin.get(f'/admin/tmdb/jobs/{token}/report.csv')
        self.assertIn('tt3659388',report.text)
        self.assertNotIn('test-token',report.text)
        self.assertEqual(self.viewer.get(f'/admin/tmdb/jobs/{token}').status_code,403)
        self.assertEqual(self.post(self.viewer,f'/admin/tmdb/jobs/{token}/resume').status_code,403)

    def test_key_validation_and_large_multipart_upload(self):
        self.accounts()
        with patch('tmdb_admin.Client') as factory:
            factory.return_value.get.return_value = {'success':True}
            response = self.post(self.admin,'/admin/tmdb/settings',data={'credential':'replacement-token'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.jobs.settings.credential(),'replacement-token')
        # Exceeds the previous global 3 MB admin request limit, but stays in 20k rows.
        data = ('imdb_id,title\n'+('tt3659388,'+'Ф'*150+'\n')*15000).encode()
        with patch.object(self.jobs,'start',return_value='a'*32) as start:
            response = self.post(self.admin,'/admin/tmdb/import',data={'list':(io.BytesIO(data),'large.csv')})
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(len(start.call_args.args[0]),15000)


if __name__ == '__main__':
    unittest.main()
