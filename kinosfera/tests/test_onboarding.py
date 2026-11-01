"""First-visit enforcement and real cold-start signals without fabricated ratings."""
import json
from urllib.parse import parse_qs, urlsplit

from support import AppTestCase


class OnboardingTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.register(self.viewer, 'new@example.test', onboard=False)
        self.viewer.get('/auth/preferences')
        rows = self.store.films()
        self.genres = sorted({g for r in rows for g in r['film'].genres})[:3]
        self.movies = [r['film'].id for r in rows[:3]]

    def test_required_after_registration_and_for_existing_session(self):
        response = self.viewer.get('/profile?section=watchlist')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response.location).path, '/auth/preferences')
        self.assertEqual(parse_qs(urlsplit(response.location).query)['next'], ['/profile?section=watchlist'])
        for path in ['/api/profile/state', '/api/recommendations']:
            response = self.viewer.get(path)
            self.assertEqual(response.status_code, 428)
            self.assertTrue(response.json['onboarding_required'])
        self.assertEqual(self.post(self.viewer, '/api/profile/state', json={'ratings':{'1':10}}).status_code, 428)

    def test_saving_persists_choices_and_personalizes_without_inventing_ratings(self):
        before = self.store.state(1)
        response = self.post(self.viewer, '/auth/preferences', data=dict(genres=self.genres, movie_ids=self.movies, next='/profile'))
        self.assertEqual(response.location, '/profile')
        with self.store.connect() as db:
            pref = db.execute('SELECT * FROM user_preferences WHERE user_id=1').fetchone()
            self.assertEqual(json.loads(pref['genres']), self.genres)
            self.assertEqual(json.loads(pref['liked_movie_ids']), self.movies)
            self.assertIsNotNone(pref['onboarding_completed_at'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ratings').fetchone()[0], 0)
        self.assertEqual(self.store.state(1), before)
        data = self.viewer.get('/api/recommendations').json
        self.assertTrue(data['personalized'])
        self.assertFalse(set(self.movies) & {i['id'] for i in data['items']})
        self.post(self.viewer, '/auth/logout')
        self.assertEqual(self.post(self.viewer, '/auth/login', data={'email':'new@example.test','password':'test-password-123'}).location, '/')

    def test_validation_csrf_hidden_films_and_safe_redirect(self):
        for genres, movies in [(self.genres[:2], self.movies), (self.genres, self.movies[:2]),
                               (self.genres, [self.movies[0]]*3), (self.genres, self.movies[:2]+[999999])]:
            result = self.post(self.viewer, '/auth/preferences', data=dict(genres=genres, movie_ids=movies))
            self.assertEqual(result.status_code, 422)
        self.assertEqual(self.viewer.post('/auth/preferences', data={}).status_code, 400)
        response = self.post(self.viewer, '/auth/preferences', data=dict(genres=self.genres, movie_ids=self.movies, next='//bad.example'))
        self.assertEqual(response.location, '/')

    def test_previously_registered_user_without_preferences_must_choose_on_login(self):
        self.post(self.viewer, '/auth/logout')
        old_id = self.store.register('legacy@example.test', 'Legacy Viewer', self.store.account(user_id=1)['password_hash'])
        state = dict(watched=[5], watchlist=[7], ratings={'5':9}, clips=[], settings={}, rooms={}, friends=[])
        self.store.save_state(old_id, state)
        old_account = self.store.account(user_id=old_id)
        response = self.post(self.viewer, '/auth/login', data={'email':'legacy@example.test','password':'test-password-123'})
        self.assertIn('/auth/preferences', response.location)
        self.assertEqual(self.store.state(old_id), state)
        self.assertEqual(self.store.account(user_id=old_id)['password_hash'], old_account['password_hash'])
        self.assertEqual(self.viewer.get('/auth/preferences/films?q=test').status_code, 200)
