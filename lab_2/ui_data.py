"""Curated fixture data for the interface. Scores are illustrative, not live APIs."""
from models import MediaItem

EXTRA_FILMS = [
    MediaItem(9, 'Дюна', 'Пол Атрейдес отправляется на самую опасную планету во Вселенной, чтобы защитить будущее своей семьи и своего народа.', ['Фантастика', 'Приключения', 'Драма'], 2021, 'США', 8.2, 155, '12+'),
    MediaItem(10, 'Прибытие', 'Лингвист Луиза Бэнкс пытается понять язык таинственных гостей и найти ответ на главный вопрос: зачем они здесь?', ['Фантастика', 'Драма'], 2016, 'США', 8.0, 116, '16+'),
    MediaItem(11, 'Бегущий по лезвию 2049', 'Офицер Кей находит секрет, который может изменить мир. Его поиски ведут к исчезнувшему тридцать лет назад Рику Декарду.', ['Фантастика', 'Драма', 'Триллер'], 2017, 'США', 7.8, 164, '18+'),
    MediaItem(12, 'Остров проклятых', 'Два судебных пристава отправляются в больницу для душевнобольных преступников, чтобы расследовать исчезновение пациентки.', ['Триллер', 'Драма'], 2010, 'США', 8.5, 138, '16+'),
]

# These snapshots intentionally do not claim to reflect current external ratings.
ART = {
    1: dict(slug='interstellar', original='Interstellar', kp='8.7', imdb='8.7', director='Кристофер Нолан', actors=['Мэттью Макконахи', 'Энн Хэтэуэй', 'Джессика Честейн', 'Майкл Кейн'], tagline='Любовь вне времени. Путешествие за пределы возможного.', backdrop='interstellar-backdrop'),
    2: dict(slug='inception', original='Inception', kp='8.7', imdb='8.8', director='Кристофер Нолан', actors=['Леонардо ДиКаприо', 'Джозеф Гордон-Левитт', 'Эллиот Пейдж', 'Том Харди']),
    3: dict(slug='green-mile', original='The Green Mile', kp='9.1', imdb='8.6', director='Фрэнк Дарабонт', actors=['Том Хэнкс', 'Майкл Кларк Дункан', 'Дэвид Морс']),
    4: dict(slug='dark-knight', original='The Dark Knight', kp='8.5', imdb='9.0', director='Кристофер Нолан', actors=['Кристиан Бэйл', 'Хит Леджер', 'Гэри Олдман']),
    5: dict(slug='lotr', original='The Lord of the Rings', kp='8.6', imdb='8.9', director='Питер Джексон', actors=['Элайджа Вуд', 'Иэн Маккеллен', 'Вигго Мортенсен']),
    6: dict(slug='pulp-fiction', original='Pulp Fiction', kp='8.7', imdb='8.9', director='Квентин Тарантино', actors=['Джон Траволта', 'Ума Турман', 'Сэмюэл Л. Джексон']),
    7: dict(slug='matrix', original='The Matrix', kp='8.5', imdb='8.7', director='Лана и Лилли Вачовски', actors=['Киану Ривз', 'Лоренс Фишбёрн', 'Кэрри-Энн Мосс']),
    8: dict(slug='forrest-gump', original='Forrest Gump', kp='8.9', imdb='8.8', director='Роберт Земекис', actors=['Том Хэнкс', 'Робин Райт', 'Гэри Синиз']),
    9: dict(slug='dune', original='Dune', kp='7.7', imdb='8.0', director='Дени Вильнёв', actors=['Тимоти Шаламе', 'Ребекка Фергюсон', 'Зендея'], tagline='За пределами знакомого мира начинается твоя история.', backdrop='dune-backdrop'),
    10: dict(slug='arrival', original='Arrival', kp='8.0', imdb='7.9', director='Дени Вильнёв', actors=['Эми Адамс', 'Джереми Реннер', 'Форест Уитакер']),
    11: dict(slug='blade-runner', original='Blade Runner 2049', kp='7.8', imdb='8.0', director='Дени Вильнёв', actors=['Райан Гослинг', 'Харрисон Форд', 'Ана де Армас']),
    12: dict(slug='shutter-island', original='Shutter Island', kp='8.5', imdb='8.2', director='Мартин Скорсезе', actors=['Леонардо ДиКаприо', 'Марк Руффало', 'Бен Кингсли']),
}

RAILS = [
    dict(id='for-you', title='В вашем вкусе', subtitle='Большие истории, которые останутся с вами', ids=[10, 9, 11, 2, 7, 5, 12], label='Подборка для вас'),
    dict(id='tonight', title='Планы на вечер', subtitle='Знакомые шедевры и новые открытия', ids=[12, 4, 6, 8, 3, 5, 1]),
    dict(id='similar', title='Выбирают зрители с похожим вкусом', subtitle='Хорошее кино объединяет', ids=[11, 10, 2, 9, 7, 12], social=True),
    dict(id='friends', title='Друзья рекомендуют', subtitle='То, что хочется обсудить после титров', ids=[5, 8, 3, 6, 4, 1], friends=True),
]

PEOPLE = [dict(name='Анна', initials='АС', color='peach', note='Совпадение вкусов 92%'), dict(name='Михаил', initials='МК', color='blue', note='Совпадение вкусов 87%'), dict(name='Соня', initials='СЛ', color='purple', note='Совпадение вкусов 84%')]

ARCHIVE = [dict(code='evening-1', title='Вечер с Интерстелларом', movie_id=1, date='2 октября', members='Анна, Михаил и вы', messages=[('Анна', 'Какая же невероятная музыка!', '21:42'), ('Михаил', 'На следующей неделе смотрим «Прибытие»?', '21:44'), ('Вы', 'Договорились! Добавил в закладки.', '21:45')]), dict(code='evening-2', title='Пятничная Матрица', movie_id=7, date='25 сентября', members='Соня и вы', messages=[('Соня', 'Этот фильм никогда не устареет.', '22:12'), ('Вы', 'Идеальный выбор на вечер.', '22:14')])]
