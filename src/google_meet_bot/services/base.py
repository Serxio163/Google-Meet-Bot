from abc import ABC, abstractmethod
from typing import Optional

class VideoConferenceProvider(ABC):
    @abstractmethod
    def login(self, email: Optional[str] = None, password: Optional[str] = None) -> None:
        """Авторизация в ВКС/учётной записи (или пропуск при гостевом входе)."""
        pass

    @abstractmethod
    def pre_join_setup(self, meeting_link: str) -> None:
        """Переход по ссылке и подготовка: выключить микрофон/камеру, дать разрешения браузера."""
        pass

    @abstractmethod
    def join(self, meeting_link: str) -> None:
        """Непосредственно присоединиться к встрече (нажатие кнопки Join/Ask to join)."""
        pass

    @abstractmethod
    def wait_until_joined(self, timeout_sec: int = 60) -> bool:
        """Дождаться факта присоединения/адмита, вернуть True/False."""
        pass

    @abstractmethod
    def leave(self) -> None:
        """Покинуть встречу (если требуется)."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Закрыть браузер и освободить ресурсы."""
        pass