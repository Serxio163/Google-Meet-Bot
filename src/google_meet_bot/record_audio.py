import sounddevice as sd
from scipy.io.wavfile import write
import os
import logging
from dotenv import load_dotenv
import numpy as np
import platform


load_dotenv()
logger = logging.getLogger(__name__)


class AudioRecorder:
    def __init__(self):
        self.sample_rate = int(os.getenv('SAMPLE_RATE', 44100))
        self._stream = None
        self._chunks = []
        self._filename = None
        self._channels = 1

    def get_audio(self, filename, duration):
        logger.info("Recording audio: duration=%ss, sample_rate=%s, channels=1 (mono)", duration, self.sample_rate)
        recording = sd.rec(int(duration * self.sample_rate), samplerate=self.sample_rate, channels=1, dtype='int16')
        sd.wait()  # Wait until the recording is finished
        write(filename, self.sample_rate, recording)
        logger.info("Recording finished. Saved as %s", filename)

    def start_recording(self, filename, source: str = 'mic'):
        """Start continuous recording until stop_recording() is called.
        source: 'mic' (microphone) or 'system' (system sound loopback, Windows WASAPI only)
        """
        if self._stream is not None:
            logger.warning("Recording already in progress, ignoring start_recording")
            return
        self._filename = filename
        self._chunks = []

        def _callback(indata, frames, time_info, status):
            if status:
                logger.warning(f"sounddevice status: {status}")
            try:
                self._chunks.append(indata.copy())
            except Exception as e:
                logger.error(f"Error buffering audio chunk: {e}")

        # Configure stream depending on source
        if source == 'system':
            os_name = platform.system()
            if os_name != 'Windows':
                logger.error("System sound recording is supported only on Windows (WASAPI). Falling back to microphone.")
                source = 'mic'
            else:
                try:
                    # Use WASAPI loopback to capture system output audio
                    wasapi = sd.WasapiSettings(loopback=True)
                    default_devices = sd.default.device  # (input, output)
                    output_dev = default_devices[1]
                    channels = 2  # system output is typically stereo
                    logger.info("Starting continuous recording (system sound): sample_rate=%s, channels=%s, device=%s", self.sample_rate, channels, output_dev)
                    self._stream = sd.InputStream(samplerate=self.sample_rate, channels=channels, dtype='int16', callback=_callback, device=output_dev, extra_settings=wasapi)
                    self._stream.start()
                    self._channels = channels
                    logger.info("Continuous recording (system sound) started")
                    return
                except Exception as e:
                    logger.error(f"Failed to start system sound recording via WASAPI loopback: {e}. Falling back to microphone.")
                    source = 'mic'

        # Fallback or microphone source
        if source == 'mic':
            channels = 1
            logger.info("Starting continuous recording (microphone): sample_rate=%s, channels=1 (mono)", self.sample_rate)
            self._stream = sd.InputStream(samplerate=self.sample_rate, channels=channels, dtype='int16', callback=_callback)
            self._stream.start()
            self._channels = channels
            logger.info("Continuous recording (microphone) started")

    def stop_recording(self):
        """Stop recording and save to WAV file."""
        if self._stream is None:
            logger.warning("No active recording stream to stop")
            return
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None
        logger.info("Continuous recording stopped, saving to file: %s", self._filename)

        try:
            if not self._chunks:
                logger.warning("No audio chunks captured; creating empty file: %s", self._filename)
                write(self._filename, self.sample_rate, np.array([], dtype=np.int16))
            else:
                concatenated = np.concatenate(self._chunks, axis=0).astype('int16')
                write(self._filename, self.sample_rate, concatenated)
            logger.info("Recording saved: %s", self._filename)
        except Exception as e:
            logger.error(f"Failed to save recording to {self._filename}: {e}")
        finally:
            self._chunks = []
            self._filename = None


