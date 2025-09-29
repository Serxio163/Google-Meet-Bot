"""Google Meet Bot package.

Automate joining Google Meet, record audio, transcribe with Whisper, and summarize using GPT.
"""

from .record_audio import AudioRecorder
from .speech_to_text import SpeechToText

__all__ = [
    "AudioRecorder",
    "SpeechToText",
]


