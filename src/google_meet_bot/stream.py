from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any, List
import logging
import json
import asyncio
from datetime import datetime

from ..models.requests import StreamTranscriptionRequest
from ..models.responses import StreamTranscriptionResponse
from ..models.base import StreamChunk
from ..services.stream_service import StreamTranscriptionService
from ..services.pyaudio_service import PyAudioService
from ..config import get_settings, Settings
from .auth import get_api_key # Добавляем импорт get_api_key

router = APIRouter()
logger = logging.getLogger(__name__)

class ConnectionManager:
    """Менеджер WebSocket соединений"""
    
    def __init__(self):
        # Сессия -> { client_id -> WebSocket }
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
        # Сессия -> { client_id -> channel }
        self.client_channels: Dict[str, Dict[str, int]] = {}
        # Сессия -> { chunk_id -> channel }
        self.chunk_channel_map: Dict[str, Dict[str, int]] = {}
        # Метаданные по сессии
        self.transcription_sessions: Dict[str, Dict[str, Any]] = {}
        # Аккумуляция результатов
        self.session_results: Dict[str, List[Dict[str, Any]]] = {}
        # Сервисы транскрибации по сессиям (единый на сессию)
        self.services: Dict[str, StreamTranscriptionService] = {}

    async def connect(self, websocket: WebSocket, session_id: str, channel: Optional[int] = None) -> str:
        await websocket.accept()
        # Инициализируем контейнеры сессии
        if session_id not in self.active_connections:
            self.active_connections[session_id] = {}
            self.client_channels[session_id] = {}
            self.chunk_channel_map[session_id] = {}
        if session_id not in self.transcription_sessions:
            self.transcription_sessions[session_id] = {
                "created_at": datetime.now(),
                "status": "connected",
                "chunks_processed": 0
            }
        if session_id not in self.session_results:
            self.session_results[session_id] = []
        
        # Генерируем client_id
        next_idx = len(self.active_connections[session_id]) + 1
        client_id = f"c{next_idx}"
        
        # Назначаем канал: если не указан, выберем минимально свободный начиная с 1
        if channel is None:
            used = set(self.client_channels[session_id].values())
            ch = 1
            while ch in used:
                ch += 1
            channel = ch
        
        # Регистрируем клиента
        self.active_connections[session_id][client_id] = websocket
        self.client_channels[session_id][client_id] = int(channel)
        
        logger.info(f"WebSocket connection established for session: {session_id}, client_id: {client_id}, channel: {channel}")
        return client_id

    def disconnect(self, session_id: str, client_id: Optional[str] = None):
        # Отключение конкретного клиента
        if client_id is not None:
            if session_id in self.active_connections:
                self.active_connections[session_id].pop(client_id, None)
            if session_id in self.client_channels:
                self.client_channels[session_id].pop(client_id, None)
            # Если клиентов больше нет — очищаем контейнер клиентов (сервис и результаты не трогаем)
            if session_id in self.active_connections and not self.active_connections[session_id]:
                self.active_connections.pop(session_id, None)
                self.client_channels.pop(session_id, None)
                # Карту чанков оставляем до завершения сессии
            logger.info(f"WebSocket client disconnected for session: {session_id}, client_id: {client_id}")
            return
        
        # Полное отключение сессии (например, при перезапуске записи микрофона)
        if session_id in self.active_connections:
            self.active_connections.pop(session_id, None)
        if session_id in self.client_channels:
            self.client_channels.pop(session_id, None)
        # Не удаляем результаты и сервис, чтобы можно было завершить и сохранить
        logger.info(f"WebSocket connection closed for session: {session_id}")

    def add_transcription_result(self, session_id: str, result: Dict[str, Any]):
        """Добавить результат транскрибации для сессии"""
        if session_id not in self.session_results:
            self.session_results[session_id] = []
        self.session_results[session_id].append(result)

    def get_session_results(self, session_id: str, include_partial: bool = True) -> Optional[List[Dict[str, Any]]]:
        """Получить результаты транскрибации для сессии"""
        if session_id not in self.session_results:
            return None
        results = self.session_results[session_id]
        if not include_partial:
            results = [r for r in results if r.get("is_final", False)]
        return results

    def get_session_transcript(self, session_id: str, format_type: str = "text") -> Optional[str]:
        """Получить полную транскрипцию сессии"""
        results = self.get_session_results(session_id, include_partial=False)
        if not results:
            return None
        if format_type == "text":
            texts = [r["text"] for r in results if r.get("text")]
            return " ".join(texts)
        elif format_type == "json":
            import json as _json
            return _json.dumps(results, default=str, ensure_ascii=False, indent=2)
        return None

    async def send_personal_message(self, message: str, session_id: str, client_id: Optional[str] = None):
        # Если указан client_id — отправляем только ему, иначе широковещательно по сессии
        try:
            if client_id is not None:
                websocket = self.active_connections.get(session_id, {}).get(client_id)
                if websocket:
                    await websocket.send_text(message)
                return
            # broadcast
            for ws in self.active_connections.get(session_id, {}).values():
                try:
                    await ws.send_text(message)
                except Exception as e:
                    logger.warning(f"Broadcast send failed for session {session_id}: {e}")
        except Exception as e:
            logger.error(f"Error sending message to session {session_id}: {e}")

    async def send_transcription_result(self, result: StreamTranscriptionResponse, session_id: str):
        # Отправляем всем клиентам в сессии
        try:
            message = {
                "type": "transcription_result",
                "chunk_id": result.chunk_id,
                "text": result.text,
                "is_final": result.is_final,
                "confidence": result.confidence,
                "speaker_id": result.speaker_id,
                "timestamp": result.timestamp.isoformat() if result.timestamp else None
            }
            payload = json.dumps(message, ensure_ascii=False)
            for ws in self.active_connections.get(session_id, {}).values():
                try:
                    await ws.send_text(payload)
                except Exception as e:
                    logger.warning(f"Broadcast result failed for session {session_id}: {e}")
        except Exception as e:
            logger.error(f"Error preparing transcription result for session {session_id}: {e}")

    # -------- Вспомогательные методы для мультиканальности --------
    def get_client_channel(self, session_id: str, client_id: str) -> Optional[int]:
        return self.client_channels.get(session_id, {}).get(client_id)

    def map_chunk_channel(self, session_id: str, chunk_id: str, channel: int):
        if session_id not in self.chunk_channel_map:
            self.chunk_channel_map[session_id] = {}
        self.chunk_channel_map[session_id][chunk_id] = int(channel)

    def resolve_channel_for_chunk(self, session_id: str, chunk_id: str) -> Optional[int]:
        return self.chunk_channel_map.get(session_id, {}).get(chunk_id)

    def has_active_clients(self, session_id: str) -> bool:
        return bool(self.active_connections.get(session_id))

    def get_service(self, session_id: str) -> Optional[StreamTranscriptionService]:
        return self.services.get(session_id)

    def set_service(self, session_id: str, service: StreamTranscriptionService):
        self.services[session_id] = service

