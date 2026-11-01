# Просмотр тестовой базы и SQL-запросы

**Историческая аналитическая версия. Совместимая с текущим проектом база находится в [cinema_project_v3](../../cinema_project_v3/README.md). Для работы с общей схемой используй новый файл v3.**

Готовый файл: **[cinema_test.sqlite3](cinema_test.sqlite3)**.

Это физическая SQLite-база из тестового набора v2: **24 таблицы, 205 полей, 56 внешних связей и 13 414 770 строк**. В ней можно просматривать данные, фильтровать и сортировать таблицы, выполнять JOIN, агрегаты и свои SQL-запросы. Подключение локальное, по пути к файлу.

## Открыть в DBeaver

1. Установи [DBeaver Community](https://dbeaver.io/download/), если его ещё нет.
2. Выбери **Database → New Database Connection → SQLite**.
3. В поле **Path** укажи:

   ```text
   C:\Users\sasha\Desktop\lab_2\datasets\cinema_ml32m_v2\sqlite\cinema_test.sqlite3
   ```

4. Заверши подключение; если DBeaver предложит загрузить драйвер SQLite, разреши загрузку.
5. В дереве соединения открой **Tables**, затем нужную таблицу и вкладку **Data**.
6. Открой для соединения **SQL Editor → New SQL Script**. Напиши запрос и выполни его кнопкой запуска SQL.

Для перенесённого файла укажи его новый путь. Сервер, порт и учётная запись для этого подключения не требуются. Настройка файлового подключения описана в [документации DBeaver](https://dbeaver.com/docs/dbeaver/Database-driver-SQLite/), работа редактора — в [SQL execution](https://dbeaver.com/docs/dbeaver/SQL-Execution/).

Альтернатива — [DB Browser for SQLite](https://sqlitebrowser.org/): **Open Database**, выбрать тот же файл; просмотр во вкладке **Browse Data**, запросы во вкладке **Execute SQL**.

## Первый запрос

```sql
SELECT movie_id, title, year, imdb_rating, tmdb_rating
FROM movies
ORDER BY imdb_rating DESC
LIMIT 20;
```

В [example_queries.sql](example_queries.sql) находятся десять готовых запросов: количества записей, карточки и рейтинги, оценки пользователя, события, закладки, JSON-жанры, сеансы и комнаты. Запускай запросы по одному. Для примеров с пользователем автоматически выбирается первый существующий пользователь набора.

## Как устроена база

- Структура соответствует [ERD набора](../erd/index.html); физический DDL — в [schema.sql](schema.sql).
- PK, составные UK и внешние ключи объявлены в самой базе. Созданы индексы для связей и основных запросов по истории пользователя.
- JSON-массивы и объекты хранятся в `TEXT` с `CHECK(json_valid(...))`; их можно читать функциями SQLite `json_each`, `json_extract`.
- Логические `bigint`/`boolean` представлены `INTEGER`, дробные показатели — `REAL`. Используется проверка типов [STRICT](https://www.sqlite.org/stricttables.html). Для всех примеров подходит современный клиент с SQLite 3.38+ и встроенной [поддержкой JSON](https://www.sqlite.org/json1.html).
- Пустые скалярные значения CSV преобразованы в SQL `NULL`. Пустой массив остаётся JSON `[]`. Даты в секундах/миллисекундах сохранены согласно исходным именам полей.
- Перед изменениями с проверкой связей включи для своего SQL-соединения:

  ```sql
  PRAGMA foreign_keys = ON;
  ```

  Эта настройка действует на конкретное соединение; подробности — в [SQLite Foreign Key Support](https://www.sqlite.org/foreignkeys.html).

База — отдельная рабочая копия для экспериментов. Сохранённые SQL-изменения относятся к этому файлу; CSV-источник и рабочая база сайта `.local/cinema.sqlite3` от него независимы. Происхождение исходных и дополненных данных описано в [README датасета](../README.md).

## Проверка и повторная сборка

[sqlite_validation.json](sqlite_validation.json) содержит количества строк, результат `integrity_check`, проверку всех внешних ключей, SHA-256 и результаты выполнения примеров. Контрольная сумма файла также лежит в `cinema_test.sqlite3.sha256`.

Для пересборки с новым именем из корня проекта:

```powershell
.venv\Scripts\python.exe datasets/cinema_ml32m_v2/sqlite/build_sqlite.py --output datasets/cinema_ml32m_v2/sqlite/cinema_test_new.sqlite3
```

Сборщик читает файлы v2 и физически создаёт базу по ERD. Существующий файл БД он не перезаписывает. Файл SQLite предоставлен отдельно от ранее подготовленного архива CSV.
