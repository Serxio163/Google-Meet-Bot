#!/usr/bin/env python3
"""
Сервис для работы только с Yandex SpeechKit API v3 (gRPC streaming).
"""

import asyncio
import logging
from typing import Dict, Any, Optional, AsyncGenerator, List

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

try:
    import grpc
    from grpc import aio
    GRPC_AVAILABLE = True
except ImportError as e:
    logger.warning(f"grpc не установлен: {e}. Стриминг режим будет недоступен.")
    grpc = None
    aio = None
    GRPC_AVAILABLE = False

try:
    # Yandex SpeechKit v3 API protobuf classes
    from yandex.cloud.ai.stt.v3.stt_service_pb2_grpc import RecognizerStub
    from yandex.cloud.ai.stt.v3.stt_pb2 import (
        StreamingRequest,
        StreamingOptions,
        RecognitionModelOptions,
        AudioFormatOptions,
        RawAudio,
        TextNormalizationOptions,
        LanguageRestrictionOptions,
        AudioChunk,
    )
except ImportError as e:
    logger.warning(f"Не удалось импортировать protobuf модули v3 из yandex.cloud: {e}")
    RecognizerStub = None
    StreamingRequest = None
    StreamingOptions = None
    RecognitionModelOptions = None
    AudioFormatOptions = None
    RawAudio = None
    TextNormalizationOptions = None
    LanguageRestrictionOptions = None
    AudioChunk = None