# Инициализируем глобальный менеджер соединений
manager = ConnectionManager()

@router.websocket("/ws/{session_id}")
async def websocket_transcription_endpoint(
    websocket: WebSocket,
    session_id: str
):
    """
    WebSocket эндпоинт для потоковой транскрибации (мультиклиент)
    """
    # Читаем параметры из query string WebSocket URL
    query_params = dict(websocket.query_params)
    provider = query_params.get("provider", "yandex")
    if provider == "yandex_v3":
        provider = "yandex"
    language = query_params.get("language", "ru-RU")
    enable_diarization = query_params.get("enable_diarization", "true")
    enable_diarization = str(enable_diarization).lower() == "true"
    sample_rate_param = query_params.get("sample_rate")
    sample_rate = int(sample_rate_param) if sample_rate_param and sample_rate_param.isdigit() else None
    channel_param = query_params.get("channel")
    channel: Optional[int] = int(channel_param) if channel_param and channel_param.isdigit() else None

    # Подключаем клиента и назначаем канал
    client_id = await manager.connect(websocket, session_id, channel=channel)
    client_channel = manager.get_client_channel(session_id, client_id)
    client_info = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
    logger.info(f"WebSocket connection established for session: {session_id}, client: {client_info}, channel: {client_channel}")
    
    # Флаг, предотвращающий повторный вызов end_session
    session_already_ended = False
    
    # Инициализируем сервис потоковой транскрибации (единый на сессию)
    settings = get_settings()
    stream_service = manager.get_service(session_id)
    if not stream_service:
        stream_service = StreamTranscriptionService(settings)
        manager.set_service(session_id, stream_service)
        # Создаем запрос на потоковую транскрибацию и стартуем сессию
        request = StreamTranscriptionRequest(
            provider=provider,
            language=language,
            enable_diarization=enable_diarization,
            sample_rate=sample_rate if sample_rate is not None else 16000
        )
        session_started = await stream_service.start_session(session_id, request)
        logger.info(f"Session {session_id} started: {session_started}")
    else:
        logger.info(f"Reusing existing StreamTranscriptionService for session {session_id}")
    
    try:
        # Устанавливаем callback для отправки результатов в реальном времени (будет бродкастить всем клиентам)
        async def result_callback(cb_session_id: str, result: Dict[str, Any]):
            try:
                text = result.get("text", "").strip()
                if not text:
                    logger.debug(f"Skipping empty result for session {cb_session_id}: {result.get('chunk_id', 'unknown')}")
                    return
                timestamp = result.get("timestamp")
                if isinstance(timestamp, str):
                    from datetime import datetime as _dt
                    timestamp = _dt.fromisoformat(timestamp.replace('Z', '+00:00'))
                elif timestamp is None:
                    timestamp = datetime.now()
                # Определяем speaker_id: приоритет — от провайдера, затем по каналу
                speaker_id = result.get("speaker_id")
                if not speaker_id:
                    # Попробуем взять из chunk_id формата ch<channel>-<id>
                    ch = None
                    chunk_id_local = result.get("chunk_id") or ""
                    if isinstance(chunk_id_local, str) and chunk_id_local.startswith("ch") and "-" in chunk_id_local:
                        try:
                            ch = int(chunk_id_local.split("-", 1)[0][2:])
                        except Exception:
                            ch = None
                    if ch is None:
                        ch = manager.resolve_channel_for_chunk(cb_session_id, chunk_id_local)
                    if ch is not None:
                        speaker_id = f"channel_{ch}"
                # Сохраняем результат в менеджере (для API)
                manager.add_transcription_result(cb_session_id, {
                    "chunk_id": result.get("chunk_id", ""),
                    "timestamp": timestamp,
                    "text": text,
                    "is_final": result.get("is_final", False),
                    "confidence": result.get("confidence"),
                    "speaker_id": speaker_id,
                    "success": True
                })
                response = StreamTranscriptionResponse(
                    chunk_id=result.get("chunk_id", ""),
                    text=text,
                    is_final=result.get("is_final", False),
                    confidence=result.get("confidence"),
                    speaker_id=speaker_id,
                    success=result.get("success", True),
                    timestamp=timestamp
                )
                await manager.send_transcription_result(response, cb_session_id)
                logger.info(f"Sent transcription result via callback: {text}")
            except Exception as e:
                logger.error(f"Error in result callback: {e}")
        stream_service.set_result_callback(result_callback)
        
        # Отправляем подтверждение подключения текущему клиенту
        await manager.send_personal_message(
            json.dumps({
                "type": "connection_established",
                "session_id": session_id,
                "status": "ready",
                "provider": provider,
                "language": language,
                "enable_diarization": enable_diarization,
                "channel": client_channel,
                "session_started": True
            }),
            session_id,
            client_id=client_id
        )
        
        
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                message_type = message.get("type")
                
                if message_type == "audio_chunk":
                    # Поддерживаем оба варианта имени поля: "audio_data" (текущий) и "audio_bytes" (устаревший)
                    raw_audio_hex = message.get("audio_data") or message.get("audio_bytes")
                    orig_chunk_id = message.get("chunk_id")
                    is_final = message.get("is_final", False)
                    
                    if raw_audio_hex is not None:
                        audio_data_bytes = bytes.fromhex(raw_audio_hex) if isinstance(raw_audio_hex, str) else raw_audio_hex
                        # Префиксуем chunk_id каналом: ch<channel>-<id>
                        generated = f"{session_id}_{manager.transcription_sessions[session_id]['chunks_processed'] + 1}"
                        new_chunk_id = f"ch{client_channel}-{orig_chunk_id or generated}"
                        # Маппим chunk->channel для последующего восстановления спикера
                        manager.map_chunk_channel(session_id, new_chunk_id, client_channel)
                        # Создаём чанк
                        chunk = StreamChunk(
                            chunk_id=new_chunk_id,
                            audio_data=audio_data_bytes,
                            timestamp=datetime.now(),
                            is_final=is_final
                        )
                        # Получаем единый сервис на сессию
                        svc = manager.get_service(session_id)
                        if not svc:
                            raise RuntimeError("Transcription service is not initialized for this session")
                        # Передаём чанк в сервис транскрибации
                        # Восстанавливаем параметры запроса из текущих настроек клиента (они общие для сессии)
                        request = StreamTranscriptionRequest(
                            provider=provider,
                            language=language,
                            enable_diarization=enable_diarization,
                            sample_rate=sample_rate if sample_rate is not None else 16000
                        )
                        await svc.process_audio_chunk(chunk, request, session_id)
                        # Обновляем счетчик обработанных чанков
                        manager.transcription_sessions[session_id]["chunks_processed"] += 1
                        logger.debug(f"Processed chunk {new_chunk_id} for session {session_id}")
                    else:
                        if is_final:
                            new_chunk_id = f"ch{client_channel}-final_{session_id}"
                            manager.map_chunk_channel(session_id, new_chunk_id, client_channel)
                            chunk = StreamChunk(
                                chunk_id=new_chunk_id,
                                audio_data=b"",
                                timestamp=datetime.now(),
                                is_final=True
                            )
                            svc = manager.get_service(session_id)
                            if not svc:
                                raise RuntimeError("Transcription service is not initialized for this session")
                            request = StreamTranscriptionRequest(
                                provider=provider,
                                language=language,
                                enable_diarization=enable_diarization,
                                sample_rate=sample_rate if sample_rate is not None else 16000
                            )
                            await svc.process_audio_chunk(chunk, request, session_id)
                            logger.debug(f"Processed final (empty) chunk for session {session_id}")
                        else:
                            logger.warning(f"Received empty audio_data for session {session_id}, chunk {orig_chunk_id}")
                
                elif message_type == "end_session":
                    # Завершаем общую сессию (для всех клиентов)
                    svc = manager.get_service(session_id)
                    if svc:
                        await svc.end_session(session_id)
                        session_already_ended = True
                    await manager.send_personal_message(
                        json.dumps({
                            "type": "session_ended",
                            "session_id": session_id,
                            "status": "completed",
                            "chunks_processed": manager.transcription_sessions.get(session_id, {}).get("chunks_processed", 0)
                        }),
                        session_id
                    )
                    break
                
                elif message_type == "ping":
                    await manager.send_personal_message(
                        json.dumps({"type": "pong", "timestamp": datetime.now().isoformat()}),
                        session_id,
                        client_id=client_id
                    )
                else:
                    logger.warning(f"Unknown message type: {message_type}")
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON received: {e}")
                await manager.send_personal_message(
                    json.dumps({
                        "type": "error",
                        "message": "Invalid JSON format"
                    }),
                    session_id,
                    client_id=client_id
                )
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                await manager.send_personal_message(
                    json.dumps({
                        "type": "error",
                        "message": str(e)
                    }),
                    session_id,
                    client_id=client_id
                )
    except WebSocketDisconnect as e:
        client_info = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
        logger.info(f"WebSocket disconnected for session: {session_id}, client: {client_info}, code: {getattr(e, 'code', 'unknown')}, reason: {getattr(e, 'reason', 'unknown')}")
    except Exception as e:
        client_info = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
        logger.error(f"WebSocket error for session {session_id}, client: {client_info}: {e}", exc_info=True)
    finally:
        logger.info(f"WebSocket connection closed for session: {session_id}")
        # Завершаем сервисную сессию только если нет других клиентов и не было явного завершения
        try:
            manager.disconnect(session_id, client_id)
            if not session_already_ended and not manager.has_active_clients(session_id):
                svc = manager.get_service(session_id)
                if svc:
                    await svc.end_session(session_id)
                    logger.info(f"Stream service session {session_id} ended successfully (no more clients)")
                else:
                    logger.info(f"No active transcription service for session {session_id}")
            elif session_already_ended:
                logger.info(f"Stream service session {session_id} was already ended earlier")
            else:
                logger.info(f"Session {session_id} still has active clients, keeping service alive")
        except Exception as e:
            logger.error(f"Error ending stream service session {session_id}: {e}")

