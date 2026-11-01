-- SQLite schema of the standalone v2 fixture.
PRAGMA foreign_keys = ON;

CREATE TABLE "ratings" (
  "rating_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "score" INTEGER NOT NULL CHECK("score" BETWEEN 1 AND 10),
  "rated_at" INTEGER NOT NULL,
  "source_movie_id" INTEGER,
  "split" TEXT,
  PRIMARY KEY ("rating_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("source_movie_id") REFERENCES "movie_mapping" ("source_movie_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "movies" (
  "movie_id" INTEGER NOT NULL,
  "imdb_id" TEXT,
  "tmdb_id" INTEGER,
  "catalog_kind" TEXT NOT NULL,
  "content_type" TEXT NOT NULL,
  "title" TEXT NOT NULL,
  "original_title" TEXT NOT NULL,
  "genres" TEXT NOT NULL CHECK(json_valid("genres")),
  "description" TEXT NOT NULL,
  "tagline" TEXT NOT NULL,
  "year" INTEGER NOT NULL,
  "countries" TEXT NOT NULL CHECK(json_valid("countries")),
  "country_codes" TEXT NOT NULL CHECK(json_valid("country_codes")),
  "original_language" TEXT NOT NULL,
  "spoken_languages" TEXT NOT NULL CHECK(json_valid("spoken_languages")),
  "duration_min" INTEGER NOT NULL,
  "age_rating" TEXT NOT NULL,
  "actors" TEXT NOT NULL CHECK(json_valid("actors")),
  "directors" TEXT NOT NULL CHECK(json_valid("directors")),
  "collection_id" INTEGER,
  "collection_name" TEXT,
  "tmdb_rating" REAL NOT NULL,
  "tmdb_vote_count" INTEGER NOT NULL,
  "imdb_rating" REAL NOT NULL,
  "imdb_vote_count" INTEGER NOT NULL,
  "kinopoisk_rating" REAL NOT NULL,
  "kinopoisk_vote_count" INTEGER NOT NULL,
  "catalog_view_count" INTEGER NOT NULL,
  "has_video_source" INTEGER NOT NULL CHECK("has_video_source" IN (0,1)),
  "is_hidden" INTEGER NOT NULL CHECK("is_hidden" IN (0,1)),
  PRIMARY KEY ("movie_id")
) STRICT;

CREATE TABLE "episodes" (
  "episode_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "season" INTEGER NOT NULL,
  "episode" INTEGER NOT NULL,
  "title" TEXT NOT NULL,
  "duration_seconds" INTEGER NOT NULL,
  "source_file_id" INTEGER,
  PRIMARY KEY ("episode_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "tags" (
  "tag_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "tag" TEXT NOT NULL,
  "created_at" INTEGER NOT NULL,
  "source_movie_id" INTEGER NOT NULL,
  PRIMARY KEY ("tag_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("source_movie_id") REFERENCES "movie_mapping" ("source_movie_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "movie_mapping" (
  "source_movie_id" INTEGER NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "match_source" TEXT NOT NULL,
  PRIMARY KEY ("source_movie_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id")
) STRICT;

CREATE TABLE "room_members" (
  "room_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "session_id" TEXT NOT NULL,
  "is_host" INTEGER NOT NULL CHECK("is_host" IN (0,1)),
  "joined_at_ms" INTEGER NOT NULL,
  "left_at_ms" INTEGER NOT NULL,
  PRIMARY KEY ("room_id", "user_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("room_id") REFERENCES "rooms" ("room_id"),
  FOREIGN KEY ("session_id") REFERENCES "watch_sessions" ("session_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "rooms" (
  "room_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "episode_id" TEXT,
  "host_user_id" TEXT NOT NULL,
  "title" TEXT NOT NULL,
  "created_at_ms" INTEGER NOT NULL,
  "ended_at_ms" INTEGER NOT NULL,
  PRIMARY KEY ("room_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("episode_id") REFERENCES "episodes" ("episode_id"),
  FOREIGN KEY ("host_user_id") REFERENCES "users" ("user_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "clips" (
  "clip_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "episode_id" TEXT,
  "session_id" TEXT NOT NULL,
  "title" TEXT NOT NULL,
  "start_seconds" REAL NOT NULL,
  "end_seconds" REAL NOT NULL,
  "format" TEXT NOT NULL,
  "pinned" INTEGER NOT NULL CHECK("pinned" IN (0,1)),
  "created_at_ms" INTEGER NOT NULL,
  "updated_at_ms" INTEGER NOT NULL,
  PRIMARY KEY ("clip_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("episode_id") REFERENCES "episodes" ("episode_id"),
  FOREIGN KEY ("session_id") REFERENCES "watch_sessions" ("session_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "reviews" (
  "review_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "rating_id" TEXT NOT NULL,
  "score" INTEGER NOT NULL CHECK("score" BETWEEN 1 AND 10),
  "text" TEXT NOT NULL,
  "created_at" INTEGER NOT NULL,
  PRIMARY KEY ("review_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("rating_id") REFERENCES "ratings" ("rating_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "episode_progress" (
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "episode_id" TEXT NOT NULL,
  "position" REAL NOT NULL,
  "duration" REAL NOT NULL,
  "watched_seconds" REAL NOT NULL,
  "completed" INTEGER NOT NULL CHECK("completed" IN (0,1)),
  "updated_at_ms" INTEGER NOT NULL,
  PRIMARY KEY ("user_id", "movie_id", "episode_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("episode_id") REFERENCES "episodes" ("episode_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "progress" (
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "episode_id" TEXT,
  "position" REAL NOT NULL,
  "duration" REAL NOT NULL,
  "watched_seconds" REAL NOT NULL,
  "completed" INTEGER NOT NULL CHECK("completed" IN (0,1)),
  "episodes_completed" INTEGER NOT NULL,
  "episode_count" INTEGER NOT NULL,
  "updated_at_ms" INTEGER NOT NULL,
  PRIMARY KEY ("user_id", "movie_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("episode_id") REFERENCES "episodes" ("episode_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "watched" (
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "marked_at_ms" INTEGER NOT NULL,
  "reason" TEXT NOT NULL,
  PRIMARY KEY ("user_id", "movie_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "not_interested" (
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "is_active" INTEGER NOT NULL CHECK("is_active" IN (0,1)),
  "reason" TEXT NOT NULL,
  "updated_at_ms" INTEGER NOT NULL,
  PRIMARY KEY ("user_id", "movie_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "watchlist" (
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "added_at_ms" INTEGER NOT NULL,
  PRIMARY KEY ("user_id", "movie_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "bookmark_events" (
  "bookmark_event_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "action" TEXT NOT NULL,
  "event_at_ms" INTEGER NOT NULL,
  "collection" TEXT NOT NULL,
  PRIMARY KEY ("bookmark_event_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "watch_sessions" (
  "session_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "episode_id" TEXT,
  "room_id" TEXT,
  "request_id" TEXT,
  "started_at_ms" INTEGER NOT NULL,
  "ended_at_ms" INTEGER NOT NULL,
  "duration_seconds" INTEGER NOT NULL,
  "start_position" REAL NOT NULL,
  "end_position" REAL NOT NULL,
  "watched_seconds" REAL NOT NULL,
  "paused_seconds" INTEGER NOT NULL,
  "buffered_seconds" INTEGER NOT NULL,
  "completed" INTEGER NOT NULL CHECK("completed" IN (0,1)),
  "start_kind" TEXT NOT NULL,
  "view_number" INTEGER NOT NULL,
  "stop_reason" TEXT NOT NULL,
  PRIMARY KEY ("session_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("episode_id") REFERENCES "episodes" ("episode_id"),
  FOREIGN KEY ("room_id") REFERENCES "rooms" ("room_id"),
  FOREIGN KEY ("request_id") REFERENCES "recommendation_requests" ("request_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "searches" (
  "search_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "query" TEXT NOT NULL,
  "query_type" TEXT NOT NULL,
  "searched_at_ms" INTEGER NOT NULL,
  "selected_movie_id" INTEGER NOT NULL,
  PRIMARY KEY ("search_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("selected_movie_id") REFERENCES "movies" ("movie_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "clicks" (
  "click_id" TEXT NOT NULL,
  "impression_id" TEXT NOT NULL,
  "request_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "clicked_at_ms" INTEGER NOT NULL,
  PRIMARY KEY ("click_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("request_id") REFERENCES "recommendation_requests" ("request_id"),
  FOREIGN KEY ("impression_id") REFERENCES "impressions" ("impression_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "impressions" (
  "impression_id" TEXT NOT NULL,
  "request_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER NOT NULL,
  "rank" INTEGER NOT NULL,
  "shown_at_ms" INTEGER NOT NULL,
  "clicked" INTEGER NOT NULL CHECK("clicked" IN (0,1)),
  PRIMARY KEY ("impression_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("request_id") REFERENCES "recommendation_requests" ("request_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "recommendation_requests" (
  "request_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "shown_at_ms" INTEGER NOT NULL,
  "surface" TEXT NOT NULL,
  "policy" TEXT NOT NULL,
  PRIMARY KEY ("request_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "events" (
  "event_id" TEXT NOT NULL,
  "user_id" TEXT NOT NULL,
  "movie_id" INTEGER,
  "event_type" TEXT NOT NULL,
  "event_at_ms" INTEGER NOT NULL,
  "session_id" TEXT,
  "request_id" TEXT,
  "payload" TEXT NOT NULL CHECK(json_valid("payload")),
  PRIMARY KEY ("event_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id"),
  FOREIGN KEY ("session_id") REFERENCES "watch_sessions" ("session_id"),
  FOREIGN KEY ("request_id") REFERENCES "recommendation_requests" ("request_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "users" (
  "user_id" TEXT NOT NULL,
  "source_user_id" INTEGER NOT NULL,
  "display_name" TEXT NOT NULL,
  "email" TEXT NOT NULL,
  "registered_at" INTEGER NOT NULL,
  "locale" TEXT NOT NULL,
  "timezone" TEXT NOT NULL,
  "rating_count" INTEGER NOT NULL,
  "preferred_genres" TEXT NOT NULL CHECK(json_valid("preferred_genres")),
  "preferred_content_types" TEXT NOT NULL CHECK(json_valid("preferred_content_types")),
  "preferred_languages" TEXT NOT NULL CHECK(json_valid("preferred_languages")),
  "preferred_duration_min" TEXT NOT NULL CHECK(json_valid("preferred_duration_min")),
  "preferred_actor_ids" TEXT NOT NULL CHECK(json_valid("preferred_actor_ids")),
  "preferred_director_ids" TEXT NOT NULL CHECK(json_valid("preferred_director_ids")),
  PRIMARY KEY ("user_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "user_activity" (
  "user_id" TEXT NOT NULL,
  "first_activity_at" INTEGER NOT NULL,
  "last_rating_at" INTEGER NOT NULL,
  "last_event_at_ms" INTEGER NOT NULL,
  "days_since_last_event" REAL NOT NULL,
  "rating_count" INTEGER NOT NULL,
  "event_count" INTEGER NOT NULL,
  "sessions" INTEGER NOT NULL,
  "completed_sessions" INTEGER NOT NULL,
  "watched_seconds" REAL NOT NULL,
  PRIMARY KEY ("user_id"),
  FOREIGN KEY ("user_id") REFERENCES "users" ("user_id")
) WITHOUT ROWID, STRICT;

CREATE TABLE "movie_stats" (
  "movie_id" INTEGER NOT NULL,
  "rating_count" INTEGER NOT NULL,
  "rating_mean" REAL,
  "impressions" INTEGER NOT NULL,
  "clicks" INTEGER NOT NULL,
  "sessions" INTEGER NOT NULL,
  "completed_sessions" INTEGER NOT NULL,
  "total_watched_seconds" REAL NOT NULL,
  "active_watchlist_users" INTEGER NOT NULL,
  "active_not_interested_users" INTEGER NOT NULL,
  PRIMARY KEY ("movie_id"),
  FOREIGN KEY ("movie_id") REFERENCES "movies" ("movie_id")
) STRICT;

CREATE UNIQUE INDEX "ux_ratings_user_id_movie_id" ON "ratings" ("user_id", "movie_id");

CREATE UNIQUE INDEX "ux_movies_imdb_id" ON "movies" ("imdb_id");

CREATE UNIQUE INDEX "ux_movies_catalog_kind_tmdb_id" ON "movies" ("catalog_kind", "tmdb_id");

CREATE UNIQUE INDEX "ux_episodes_movie_id_season_episode" ON "episodes" ("movie_id", "season", "episode");

CREATE UNIQUE INDEX "ux_room_members_session_id" ON "room_members" ("session_id");

CREATE UNIQUE INDEX "ux_reviews_rating_id" ON "reviews" ("rating_id");

CREATE UNIQUE INDEX "ux_reviews_user_id_movie_id" ON "reviews" ("user_id", "movie_id");

CREATE UNIQUE INDEX "ux_clicks_impression_id" ON "clicks" ("impression_id");

CREATE UNIQUE INDEX "ux_impressions_request_id_rank" ON "impressions" ("request_id", "rank");

CREATE UNIQUE INDEX "ux_impressions_request_id_movie_id" ON "impressions" ("request_id", "movie_id");

CREATE UNIQUE INDEX "ux_users_source_user_id" ON "users" ("source_user_id");

CREATE INDEX "idx_ratings_movie_id_score" ON "ratings" ("movie_id", "score");

CREATE INDEX "idx_events_user_id_event_at_ms" ON "events" ("user_id", "event_at_ms");

CREATE INDEX "idx_events_movie_id_event_at_ms" ON "events" ("movie_id", "event_at_ms");

CREATE INDEX "idx_events_event_type_event_at_ms" ON "events" ("event_type", "event_at_ms");

CREATE INDEX "idx_watch_sessions_user_id_movie_id_started_at_ms" ON "watch_sessions" ("user_id", "movie_id", "started_at_ms");

CREATE INDEX "idx_bookmark_events_user_id_movie_id_event_at_ms" ON "bookmark_events" ("user_id", "movie_id", "event_at_ms");

CREATE INDEX "idx_impressions_user_id_shown_at_ms" ON "impressions" ("user_id", "shown_at_ms");

CREATE INDEX "idx_clicks_user_id_clicked_at_ms" ON "clicks" ("user_id", "clicked_at_ms");

CREATE INDEX "idx_searches_user_id_searched_at_ms" ON "searches" ("user_id", "searched_at_ms");

CREATE INDEX "idx_movie_mapping_movie_id" ON "movie_mapping" ("movie_id");

CREATE INDEX "idx_tags_user_id" ON "tags" ("user_id");

CREATE INDEX "idx_room_members_user_id" ON "room_members" ("user_id");

CREATE INDEX "idx_clips_user_id" ON "clips" ("user_id");

CREATE INDEX "idx_tags_movie_id" ON "tags" ("movie_id");

CREATE INDEX "idx_reviews_movie_id" ON "reviews" ("movie_id");

CREATE INDEX "idx_watched_movie_id" ON "watched" ("movie_id");

CREATE INDEX "idx_watchlist_movie_id" ON "watchlist" ("movie_id");

CREATE INDEX "idx_not_interested_movie_id" ON "not_interested" ("movie_id");

CREATE INDEX "idx_bookmark_events_movie_id" ON "bookmark_events" ("movie_id");

CREATE INDEX "idx_impressions_movie_id" ON "impressions" ("movie_id");

CREATE INDEX "idx_clicks_movie_id" ON "clicks" ("movie_id");

CREATE INDEX "idx_watch_sessions_movie_id" ON "watch_sessions" ("movie_id");

CREATE INDEX "idx_progress_movie_id" ON "progress" ("movie_id");

CREATE INDEX "idx_episode_progress_movie_id" ON "episode_progress" ("movie_id");

CREATE INDEX "idx_clips_movie_id" ON "clips" ("movie_id");

CREATE INDEX "idx_rooms_movie_id" ON "rooms" ("movie_id");

CREATE INDEX "idx_ratings_source_movie_id" ON "ratings" ("source_movie_id") WHERE "source_movie_id" IS NOT NULL;

CREATE INDEX "idx_tags_source_movie_id" ON "tags" ("source_movie_id");

CREATE INDEX "idx_recommendation_requests_user_id" ON "recommendation_requests" ("user_id");

CREATE INDEX "idx_clicks_request_id" ON "clicks" ("request_id");

CREATE INDEX "idx_searches_selected_movie_id" ON "searches" ("selected_movie_id");

CREATE INDEX "idx_watch_sessions_episode_id" ON "watch_sessions" ("episode_id") WHERE "episode_id" IS NOT NULL;

CREATE INDEX "idx_progress_episode_id" ON "progress" ("episode_id") WHERE "episode_id" IS NOT NULL;

CREATE INDEX "idx_episode_progress_episode_id" ON "episode_progress" ("episode_id");

CREATE INDEX "idx_rooms_episode_id" ON "rooms" ("episode_id") WHERE "episode_id" IS NOT NULL;

CREATE INDEX "idx_clips_episode_id" ON "clips" ("episode_id") WHERE "episode_id" IS NOT NULL;

CREATE INDEX "idx_watch_sessions_room_id" ON "watch_sessions" ("room_id") WHERE "room_id" IS NOT NULL;

CREATE INDEX "idx_watch_sessions_request_id" ON "watch_sessions" ("request_id") WHERE "request_id" IS NOT NULL;

CREATE INDEX "idx_rooms_host_user_id" ON "rooms" ("host_user_id");

CREATE INDEX "idx_clips_session_id" ON "clips" ("session_id");

CREATE INDEX "idx_events_session_id" ON "events" ("session_id") WHERE "session_id" IS NOT NULL;

CREATE INDEX "idx_events_request_id" ON "events" ("request_id") WHERE "request_id" IS NOT NULL;
