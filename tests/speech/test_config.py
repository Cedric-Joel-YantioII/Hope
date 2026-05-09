"""Tests for speech configuration."""

from hope.core.config import HopeConfig, SpeechConfig


def test_speech_config_defaults():
    cfg = SpeechConfig()
    assert cfg.backend == "auto"
    assert cfg.model == "base"
    assert cfg.language == ""
    assert cfg.device == "auto"
    assert cfg.compute_type == "float16"


def test_hope_config_has_speech():
    cfg = HopeConfig()
    assert hasattr(cfg, "speech")
    assert isinstance(cfg.speech, SpeechConfig)
    assert cfg.speech.backend == "auto"


# The ``hope.system`` façade was removed during voice-arch cleanup.
# Speech backend wiring now lives on the daemon/brain_session primitives
# directly, so the old HopeSystem dataclass-field assertion is obsolete.
