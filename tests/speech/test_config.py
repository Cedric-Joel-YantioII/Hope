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


def test_hope_system_has_speech_backend():
    """HopeSystem has a speech_backend attribute."""
    from hope.system import HopeSystem

    assert "speech_backend" in HopeSystem.__dataclass_fields__
