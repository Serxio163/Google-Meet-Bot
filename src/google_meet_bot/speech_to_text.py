import json
import os
import subprocess
import tempfile
import datetime
import logging
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

logger = logging.getLogger(__name__)


class SpeechToText:
    def __init__(self):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.MAX_AUDIO_SIZE_BYTES = int(os.getenv('MAX_AUDIO_SIZE_BYTES', 20 * 1024 * 1024))
        self.GPT_MODEL = os.getenv('GPT_MODEL', 'gpt-4')
        self.WHISPER_MODEL = os.getenv('WHISPER_MODEL', 'whisper-1')

    def _which(self, program: str) -> Optional[str]:
        from shutil import which
        return which(program)

    def _ensure_ff_tools(self) -> None:
        for tool in ("ffmpeg", "ffprobe"):
            if not self._which(tool):
                raise RuntimeError(f"{tool} not found in PATH. Please install and ensure it's available.")

    def get_file_size(self, file_path):
        return os.path.getsize(file_path)

    def get_audio_duration(self, audio_file_path):
        self._ensure_ff_tools()
        result = subprocess.run(
            ['ffprobe', '-i', audio_file_path, '-show_entries', 'format=duration', '-v', 'quiet', '-of', 'csv=p=0'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
        )
        out = (result.stdout or b'').decode('utf-8', errors='ignore').strip()
        try:
            return float(out)
        except Exception:
            logger.warning(f"Failed to parse duration from ffprobe output: '{out}'")
            return 0.0

    def _reencode_audio(self, src_path: str) -> str:
        """Re-encode to mono 16kHz with moderate bitrate to reduce size."""
        self._ensure_ff_tools()
        temp_dir = tempfile.mkdtemp()
        dst_path = os.path.join(
            temp_dir, f'reencoded_{datetime.datetime.now().strftime("%Y%m%d%H%M%S")}.wav'
        )
        cmd = [
            'ffmpeg', '-y', '-i', src_path,
            '-ac', '1',           # mono
            '-ar', '16000',        # 16 kHz
            '-b:a', '64k',         # 64 kbps
            dst_path
        ]
        logger.debug(f"Re-encode command: {' '.join(cmd)}")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return dst_path

    def _trim_audio(self, src_path: str, target_duration: float) -> str:
        self._ensure_ff_tools()
        temp_dir = tempfile.mkdtemp()
        dst_path = os.path.join(
            temp_dir, f'trimmed_{datetime.datetime.now().strftime("%Y%m%d%H%M%S")}.wav'
        )
        cmd = ['ffmpeg', '-y', '-i', src_path, '-ss', '0', '-t', str(max(0.0, target_duration)), dst_path]
        logger.debug(f"Trim command: {' '.join(cmd)}")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return dst_path

    def resize_audio_if_needed(self, audio_file_path):
        try:
            audio_size = self.get_file_size(audio_file_path)
        except Exception as e:
            raise RuntimeError(f"Audio file not accessible: {e}")

        if audio_size <= self.MAX_AUDIO_SIZE_BYTES:
            return audio_file_path

        # Try re-encoding first
        logger.info("Audio exceeds size limit. Re-encoding to reduce size...")
        reencoded = self._reencode_audio(audio_file_path)
        if self.get_file_size(reencoded) <= self.MAX_AUDIO_SIZE_BYTES:
            return reencoded

        # If still too large, trim proportionally
        current_duration = max(1e-3, self.get_audio_duration(reencoded)) or max(1e-3, self.get_audio_duration(audio_file_path))
        src_for_trim = reencoded if os.path.exists(reencoded) else audio_file_path
        # Compute proportional target duration based on size ratio
        src_size = self.get_file_size(src_for_trim)
        target_duration = current_duration * self.MAX_AUDIO_SIZE_BYTES / max(1, src_size)
        logger.info("Trimming audio to fit size limit (target %.2fs)...", target_duration)
        trimmed = self._trim_audio(src_for_trim, target_duration)
        return trimmed

    def transcribe_audio(self, audio_file_path):
        with open(audio_file_path, 'rb') as audio_file:
            transcript = self.client.audio.transcriptions.create(
                file=audio_file,
                model=self.WHISPER_MODEL,
            )
            logger.info("Transcribe: Done")
            return transcript.text

    def abstract_summary_extraction(self, transcription):
        response = self.client.chat.completions.create(
            model=self.GPT_MODEL,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": "You are a highly skilled AI trained in language comprehension and summarization. I would like you to read the following text and summarize it into a concise abstract paragraph. Aim to retain the most important points, providing a coherent and readable summary that could help a person understand the main points of the discussion without needing to read the entire text. Please avoid unnecessary details or tangential points."
                },
                {"role": "user", "content": transcription}
            ]
        )
        logger.info("Summary: Done")
        return response.choices[0].message.content

    def key_points_extraction(self, transcription):
        response = self.client.chat.completions.create(
            model=self.GPT_MODEL,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": "You are a proficient AI with a specialty in distilling information into key points. Based on the following text, identify and list the main points that were discussed or brought up. These should be the most important ideas, findings, or topics that are crucial to the essence of the discussion. Your goal is to provide a list that someone could read to quickly understand what was talked about."
                },
                {"role": "user", "content": transcription}
            ]
        )
        logger.info("Key Points: Done")
        return response.choices[0].message.content

    def action_item_extraction(self, transcription):
        response = self.client.chat.completions.create(
            model=self.GPT_MODEL,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": "You are an AI expert in analyzing conversations and extracting action items. Please review the text and identify any tasks, assignments, or actions that were agreed upon or mentioned as needing to be done. These could be tasks assigned to specific individuals, or general actions that the group has decided to take. Please list these action items clearly and concisely."
                },
                {"role": "user", "content": transcription}
            ]
        )
        logger.info("Action Items: Done")
        return response.choices[0].message.content

    def sentiment_analysis(self, transcription):
        response = self.client.chat.completions.create(
            model=self.GPT_MODEL,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": "As an AI with expertise in language and emotion analysis, your task is to analyze the sentiment of the following text. Please consider the overall tone of the discussion, the emotion conveyed by the language used, and the context in which words and phrases are used. Indicate whether the sentiment is generally positive, negative, or neutral, and provide brief explanations for your analysis where possible."
                },
                {"role": "user", "content": transcription}
            ]
        )
        logger.info("Sentiment: Done")
        return response.choices[0].message.content

    def meeting_minutes(self, transcription):
        abstract_summary = self.abstract_summary_extraction(transcription)
        key_points = self.key_points_extraction(transcription)
        action_items = self.action_item_extraction(transcription)
        sentiment = self.sentiment_analysis(transcription)
        return {
            'abstract_summary': abstract_summary,
            'key_points': key_points,
            'action_items': action_items,
            'sentiment': sentiment
        }

    def store_in_json_file(self, data):
        temp_dir = tempfile.mkdtemp()
        file_path = os.path.join(temp_dir, f'meeting_data_{datetime.datetime.now().strftime("%Y%m%d%H%M%S")}.json')
        logger.info("JSON file path: %s", file_path)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("JSON file created successfully.")

    def transcribe(self, audio_file_path):
        audio_file_path = self.resize_audio_if_needed(audio_file_path)
        transcription = self.transcribe_audio(audio_file_path)
        summary = self.meeting_minutes(transcription)
        self.store_in_json_file(summary)

        logger.info("Abstract Summary: %s", summary['abstract_summary'])
        logger.info("Key Points: %s", summary['key_points'])
        logger.info("Action Items: %s", summary['action_items'])
        logger.info("Sentiment: %s", summary['sentiment'])


