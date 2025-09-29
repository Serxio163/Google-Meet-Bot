import argparse
import os
import tempfile
import logging
import time

from .speech_to_text import SpeechToText
from .record_audio import AudioRecorder
from .services.google_meet import GoogleMeetProvider
from .services.yandex import YandexProvider


def main():
    # Логирование будет настроено ПОСЛЕ парсинга аргументов
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    parser = argparse.ArgumentParser(description="Join a video conference, record audio, and summarize it.")
    parser.add_argument("--provider", dest="provider", default=os.getenv("PROVIDER", "meet"), help="Video conference provider: meet, zoom, yandex")
    parser.add_argument("--meet-link", dest="meet_link", default=os.getenv("MEET_LINK"), help="Meeting link")
    parser.add_argument("--duration", dest="duration", type=int, default=int(os.getenv("RECORDING_DURATION", 60)), help="Recording duration in seconds")
    parser.add_argument("--no-analysis", dest="no_analysis", action="store_true", help="Skip analysis phase")
    # New flags
    parser.add_argument("--until-leave", dest="until_leave", action="store_true", help="Record until the meeting explicitly ends; ignores --duration")
    parser.add_argument("--record-source", dest="record_source", choices=["mic", "system"], default=os.getenv("RECORD_SOURCE", "mic"), help="Audio source: microphone (mic) or system sound (Windows loopback)")
    parser.add_argument("--join-timeout", dest="join_timeout", type=int, default=int(os.getenv("JOIN_TIMEOUT", 60)), help="Timeout (seconds) to wait for meeting join confirmation")
    # Новый флаг уровня логирования
    parser.add_argument("--log-level", dest="log_level", choices=["DEBUG","INFO","WARNING","ERROR"], default=os.getenv("LOG_LEVEL","INFO"), help="Logging level for console output")
    parser.add_argument("--guest-mode", dest="guest_mode", action="store_true", help="Force guest mode (skip authorization even if credentials are available)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, (args.log_level or "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not args.meet_link:
        raise SystemExit("--meet-link (or MEET_LINK env) is required")

    temp_dir = tempfile.mkdtemp()
    audio_path = os.path.join(temp_dir, "output.wav")

    # Выбор провайдера
    provider_key = (args.provider or "meet").lower()
    if provider_key == "meet":
        provider = GoogleMeetProvider()
    elif provider_key in ("yandex", "telemost", "yandex_telemost"):
        provider = YandexProvider()
    else:
        raise SystemExit(f"Unsupported provider: {provider_key}. Supported: meet, yandex")

    recorder = None
    try:
        # Pass guest mode flag to skip authorization if requested
        if hasattr(provider, 'login'):
            if args.guest_mode:
                provider.login(email=None, password=None)  # Force guest mode
            else:
                provider.login()  # Normal login with credentials if available
        provider.pre_join_setup(args.meet_link)
        provider.join(args.meet_link)

        # Ждём подтверждения входа в встречу
        joined = provider.wait_until_joined(timeout_sec=args.join_timeout)
        if not joined:
            logging.warning("Skip recording: meeting was not joined (provider=%s)", provider_key)
            return

        # Начинаем запись только после успешного подключения
        recorder = AudioRecorder()
        recorder.start_recording(audio_path, source=args.record_source)
        if args.until_leave:
            logging.info("Recording started and will continue until the meeting ends (provider=%s)", provider_key)
            # Block until provider detects meeting end/leave
            try:
                # Prefer provider-specific wait if implemented
                if hasattr(provider, "wait_until_left"):
                    provider.wait_until_left(check_interval_sec=5, max_wait_sec=None)
                else:
                    # Fallback: run indefinitely for the specified duration
                    logging.warning("Provider has no wait_until_left(); falling back to --duration sleep")
                    time.sleep(args.duration)
            finally:
                try:
                    provider.leave()
                finally:
                    try:
                        if recorder is not None:
                            recorder.stop_recording()
                    except Exception:
                        pass
                    try:
                        provider.close()
                    except Exception:
                        pass
            logging.info("Recording stopped, file saved: %s", audio_path)
        else:
            logging.info("Recording started and will stop on meeting leave (~%ss)", args.duration)
            time.sleep(args.duration)
            try:
                provider.leave()
            finally:
                try:
                    if recorder is not None:
                        recorder.stop_recording()
                except Exception:
                    pass
                try:
                    provider.close()
                except Exception:
                    pass
            logging.info("Recording stopped, file saved: %s", audio_path)

        # Аналитика после записи
        if not args.no_analysis:
            SpeechToText().transcribe(audio_path)
    finally:
        try:
            provider.close()
        except Exception:
            pass



if __name__ == "__main__":
    main()


