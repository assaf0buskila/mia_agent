"""The browser's audio label is a claim, not evidence.

`MediaRecorder` leaves `blob.type` empty on several browsers, and the widget then
assumes webm (`app/web/ask_mia.js`: `blob.type || 'audio/webm'`, filename forced to
`note.webm`). A Firefox recording is ogg/opus, so it arrives labelled `audio/webm`.
The provider honours the label, fails to demux, and returns empty text — which the
visitor sees as "לא שמעתי טוב" with nothing wrong with their microphone.

Verified against live production before the fix: the identical opus bytes transcribed
correctly as `audio/ogg` and came back empty as `audio/webm`.
"""

from __future__ import annotations

import struct

from app.api.website import _voice_filename, sniff_audio_container

OGG = b"OggS" + b"\x00" * 60
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 60
MP4 = b"\x00\x00\x00\x20" + b"ftyp" + b"isom" + b"\x00" * 40
WAV = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + b"\x00" * 40
MP3_ID3 = b"ID3\x03\x00" + b"\x00" * 60
MP3_SYNC = b"\xff\xfb\x90\x00" + b"\x00" * 60


def test_each_container_is_identified_from_its_own_bytes() -> None:
    assert sniff_audio_container(OGG) == "audio/ogg"
    assert sniff_audio_container(WEBM) == "audio/webm"
    assert sniff_audio_container(MP4) == "audio/mp4"
    assert sniff_audio_container(WAV) == "audio/wav"
    assert sniff_audio_container(MP3_ID3) == "audio/mpeg"
    assert sniff_audio_container(MP3_SYNC) == "audio/mpeg"


def test_the_regression_an_ogg_recording_labelled_webm() -> None:
    """The exact production failure. The bytes are ogg; the browser said webm."""
    assert sniff_audio_container(OGG) == "audio/ogg"
    assert sniff_audio_container(OGG) != "audio/webm"
    # And the filename follows the truth, because the provider reads that too.
    assert _voice_filename(sniff_audio_container(OGG)) == "note.ogg"


def test_unknown_bytes_defer_to_the_claim_rather_than_guessing() -> None:
    """Sniffing must not become a second way to be wrong."""
    assert sniff_audio_container(b"not audio at all") == ""
    assert sniff_audio_container(b"") == ""


def test_every_sniffed_container_has_a_filename_the_provider_understands() -> None:
    """A container we can name but not file would trade one silent failure for another."""
    for blob in (OGG, WEBM, MP4, WAV, MP3_ID3):
        mime = sniff_audio_container(blob)
        assert mime, blob[:4]
        name = _voice_filename(mime)
        assert name != "note.webm" or mime == "audio/webm", (mime, name)
        assert "." in name
