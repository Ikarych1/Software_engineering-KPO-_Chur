from typing import List, Dict
import time
from models import MediaItem, User, WatchRoom
import storage

class RecommendationService:
    """Подсистема персонализированных рекомендаций (п. 4.1.1 и 4.8 ТЗ)."""

    @staticmethod
    def _filter_available(catalog: List[MediaItem], watched_ids: List[int]) -> List[MediaItem]:
        """Исключение скрытых администратором и уже просмотренных фильмов."""
        return [m for m in catalog if not m.is_hidden and m.id not in watched_ids]

    @classmethod
    def get_recommendations(cls, user_id: int, limit: int = 4) -> List[Dict]:
        user_ratings = storage.USER_RATINGS.get(user_id, {})
        watched = storage.USER_WATCHED.get(user_id, [])
        available = cls._filter_available(storage.CATALOG, watched)

        # Вариант 1: Холодный старт (нет истории или оценок)
        if not user_ratings:
            top_rated = sorted(available, key=lambda x: x.rating, reverse=True)[:limit]
            return [{"item": m, "reason": "Популярно в онлайн-кинотеатре (Топ рейтинга)"} for m in top_rated]

        # Вариант 2: Персонализированный подбор по жанрам с оценками >= 7
        favorite_genres = set()
        for media_id, score in user_ratings.items():
            if score >= 7:
                item = next((m for m in storage.CATALOG if m.id == media_id), None)
                if item:
                    favorite_genres.update(item.genres)

        ranked_items = []
        for item in available:
            matches = sum(1 for g in item.genres if g in favorite_genres)
            ranked_items.append((matches, item.rating, item))

        # Сортировка: сначала совпадения жанров, затем собственный рейтинг
        ranked_items.sort(key=lambda x: (x[0], x[1]), reverse=True)

        results = []
        for match_cnt, _, item in ranked_items[:limit]:
            if match_cnt > 0:
                reason = f"Потому что вам нравятся жанры: {', '.join(item.genres)}"
            else:
                reason = "Высокая оценка зрителей"
            results.append({"item": item, "reason": reason})

        return results

class RoomService:
    """Сервис управления комнатами совместного просмотра."""

    @staticmethod
    def get_room(room_code: str) -> WatchRoom:
        return storage.ACTIVE_ROOMS.get(room_code)

    @staticmethod
    def create_room(host_user: User, media_id: int) -> WatchRoom:
        media = next((m for m in storage.CATALOG if m.id == media_id), storage.CATALOG[0])
        code = f"ROOM-{int(time.time()) % 10000}"
        new_room = WatchRoom(code, host_user, media)
        new_room.add_message("Система", f"Хост {host_user.username} создал комнату совместного просмотра.")
        storage.ACTIVE_ROOMS[code] = new_room
        return new_room