class YandexSpeechKitV3Service:
    """Сервис для работы только с Yandex SpeechKit API v3 (gRPC Streaming)"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()

    def _build_auth_metadata(self) -> List[tuple]:
        """
        Формирует метаданные авторизации для gRPC v3.
        - Api-Key <KEY> или Bearer <IAM_TOKEN>
        - При использовании IAM добавляем x-folder-id (если задан)
        """
        metadata = []
        if getattr(self.settings, "yandex_api_key", None):
            metadata.append(("authorization", f"Api-Key {self.settings.yandex_api_key}"))
        elif getattr(self.settings, "yandex_iam_token", None):
            metadata.append(("authorization", f"Bearer {self.settings.yandex_iam_token}"))
            if getattr(self.settings, "yandex_folder_id", None):
                metadata.append(("x-folder-id", self.settings.yandex_folder_id))
        else:
            raise ValueError("Не настроена авторизация: требуется yandex_api_key или yandex_iam_token")
        return metadata

    async def recognize_stream(
        self,
        audio_stream,
        language: str = "ru-RU",
        enable_diarization: bool = True,
        sample_rate: int = 16000
    ):
        """
        Потоковое распознавание через Recognizer.RecognizeStreaming (v3, gRPC).
        Первый запрос должен быть session_options, затем передаются chunk с данными аудио <mcreference link="https://yandex.cloud/ru/docs/speechkit/stt-v3/api-ref/grpc/Recognizer/recognizeStreaming#speechkit.stt.v3.StreamingRequest" index="0">0</mcreference>.
        Поддерживаются частоты 8000/16000/48000 Hz; используем 16000 Hz и LINEAR16_PCM mono <mcreference link="https://yandex.cloud/ru/docs/speechkit/stt-v3/api-ref/grpc/Recognizer/recognizeStreaming#speechkit.stt.v3.StreamingRequest" index="0">0</mcreference>.
        """
        if not GRPC_AVAILABLE or RecognizerStub is None or StreamingRequest is None:
            yield {
                "type": "error",
                "error": "gRPC/Protobuf not available",
                "details": "Установите grpcio и пакет protobuf для Yandex SpeechKit v3",
                "provider": "yandex_v3",
                "mode": "streaming",
            }
            return

        if not getattr(self.settings, "yandex_folder_id", None) and not getattr(self.settings, "yandex_api_key", None):
            # При авторизации Api-Key folder_id не обязателен; при Bearer — обязателен
            logger.warning("yandex_folder_id не указан. Для Bearer требуется x-folder-id.")
        try:
            metadata = self._build_auth_metadata()
        except Exception as e:
            yield {
                "type": "error",
                "error": "Authorization failed",
                "details": str(e),
                "provider": "yandex_v3",
                "mode": "streaming",
            }
            return

        # Приводим к полному коду языка
        if language == "ru":
            language = "ru-RU"
        if language == "en":
            language = "en-US"

        # Валидация sample_rate для Yandex STT v3
        allowed_rates = {8000, 16000, 48000}
        if sample_rate not in allowed_rates:
            logger.warning(
                f"Unsupported sample_rate={sample_rate} for Yandex STT v3. "
                f"Falling back to 16000. Allowed: {sorted(allowed_rates)}"
            )
            sample_rate = 16000

        # Конфигурация модели и входного аудио
        session_options = StreamingOptions(
            recognition_model=RecognitionModelOptions(
                audio_format=AudioFormatOptions(
                    raw_audio=RawAudio(
                        audio_encoding=RawAudio.LINEAR16_PCM,
                        sample_rate_hertz=sample_rate,  # используем запрошенное значение
                        audio_channel_count=1,
                    )
                ),
                text_normalization=TextNormalizationOptions(
                    text_normalization=TextNormalizationOptions.TEXT_NORMALIZATION_ENABLED,
                    profanity_filter=True,
                    literature_text=False,
                ),
                language_restriction=LanguageRestrictionOptions(
                    restriction_type=LanguageRestrictionOptions.WHITELIST,
                    language_code=[language],
                ),
                audio_processing_type=RecognitionModelOptions.REAL_TIME,
            )
        )

        CHUNK_SIZE = 4096  # рекомендуемый размер чанка
        logger.info("Инициализация gRPC канала к stt.api.cloud.yandex.net:443")
        channel = None
        try:
            channel = aio.secure_channel("stt.api.cloud.yandex.net:443", grpc.ssl_channel_credentials())
            await channel.channel_ready()
            stub = RecognizerStub(channel)

            async def request_generator():
                # 1) Первый запрос — session_options
                yield StreamingRequest(session_options=session_options)

                # 2) Отправляем аудио чанки как есть (входной поток уже разбивает их оптимально)
                sent = 0
                async for chunk in audio_stream:
                    if not chunk:
                        continue
                    sent += 1
                    if sent % 50 == 0:
                        logger.info(f"request_generator: sent {sent} audio chunks")
                    # Подразумевается, что chunk уже PCM16 mono с нужной частотой
                    yield StreamingRequest(chunk=AudioChunk(data=chunk))

                logger.info(f"request_generator: completed after sending {sent} chunks")
                # (end_of_stream не поддерживается в v3 StreamingRequest, генератор завершается сам)

            response_stream = stub.RecognizeStreaming(request_generator(), metadata=metadata)

            # Читаем ответы
            idx = 0
            async for response in response_stream:
                idx += 1
                try:
                    has_partial = bool(getattr(response, "partial", None))
                    has_final = bool(getattr(response, "final", None))
                    has_final_ref = bool(getattr(response, "final_refinement", None))
                    logger.info(
                        f"StreamingResponse #{idx}: partial={has_partial}, final={has_final}, final_refinement={has_final_ref}"
                    )
                except Exception:
                    logger.info(f"StreamingResponse #{idx}: <unable to introspect>")
                async for parsed in self._process_single_response(response, idx):
                    yield parsed

        except grpc.RpcError as err:
            yield {
                "type": "error",
                "error": f"gRPC error: {err.code().name}",
                "details": err.details(),
                "provider": "yandex_v3",
                "mode": "streaming",
            }
        except Exception as e:
            yield {
                "type": "error",
                "error": str(e),
                "provider": "yandex_v3",
                "mode": "streaming",
            }
        finally:
            if channel is not None:
                await channel.close()

    async def _process_single_response(self, response, response_count: int) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Преобразование ответа v3 StreamingResponse в унифицированный словарь.
        Поддержаны partial/final/final_refinement.
        """
        handled = False
        try:
            # partial
            if hasattr(response, "partial") and response.partial and getattr(response.partial, "alternatives", None):
                for alt in response.partial.alternatives:
                    text = getattr(alt, "text", "") or ""
                    if not text.strip():
                        continue
                    handled = True
                    yield {
                        "type": "partial",
                        "text": text,
                        "confidence": getattr(alt, "confidence", 0.0),
                        "words": [
                            {
                                "word": getattr(w, "text", ""),
                                "start": getattr(w, "start_time_ms", 0) / 1000.0 if hasattr(w, "start_time_ms") else 0.0,
                                "end": getattr(w, "end_time_ms", 0) / 1000.0 if hasattr(w, "end_time_ms") else 0.0,
                            }
                            for w in getattr(alt, "words", [])
                        ],
                        "provider": "yandex_v3",
                        "mode": "streaming",
                        "is_final": False,
                    }

            # final
            if hasattr(response, "final") and response.final and getattr(response.final, "alternatives", None):
                for alt in response.final.alternatives:
                    text = getattr(alt, "text", "") or ""
                    if not text.strip():
                        continue
                    handled = True
                    yield {
                        "type": "final",
                        "text": text,
                        "confidence": getattr(alt, "confidence", 0.0),
                        "words": [
                            {
                                "word": getattr(w, "text", ""),
                                "start": getattr(w, "start_time_ms", 0) / 1000.0 if hasattr(w, "start_time_ms") else 0.0,
                                "end": getattr(w, "end_time_ms", 0) / 1000.0 if hasattr(w, "end_time_ms") else 0.0,
                            }
                            for w in getattr(alt, "words", [])
                        ],
                        "provider": "yandex_v3",
                        "mode": "streaming",
                        "is_final": True,
                    }

            # final_refinement.normalized_text.alternatives
            if (
                hasattr(response, "final_refinement")
                and response.final_refinement
                and getattr(response.final_refinement, "normalized_text", None)
                and getattr(response.final_refinement.normalized_text, "alternatives", None)
            ):
                for alt in response.final_refinement.normalized_text.alternatives:
                    text = getattr(alt, "text", "") or ""
                    if not text.strip():
                        continue
                    handled = True
                    yield {
                        "type": "final_refinement",
                        "text": text,
                        "confidence": getattr(alt, "confidence", 0.0),
                        "words": [],
                        "provider": "yandex_v3",
                        "mode": "streaming",
                        "is_final": True,
                    }

            if not handled:
                # Логируем непарсируемые ответы для диагностики
                try:
                    logger.debug(f"_process_single_response: unhandled response #{response_count}: {response}")
                except Exception:
                    logger.debug(f"_process_single_response: unhandled response #{response_count}: <repr failed>")

        except Exception as e:
            logger.error(f"_process_single_response error: {e}", exc_info=True)
            yield {
                "type": "error",
                "error": f"response parse error: {e}",
                "provider": "yandex_v3",
                "mode": "streaming",
            }

async def _convert_audio_to_pcm_linear16(audio_data: bytes, target_sample_rate: int) -> bytes:
    """
    Преобразование входного аудио к LINEAR16 PCM mono с частотой target_sample_rate.
    Текущая реализация — pass-through, предполагает, что апстрим уже отдал PCM16 mono
    с частотой, совпадающей с target_sample_rate.
    """
    allowed_rates = {8000, 16000, 48000}
    if target_sample_rate not in allowed_rates:
        logger.warning(
            f"Unsupported sample_rate={target_sample_rate} for Yandex STT v3. "
            f"Falling back to 16000. Allowed: {sorted(allowed_rates)}"
        )
        target_sample_rate = 16000

    # Здесь должна быть логика преобразования аудио (например, через resample или проверку формата)
    # Пока просто возвращаем входные данные (pass-through)
    return audio_data