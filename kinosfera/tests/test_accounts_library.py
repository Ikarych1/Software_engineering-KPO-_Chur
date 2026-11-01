"""Accounts, administrator library, reviews, progress and subscriptions."""
from dataclasses import asdict
import io
from pathlib import Path
import re
import time
import unittest

import support
from support import AppTestCase


class AccountsTests(AppTestCase):
    def test_first_account_is_only_admin_and_password_is_hashed(self):
        self.accounts()
        users = self.store.users()
        self.assertEqual([u['role'] for u in users], ['ADMIN', 'USER'])
        self.assertTrue(self.store.account(user_id=1)['password_hash'].startswith('scrypt:'))
        self.assertEqual(self.admin.get('/admin/').status_code, 200)
        self.assertEqual(self.viewer.get('/admin/').status_code, 403)

    def test_guests_cannot_watch_or_access_personal_data(self):
        for path in ['/watch/1', '/profile', '/admin/', '/rooms']:
            self.assertEqual(self.viewer.get(path).status_code, 302, path)
        self.assertEqual(self.viewer.get('/api/profile/state').status_code, 401)
        self.assertEqual(self.viewer.post('/api/play/1/start', json={}, headers={'X-CSRF-Token': self.csrf(self.viewer)}).status_code, 401)

    def test_csrf_and_invalid_password_registration(self):
        self.assertEqual(self.viewer.post('/auth/register', data={}).status_code, 400)
        response = self.viewer.post('/auth/register', data=dict(email='x@example.test', username='XX',
                                    password='short', confirm='short', csrf_token=self.csrf(self.viewer)))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.store.accounts_exist())

    def test_duplicate_email_normalized_and_login_logout_persist(self):
        self.accounts()
        response = self.register(self.app.test_client(), 'VIEWER@EXAMPLE.TEST')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.store.users()), 2)
        self.post(self.viewer, '/auth/logout')
        self.assertEqual(self.viewer.get('/profile').status_code, 302)
        response = self.post(self.viewer, '/auth/login', data={'email': 'viewer@example.test', 'password': 'test-password-123', 'next': 'https://bad.example'})
        self.assertEqual(response.location, '/')
        self.assertEqual(self.viewer.get('/profile').status_code, 200)

    def test_personal_state_is_private_and_ratings_feed_the_community_score(self):
        self.accounts()
        self.post(self.viewer, '/api/profile/state', json={'watched': [1], 'watchlist': [2], 'ratings': {'1': 9}})
        self.post(self.admin, '/api/profile/state', json={'ratings': {'1': 7}})
        self.assertEqual(self.admin.get('/api/profile/state').json['watchlist'], [])
        self.assertEqual(self.store.film_stats()[1]['avg'], 8.0)
        self.post(self.viewer, '/api/profile/state', json={'ratings': {}})
        self.assertEqual(self.store.film_stats()[1]['votes'], 1)

    def test_settings_are_whitelisted(self):
        self.accounts()
        self.post(self.viewer, '/api/profile/state', json={'settings': {'name': 'Новое Имя', 'color': 'blue', 'evil': {'x': 1}}})
        settings = self.viewer.get('/api/profile/state').json['settings']
        self.assertEqual(settings, {'name': 'Новое Имя', 'color': 'blue'})
        self.assertEqual(self.store.account(user_id=2)['username'], 'Новое Имя')

    def test_password_change_revokes_other_sessions(self):
        self.accounts()
        other = self.app.test_client()
        self.post(other, '/auth/login', data={'email': 'viewer@example.test', 'password': 'test-password-123'})
        self.post(self.viewer, '/auth/password', data=dict(current_password='test-password-123', password='new-password-123', confirm='new-password-123'))
        self.assertEqual(other.get('/profile').status_code, 302)
        self.assertEqual(self.viewer.get('/profile').status_code, 200)

    def test_last_admin_and_disabled_accounts_are_protected(self):
        self.accounts()
        self.post(self.admin, '/admin/users/1', data={'role': 'USER', 'active': 'on'})
        self.assertEqual(self.store.account(user_id=1)['role'], 'ADMIN')
        self.post(self.admin, '/admin/users/2', data={'role': 'USER'})
        self.assertEqual(self.viewer.get('/profile').status_code, 302)

    def test_demo_subscription_is_per_user(self):
        self.accounts()
        self.post(self.viewer, '/subscription/pay', data={'tariff': 'plus'})
        self.assertEqual(len(self.store.payments(2)), 1)
        self.assertEqual(self.store.payments(1), [])
        self.assertTrue(self.store.user(self.store.account(user_id=2)).is_subscribed)
        self.assertIn('Активна до', self.viewer.get('/profile?section=subscription').text)


class PasswordResetTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.outbox = self.app.extensions['mail_outbox'] = []

    def request_reset(self, client, email):
        return self.post(client, '/auth/forgot', data={'email': email})

    def reset_link(self):
        text = self.outbox[-1].get_body(('plain',)).get_content()
        return re.search(r'http://localhost(/auth/reset/\S+)', text).group(1)

    def test_reset_by_email_changes_password_once_and_revokes_sessions(self):
        self.accounts()
        guest = self.app.test_client()
        self.assertIn('Забыли пароль?', guest.get('/auth/login').get_data(as_text=True))
        response = self.request_reset(guest, ' Viewer@Example.TEST ')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Проверьте почту', response.get_data(as_text=True))
        self.assertEqual(len(self.outbox), 1)
        self.assertEqual(self.outbox[0]['To'], 'viewer@example.test')
        link = self.reset_link()
        token = link.rsplit('/', 1)[1]
        with self.store.connect() as db:
            stored = [r[0] for r in db.execute('SELECT token_hash FROM password_resets')]
        self.assertNotIn(token, stored)  # only the hash is kept in the database

        self.assertEqual(guest.get(link).status_code, 200)
        response = self.post(guest, link, data={'password': 'short', 'confirm': 'short'})
        self.assertIn('от 10 до 128', response.get_data(as_text=True))
        response = self.post(guest, link, data={'password': 'brand-new-pass-1', 'confirm': 'brand-new-pass-1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(guest.get('/profile').status_code, 200)
        self.assertEqual(self.viewer.get('/profile').status_code, 302)  # old session revoked
        self.assertEqual(guest.get(link).status_code, 410)  # single use

        other = self.app.test_client()
        self.post(other, '/auth/login', data={'email': 'viewer@example.test', 'password': 'test-password-123'})
        self.assertEqual(other.get('/profile').status_code, 302)
        self.post(other, '/auth/login', data={'email': 'viewer@example.test', 'password': 'brand-new-pass-1'})
        self.assertEqual(other.get('/profile').status_code, 200)

    def test_unknown_email_gets_same_answer_without_letter(self):
        self.accounts()
        guest = self.app.test_client()
        known = self.request_reset(guest, 'viewer@example.test').get_data(as_text=True)
        unknown = self.request_reset(guest, 'nobody@example.test').get_data(as_text=True)
        self.assertEqual(known.replace('viewer@', ''), unknown.replace('nobody@', ''))
        self.assertEqual(len(self.outbox), 1)
        self.assertEqual(self.post(guest, '/auth/forgot', data={'email': 'not-an-email'}).status_code, 200)
        self.assertEqual(len(self.outbox), 1)

    def test_disabled_account_cooldown_and_expiry(self):
        self.accounts()
        guest = self.app.test_client()
        self.request_reset(guest, 'viewer@example.test')
        self.request_reset(guest, 'viewer@example.test')
        self.assertEqual(len(self.outbox), 1)  # second letter within a minute is not sent
        link = self.reset_link()
        with self.store.connect() as db:
            db.execute('UPDATE password_resets SET expires_at=?', (time.time() - 1,))
        self.assertEqual(guest.get(link).status_code, 410)
        self.post(self.admin, '/admin/users/2', data={'role': 'USER'})
        with self.store.connect() as db:
            db.execute('DELETE FROM password_resets')
        self.request_reset(guest, 'viewer@example.test')
        self.assertEqual(len(self.outbox), 1)

    def test_profile_password_change_invalidates_reset_links(self):
        self.accounts()
        guest = self.app.test_client()
        self.request_reset(guest, 'viewer@example.test')
        link = self.reset_link()
        self.post(self.viewer, '/auth/password', data=dict(current_password='test-password-123',
                                                           password='new-password-123', confirm='new-password-123'))
        self.assertEqual(guest.get(link).status_code, 410)

    def test_requests_are_rate_limited_per_address(self):
        guest = self.app.test_client()
        for _ in range(5):
            self.assertIn('Проверьте почту', self.request_reset(guest, 'x@example.test').get_data(as_text=True))
        self.assertIn('Слишком много запросов', self.request_reset(guest, 'x@example.test').get_data(as_text=True))


class LibraryTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.video = support.make_video(Path(self.temp.name) / 'seed' / 'film.webm', seconds=12)
        self.torrent = support.seeded_torrent(self.video)

    def film_form(self, **extra):
        data = dict(title='Local test film', description='A locally generated test video.', genres='Test, Drama',
                    year='2026', duration_min='1', rating='8.0', age_rating='12+', published='on')
        data.update(extra)
        return data

    def test_admin_uploads_torrent_publishes_and_hides_film(self):
        self.accounts()
        data = self.film_form(torrent=(io.BytesIO(self.torrent), 'test.torrent'))
        self.assertEqual(self.post(self.admin, '/admin/films/new', data=data).status_code, 302)
        row = self.store.film(13)
        self.assertEqual(row['file_id'], 0)
        self.assertTrue(row['info_hash'])
        self.assertEqual(self.viewer.get('/watch/13').status_code, 200)
        self.assertIn('Local test film', self.viewer.get('/').text)
        data = self.film_form(file_id='0')
        data.pop('published')
        self.assertEqual(self.post(self.admin, '/admin/films/13', data=data).status_code, 302)
        self.assertIsNone(self.store.film(13))
        self.assertEqual(self.viewer.get('/watch/13').status_code, 404)

    def test_poster_upload_is_validated_and_used(self):
        self.accounts()
        png = b'\x89PNG\r\n\x1a\n' + b'\0' * 64
        data = self.film_form(torrent=(io.BytesIO(self.torrent), 'test.torrent'), poster=(io.BytesIO(png), 'p.png'))
        self.post(self.admin, '/admin/films/new', data=data)
        poster = self.store.film(13)['art']['poster']
        self.assertTrue(poster.startswith('uploads/poster-') and poster.endswith('.png'))
        path = Path(self.app.static_folder) / poster
        self.assertTrue(path.exists())
        path.unlink()
        bad = self.film_form(torrent=(io.BytesIO(self.torrent), 'test.torrent'), poster=(io.BytesIO(b'<svg/>'), 'x.png'))
        self.assertIn('JPEG, PNG или WebP', self.post(self.admin, '/admin/films/new', data=bad).text)

    def test_viewer_cannot_change_library(self):
        self.accounts()
        self.assertEqual(self.post(self.viewer, '/admin/films/new', data=self.film_form()).status_code, 403)
        self.assertEqual(self.viewer.get('/admin/api/engine').status_code, 403)

    def test_invalid_torrent_does_not_publish_a_film(self):
        self.accounts()
        data = self.film_form(torrent=(io.BytesIO(b'not a torrent'), 'broken.torrent'))
        self.assertEqual(self.post(self.admin, '/admin/films/new', data=data).status_code, 200)
        self.assertIsNone(self.store.film(13, include_hidden=True))

    def test_draft_without_source_and_deletion(self):
        self.accounts()
        data = self.film_form()
        data.pop('published')
        self.assertEqual(self.post(self.admin, '/admin/films/new', data=data).status_code, 302)
        self.assertIsNotNone(self.store.film(13, include_hidden=True))
        self.post(self.admin, '/admin/films/13/delete', data={'confirm': 'wrong'})
        self.assertIsNotNone(self.store.film(13, include_hidden=True))
        self.post(self.admin, '/admin/films/13/delete', data={'confirm': 'Local test film'})
        self.assertIsNone(self.store.film(13, include_hidden=True))

    def test_metadata_edit_keeps_running_sessions(self):
        self.accounts()
        self.post(self.admin, '/admin/films/new', data=self.film_form(torrent=(io.BytesIO(self.torrent), 'test.torrent')))
        revision = self.store.film(13)['revision']
        self.post(self.admin, '/admin/films/13', data=self.film_form(title='Renamed film', file_id='0'))
        self.assertEqual(self.store.film(13)['revision'], revision)
        self.assertEqual(self.store.film(13)['film'].title, 'Renamed film')


class ReviewsAndProgressTests(AppTestCase):
    def test_review_lifecycle_and_moderation(self):
        self.accounts()
        response = self.post(self.viewer, '/movie/2/review', data={'score': '9', 'text': 'Отличный фильм, смотреть всем!'})
        self.assertEqual(response.status_code, 302)
        page = self.viewer.get('/movie/2').text
        self.assertIn('Отличный фильм', page)
        self.assertEqual(self.store.film_stats()[2]['avg'], 9.0)
        self.post(self.viewer, '/movie/2/review', data={'score': '6', 'text': 'Пересмотрел — уже не так впечатлил.'})
        self.assertEqual(len(self.store.reviews(2)), 1)
        review_id = self.store.reviews(2)[0]['id']
        self.post(self.admin, f'/admin/reviews/{review_id}/delete', data={})
        self.assertEqual(self.store.reviews(2), [])

    def test_short_review_and_bad_score_are_rejected(self):
        self.accounts()
        self.post(self.viewer, '/movie/2/review', data={'score': '11', 'text': 'Ок'})
        self.post(self.viewer, '/movie/2/review', data={'score': '5', 'text': 'Ок'})
        self.assertEqual(self.store.reviews(2), [])

    def test_progress_powers_continue_watching_and_history(self):
        self.accounts()
        self.store.save_progress(2, 3, 1200, 6000, 30)
        home = self.viewer.get('/').text
        self.assertIn('Продолжить просмотр', home)
        self.assertIn('Остановились на 20:00', self.viewer.get('/profile?section=history').text)
        self.post(self.viewer, '/progress/3/hide', data={})
        self.assertNotIn('Продолжить просмотр', self.viewer.get('/').text)
        self.assertTrue(self.store.save_progress(2, 3, 5900, 6000))

    def test_recommendations_follow_ratings(self):
        self.accounts()
        self.post(self.viewer, '/api/profile/state', json={'ratings': {'1': 10, '2': 10, '7': 10}})
        import services
        rows = self.store.films()
        picks = services.recommend(rows, self.store.film_stats(), self.store.state(2), [])
        self.assertTrue(picks)
        self.assertIn('Фантастика', picks[0][0]['film'].genres)
        self.assertNotIn(1, [row['film'].id for row, _ in picks])


if __name__ == '__main__':
    unittest.main()
