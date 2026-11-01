from dataclasses import dataclass, field
from typing import List, Dict
import time

@dataclass
class PlaybackState:
    """Состояние плеера в комнате совместного просмотра."""
    is_playing: bool = False
    current_time_sec: float = 0.0
    last_update_epoch: float = field(default_factory=time.time)

@dataclass
class ChatMessage:
    """Сообщение в текстовом чате комнаты."""
    sender: str
    text: str
    timestamp: str

@dataclass
class MediaItem:
    """Карточка медиаконтента в каталоге."""
    id: int
    title: str
    description: str
    genres: List[str]
    year: int
    country: str
    rating: float
    duration_min: int
    age_rating: str
    is_hidden: bool = False
    kind: str = 'movie'          # 'movie' | 'series' (episodes live in films.episodes)

@dataclass
class Review:
    """Отзыв и оценка пользователя."""
    id: int
    user_name: str
    media_id: int
    score: int
    comment: str
    created_at: str

@dataclass
class PaymentRecord:
    """Запись истории имитированного платежа."""
    id: int
    tariff_name: str
    amount: float
    status: str
    created_at: str

@dataclass
class User:
    """Учетная запись пользователя."""
    id: int
    email: str
    username: str
    role: str
    is_subscribed: bool = False

class WatchRoom:
    """Сессия совместного просмотра контента."""
    def __init__(self, room_code: str, host_user: User, media: MediaItem):
        self.room_code = room_code
        self.host_id = host_user.id
        self.host_name = host_user.username
        self.media = media
        self.state = PlaybackState()
        self.chat_history: List[ChatMessage] = []
        self.members: List[str] = [host_user.username]

    def update_playback(self, is_playing: bool, time_sec: float):
        self.state.is_playing = is_playing
        self.state.current_time_sec = max(0.0, round(float(time_sec), 1))
        self.state.last_update_epoch = time.time()

    def add_message(self, sender: str, text: str):
        # Санитизация входных данных от внедрения сценариев (п. 4.1.1 ТЗ)
        clean_text = text.replace("<", "&lt;").replace(">", "&gt;").strip()[:1000]
        if clean_text:
            ts = time.strftime("%H:%M:%S")
            self.chat_history.append(ChatMessage(sender=sender, text=clean_text, timestamp=ts))