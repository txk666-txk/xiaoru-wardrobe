"""Verify the packaged FFmpeg can serve the TTS path without ffprobe."""

import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg
from pydub import AudioSegment

from tts.ms_tts_sdk import Speech


class AudioConversionTests(unittest.TestCase):
    def test_edge_mp3_converts_to_playable_wav_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as directory:
            mp3 = Path(directory) / "speech.mp3"
            with patch.object(AudioSegment, "converter", imageio_ffmpeg.get_ffmpeg_exe()):
                # Generate real encoded audio; no network or TTS credentials.
                AudioSegment.silent(duration=350, frame_rate=24000).export(mp3, format="mp3").close()
                with patch("pydub.audio_segment.mediainfo_json",
                           side_effect=AssertionError("ffprobe is not part of the slim runtime")):
                    wav = Speech.convert_mp3_to_wav(None, str(mp3))
            with wave.open(wav) as audio:
                self.assertEqual(audio.getframerate(), 44100)
                self.assertGreater(audio.getnframes(), 10000)
                self.assertLess(audio.getnframes(), 22050)


if __name__ == "__main__":
    unittest.main()
