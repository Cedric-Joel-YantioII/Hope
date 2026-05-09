"""Speech subsystem — whisper.cpp STT only.

Cloud STT (Deepgram, OpenAI Whisper), faster-whisper, and all of the
TTS backends were deleted during the voice-arch cleanup. TTS now goes
through ``hope.audio.say`` (macOS ``say``). The surviving STT stub
registers itself via ``@SpeechRegistry.register`` on import.
"""

from __future__ import annotations

try:
    from hope.speech import whisper_cpp  # noqa: F401
except ImportError:
    pass
