"""Конфигурация приложения из переменных окружения / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # БД
    database_url: str = "sqlite+aiosqlite:///./data/nvrmon.db"

    # Часовой пояс для показа времени в панели (IANA, напр. Asia/Vladivostok)
    timezone: str = "UTC"

    # Шифрование
    secret_key: str = ""
    # Мастер-ключ модуля «Заселения» (приоритетнее secret_key). Если пуст — берётся
    # secret_key, иначе автогенерация с сохранением в data/secret.key.
    nvr_secret_key: str = ""

    # Планировщик
    poll_interval_minutes: int = 5
    archive_check_hour: int = 9
    archive_check_minute: int = 0
    max_concurrent_polls: int = 10
    nvr_unreachable_threshold: int = 3
    nvr_auth_error_threshold: int = 3       # после скольких подряд 401 → алерт авторизации
    camera_offline_alert_minutes: int = 10
    time_drift_alert_minutes: int = 5
    archive_gap_alert_minutes: int = 60
    hdd_usage_alert_percent: int = 0

    # Свежий архив: отдельная проверка без уведомлений Telegram.
    recording_check_minutes: int = Field(default=5, ge=0, le=60)
    recording_max_age_minutes: int = Field(default=15, ge=1, le=1440)
    recording_window_minutes: int = Field(default=45, ge=1, le=1440)

    # Коммутаторы: SNMP только для чтения, с ограничением нагрузки.
    switch_poll_seconds: int = Field(default=60, ge=10, le=3600)
    switch_poll_timeout_seconds: int = Field(default=30, ge=5, le=120)
    switch_max_concurrent_polls: int = Field(default=4, ge=1, le=20)

    # Контроль качества картинки (компьютерное зрение)
    quality_check_minutes: int = 0          # период проверки, 0 = выключено
    quality_dark_threshold: float = 30.0    # средняя яркость ниже → тёмный/чёрный кадр
    quality_uniform_threshold: float = 8.0  # контраст (ст.откл.) ниже → однотонный (залеплен)
    quality_uniform_min_brightness: float = 50.0  # «залеплен» только если кадр светлее (иначе это просто темнота)
    quality_blur_threshold: float = 15.0    # резкость (дисперсия лапласиана) ниже → расфокус
    quality_frozen_diff: float = 1.0        # среднее отличие кадров меньше → кадр идентичен
    quality_frozen_changed_frac: float = 0.01  # доля изменившихся пикселей меньше → кадр идентичен (отсекает шум живой статичной сцены)
    quality_frozen_checks: int = 3          # сколько идентичных проверок ПОДРЯД = фриз

    # Здоровье железа NVR
    temp_alert_celsius: float = 65.0        # температура выше → алерт
    cpu_alert_percent: float = 95.0         # загрузка CPU выше → алерт

    # Модуль «Автобусы» (ручной учёт дисков)
    bus_swap_alert_days: int = 14           # диск стоит дольше → пора менять
    bus_review_alert_days: int = 7          # диск висит на просмотре дольше → забыли

    # Сеть
    default_http_timeout: float = 15.0
    default_http_retries: int = 2

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = True
    telegram_bot_enabled: bool = True  # двусторонний бот (команды/кнопки)
    # Прокси для доступа к Telegram, если сервер не пускают напрямую (DPI/блокировка).
    # Примеры: http://user:pass@host:3128  |  socks5://host:1080
    telegram_proxy: str = ""

    # Watchdog (внешний «пульс»: healthchecks.io и т.п.)
    watchdog_url: str = ""
    watchdog_interval_minutes: int = 1

    # Владелец панели (главный администратор). Вход — только по учёткам из БД,
    # режима «открытой панели» больше нет.
    owner_username: str = "IOO"   # логин владельца (бутстрап/промоут)
    owner_password: str = ""      # ТОЛЬКО для первичного создания/аварийного сброса владельца

    # Модуль «Заселения» (ingestion субпотока)
    clips_dir: str = "clips"          # корень для скачанных клипов (в Docker монтируется /clips)
    ffmpeg_bin: str = "ffmpeg"        # бинарь ffmpeg (в образе ставится apt-ом)
    checkin_segment_padding_sec: int = 10   # добор до/после найденной активности
    checkin_merge_gap_sec: int = 60         # склеивать соседние сегменты ближе этого
    checkin_max_segments: int = 300         # предохранитель: клипов на канал за прогон
    checkin_max_clip_mb: int = 8000         # предохранитель на размер одного сегмента (МБ)
    checkin_merge_day: bool = True          # склеивать сегменты дня в один клип на канал

    # ── Интеграция 1С:Отель (подмодуль «Заселения») ──
    # Пояс камер (общий для всех ГС); время заселений храним наивным локальным (§6.2)
    camera_tz: str = "Asia/Khabarovsk"
    onec_enabled: bool = False          # общий рубильник; подключения настраиваются на гостиницу в UI
    # Дефолты для новых подключений (реальная схема Document_Accommodation)
    onec_default_entity: str = "Document_Accommodation"
    onec_default_field_ref: str = "Ref_Key"
    onec_default_field_date: str = "CheckInDate"
    onec_default_field_date_fallback: str = "Date"
    onec_default_field_query_date: str = "Date"  # $filter/$orderby (отбираемое поле)
    onec_default_field_guest: str = "GuestFullName"
    onec_default_field_arrival: str = ""
    onec_default_filter_posted: bool = True
    onec_default_expand_room: str = "Room"
    onec_default_field_room_ref: str = "Room_Key"
    onec_default_field_room_number: str = "Description"
    onec_default_field_room_floor: str = "Floor"
    onec_default_lookback_days: int = 5
    onec_default_tz: str = "Asia/Khabarovsk"
    onec_default_mask_guest: bool = True
    # Синхронизация меток и часов (общее)
    onec_sync_minutes: int = 20
    onec_http_timeout: float = 180.0    # в 1С большой $skip медленный — таймаут щедрый
    onec_pre_roll_sec: int = 300        # гость появляется на камере ДО проводки документа
    onec_post_roll_sec: int = 120
    onec_marker_margin_sec: int = 120
    onec_clock_warn_sec: int = 120      # |смещение часов регистратора| больше → предупреждение

    # Прочее
    log_level: str = "INFO"
    mock_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