@router.post("/sessions", dependencies=[Depends(get_api_key)])
async def create_session(
    request: StreamTranscriptionRequest,
    settings: Settings = Depends(get_settings)
):
    """
    Создать новую сессию потоковой транскрибации
    """
    try:
        import uuid
        session_id = str(uuid.uuid4())
        
        # Создаем новую сессию в менеджере
        manager.transcription_sessions[session_id] = {
            "created_at": datetime.now(),
            "status": "created",
            "chunks_processed": 0,
            "provider": request.provider,
            "language": request.language,
            "enable_diarization": request.enable_diarization
        }
        manager.session_results[session_id] = []
        
        logger.info(f"Created new transcription session: {session_id}")
        
        return {
            "success": True,
            "session_id": session_id,
            "status": "created",
            "provider": request.provider,
            "language": request.language,
            "enable_diarization": request.enable_diarization,
            "websocket_url": f"/api/v1/stream/ws/{session_id}"
        }
        
    except Exception as e:
        logger.error(f"Error creating session: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка создания сессии: {str(e)}"
        )



@router.get("/sessions/{session_id}/results", dependencies=[Depends(get_api_key)])
async def get_session_results(session_id: str, include_partial: bool = True):
    """
    Получить результаты транскрибации для сессии
    """
    try:
        results = manager.get_session_results(session_id, include_partial)
        
        if results is None:
            raise HTTPException(
                status_code=404,
                detail=f"Сессия {session_id} не найдена"
            )
        
        return {
            "success": True,
            "session_id": session_id,
            "results_count": len(results),
            "results": results
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session results {session_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка получения результатов сессии: {str(e)}"
        )

@router.get("/sessions/{session_id}/transcript", dependencies=[Depends(get_api_key)])
async def get_session_transcript(session_id: str, format_type: str = "text"):
    """
    Получить полную транскрипцию сессии в указанном формате
    """
    try:
        transcript = manager.get_session_transcript(session_id, format_type)
        
        if transcript is None:
            raise HTTPException(
                status_code=404,
                detail=f"Сессия {session_id} не найдена или не содержит результатов"
            )
        
        return {
            "success": True,
            "session_id": session_id,
            "format": format_type,
            "transcript": transcript
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session transcript {session_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка получения транскрипции сессии: {str(e)}"
        )

@router.get("/sessions/{session_id}", dependencies=[Depends(get_api_key)])
async def get_session_info(session_id: str):
    """
    Получить информацию о сессии
    """
    try:
        session_info = manager.transcription_sessions.get(session_id)
        
        if session_info is None:
            raise HTTPException(
                status_code=404,
                detail=f"Сессия {session_id} не найдена"
            )
        
        # Добавляем информацию о результатах
        results_count = len(manager.session_results.get(session_id, []))
        session_info_with_results = {
            **session_info,
            "results_count": results_count,
            "created_at": session_info["created_at"].isoformat()
        }

        # Добавляем s3_result_url после завершения сессии, если доступен в StreamTranscriptionService
        try:
            s3_url = None
            svc = manager.get_service(session_id)
            if svc:
                svc_info = svc.get_session_info(session_id)
                if svc_info:
                    s3_url = svc_info.get("s3_result_url")
            if s3_url and session_info.get("status") == "completed":
                session_info_with_results["s3_result_url"] = s3_url
        except Exception as e:
            logger.warning(f"Failed to fetch s3_result_url for session {session_id}: {e}")
        
        return {
            "success": True,
            "session_info": session_info_with_results
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session info {session_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка получения информации о сессии: {str(e)}"
        )

@router.delete("/sessions/{session_id}", dependencies=[Depends(get_api_key)])
async def terminate_session(session_id: str, settings: Settings = Depends(get_settings)):
    """
    Принудительно завершить сессию потоковой транскрибации
    """
    try:
        stream_service = manager.get_service(session_id) or StreamTranscriptionService(settings)
        session_found = False
        
        # Закрываем все WebSocket соединения если есть
        if session_id in manager.active_connections:
            for cid, websocket in list(manager.active_connections[session_id].items()):
                try:
                    await websocket.close()
                except Exception:
                    pass
                manager.disconnect(session_id, cid)
            session_found = True
        
        # Завершаем сессию в stream_service (это сохранит результаты в S3)
        if await stream_service.end_session(session_id):
            session_found = True
        
        if session_found:
            return {
                "success": True,
                "message": f"Сессия {session_id} завершена и результаты сохранены в S3"
            }
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Сессия {session_id} не найдена"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error terminating session {session_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка завершения сессии: {str(e)}"
        )


# PyAudio эндпоинты для записи с микрофона

@router.post("/microphone/start/{session_id}", dependencies=[Depends(get_api_key)])
async def start_microphone_recording(
    session_id: str,
    provider: str = "yandex_v3",
    language: str = "ru-RU",
    enable_diarization: bool = True,
    input_device_index: Optional[int] = None,
    settings: Settings = Depends(get_settings)
):
    """
    Начинает запись аудио с микрофона через PyAudio и потоковое распознавание.
    
    Args:
        session_id: Уникальный идентификатор сессии
        provider: Провайдер распознавания (yandex_v3, openai, etc.)
        language: Язык распознавания (ru-RU, en-US, etc.)
        enable_diarization: Включить диаризацию (разделение говорящих)
        input_device_index: Индекс устройства ввода (микрофона), если None — будет выбран первый доступный
    
    Returns:
        JSON с информацией о начатой сессии записи
    """
    try:
        logger.info(f"Начинаем запись с микрофона для сессии: {session_id}")
        
        # Проверяем существующую сессию
        if session_id in manager.transcription_sessions:
            existing_session = manager.transcription_sessions[session_id]
            
            # Если сессия активна, останавливаем её
            if existing_session.get("status") in ["recording", "connected"]:
                logger.info(f"Stopping existing session {session_id} before starting new one")
                
                # Останавливаем PyAudio если есть
                if "pyaudio_service" in existing_session:
                    try:
                        existing_session["pyaudio_service"].stop_recording()
                    except Exception as e:
                        logger.warning(f"Error stopping existing PyAudio service: {e}")
                
                # Очищаем сессию
                manager.disconnect(session_id)
            
            logger.info(f"Reusing session ID {session_id} for new recording")
        
        # Создаем PyAudio сервис
        pyaudio_service = PyAudioService()
        
        # Начинаем запись
        if not pyaudio_service.start_recording(input_device_index=input_device_index):
            raise HTTPException(
                status_code=500,
                detail="Failed to start microphone recording"
            )
        
        # Создаем сессию в менеджере
        manager.transcription_sessions[session_id] = {
            "created_at": datetime.now(),
            "status": "recording",
            "chunks_processed": 0,
            "provider": provider,
            "language": language,
            "enable_diarization": enable_diarization,
            "pyaudio_service": pyaudio_service
        }
        manager.session_results[session_id] = []
        
        # Создаем сервис транскрибации
        stream_service = StreamTranscriptionService(settings)
        
        # Запускаем фоновую задачу для обработки аудио
        asyncio.create_task(
            _process_microphone_audio(
                session_id, 
                pyaudio_service, 
                stream_service, 
                provider, 
                language, 
                enable_diarization
            )
        )
        
        logger.info(f"✅ Запись с микрофона начата для сессии: {session_id}")
        
        return {
            "success": True,
            "session_id": session_id,
            "status": "recording",
            "provider": provider,
            "language": language,
            "enable_diarization": enable_diarization,
            "input_device_index": input_device_index,
            "audio_info": pyaudio_service.get_audio_info(),
            "message": "Microphone recording started successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting microphone recording: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error starting microphone recording: {str(e)}"
        )

# Сервисные эндпоинты для работы с устройствами ввода
@router.get("/microphone/devices", dependencies=[Depends(get_api_key)])
async def list_microphone_devices():
    try:
        devices = PyAudioService.list_input_devices()
        return {
            "success": True,
            "count": len(devices),
            "devices": devices
        }
    except Exception as e:
        logger.error(f"Failed to list input devices: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list input devices: {e}")

@router.get("/microphone/devices/default", dependencies=[Depends(get_api_key)])
async def get_default_microphone_device():
    try:
        index = PyAudioService.get_default_input_device_index()
        return {
            "success": True,
            "default_index": index
        }
    except Exception as e:
        logger.error(f"Failed to get default input device: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get default input device: {e}")

@router.post("/microphone/stop/{session_id}", dependencies=[Depends(get_api_key)])
async def stop_microphone_recording(session_id: str):
    """
    Останавливает запись аудио с микрофона.
    
    Args:
        session_id: Уникальный идентификатор сессии
    
    Returns:
        JSON с информацией об остановленной сессии и результатами
    """
    try:
        logger.info(f"Останавливаем запись с микрофона для сессии: {session_id}")
        
        # Проверяем, что сессия существует
        if session_id not in manager.transcription_sessions:
            raise HTTPException(
                status_code=404,
                detail=f"Session {session_id} not found"
            )
        
        session_info = manager.transcription_sessions[session_id]
        
        # Останавливаем PyAudio сервис
        if "pyaudio_service" in session_info:
            pyaudio_service = session_info["pyaudio_service"]
            pyaudio_service.stop_recording()
            logger.info(f"PyAudio запись остановлена для сессии: {session_id}")
        
        # Обновляем статус сессии
        session_info["status"] = "stopped"
        session_info["stopped_at"] = datetime.now()
        
        # Получаем результаты
        results = manager.get_session_results(session_id, include_partial=False)
        transcript = manager.get_session_transcript(session_id, format_type="text")
        
        logger.info(f"✅ Запись с микрофона остановлена для сессии: {session_id}")
        
        return {
            "success": True,
            "session_id": session_id,
            "status": "stopped",
            "chunks_processed": session_info.get("chunks_processed", 0),
            "results_count": len(results) if results else 0,
            "transcript": transcript,
            "results": results,
            "message": "Microphone recording stopped successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping microphone recording: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error stopping microphone recording: {str(e)}"
        )


@router.get("/microphone/status/{session_id}", dependencies=[Depends(get_api_key)])
async def get_microphone_recording_status(session_id: str):
    """
    Получает статус записи с микрофона.
    
    Args:
        session_id: Уникальный идентификатор сессии
    
    Returns:
        JSON с информацией о статусе записи
    """
    try:
        # Проверяем, что сессия существует
        if session_id not in manager.transcription_sessions:
            raise HTTPException(
                status_code=404,
                detail=f"Session {session_id} not found"
            )
        
        session_info = manager.transcription_sessions[session_id]
        
        # Получаем информацию о PyAudio сервисе
        audio_info = {}
        if "pyaudio_service" in session_info:
            pyaudio_service = session_info["pyaudio_service"]
            audio_info = pyaudio_service.get_audio_info()
        
        # Получаем результаты
        results = manager.get_session_results(session_id)
        
        return {
            "success": True,
            "session_id": session_id,
            "status": session_info.get("status", "unknown"),
            "created_at": session_info.get("created_at"),
            "provider": session_info.get("provider"),
            "language": session_info.get("language"),
            "enable_diarization": session_info.get("enable_diarization"),
            "chunks_processed": session_info.get("chunks_processed", 0),
            "results_count": len(results) if results else 0,
            "audio_info": audio_info
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting microphone recording status: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error getting microphone recording status: {str(e)}"
        )


async def _process_microphone_audio(
    session_id: str,
    pyaudio_service: PyAudioService,
    stream_service: StreamTranscriptionService,
    provider: str,
    language: str,
    enable_diarization: bool
):
    """
    Обработка аудио с микрофона (без изменений основной логики)
    """
    try:
        logger.info(f"Начинаем обработку аудио с микрофона для сессии: {session_id}")
        
        # Начинаем сессию потокового распознавания
        request = StreamTranscriptionRequest(
            provider=provider,
            language=language,
            enable_diarization=enable_diarization,
            sample_rate=pyaudio_service.SAMPLE_RATE
        )
        await stream_service.start_session(session_id, request)
        
        chunk_count = 0
        
        # Обрабатываем аудио чанки
        async for audio_chunk in pyaudio_service.get_audio_chunks():
            if not pyaudio_service.is_recording_active():
                logger.info(f"Запись остановлена, завершаем обработку для сессии: {session_id}")
                break
            
            chunk_count += 1
            
            # Отправляем чанк на распознавание
            chunk = StreamChunk(
                chunk_id=f"{session_id}_{chunk_count}",
                audio_data=audio_chunk,
                timestamp=datetime.now(),
                is_final=False
            )
            await stream_service.process_audio_chunk(chunk, request, session_id)
            
            # Обновляем счетчик обработанных чанков
            if session_id in manager.transcription_sessions:
                manager.transcription_sessions[session_id]["chunks_processed"] = chunk_count
            
            # Получаем результаты и добавляем в менеджер
            results = await stream_service.get_session_results(session_id)
            if results:
                for result in results:
                    manager.add_transcription_result(session_id, result)
        
        # Отправляем финальный чанк
        final_chunk = StreamChunk(
            chunk_id=f"final_{session_id}",
            audio_data=b"",
            timestamp=datetime.now(),
            is_final=True
        )
        await stream_service.process_audio_chunk(final_chunk, request, session_id)
        
        # Получаем финальные результаты
        final_results = await stream_service.get_session_results(session_id)
        if final_results:
            for result in final_results:
                manager.add_transcription_result(session_id, result)
        
        logger.info(f"✅ Обработка аудио завершена для сессии: {session_id}, обработано чанков: {chunk_count}")
        
    except Exception as e:
        logger.error(f"Ошибка при обработке аудио с микрофона для сессии {session_id}: {e}")
        
        # Обновляем статус сессии на ошибку
        if session_id in manager.transcription_sessions:
            manager.transcription_sessions[session_id]["status"] = "error"
            manager.transcription_sessions[session_id]["error"] = str(e)
    
    finally:
        # Завершаем сессию потокового распознавания
        try:
            await stream_service.end_session(session_id)
        except Exception as e:
            logger.error(f"Ошибка при завершении сессии {session_id}: {e}")

@router.get("/status/{session_id}", response_model=Dict[str, Any])
async def get_stream_status(session_id: str, api_key: str = Depends(get_api_key)):
    """
    Получает статус потоковой транскрибации
    """
    try:
        results = manager.get_session_results(session_id, include_partial=False)
        
        if results is None:
            raise HTTPException(
                status_code=404,
                detail=f"Сессия {session_id} не найдена"
            )
        
        return {
            "success": True,
            "session_id": session_id,
            "results_count": len(results),
            "results": results
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session results {session_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка получения результатов сессии: {str(e)}"
        )


@router.post("/end_session/{session_id}") # Новый эндпоинт для завершения сессии
async def end_stream_session(session_id: str, api_key: str = Depends(get_api_key)):
    """
    Завершает сессию потокового распознавания по ID.
    """
    try:
        svc = manager.get_service(session_id)
        if not svc:
            return {"message": f"Сессия {session_id} не активна или уже завершена."}
        await svc.end_session(session_id)
        return {"message": f"Сессия {session_id} успешно завершена."}
    except Exception as e:
        logger.error(f"Ошибка при завершении сессии {session_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка при завершении сессии: {e}")