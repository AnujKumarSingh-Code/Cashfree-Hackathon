"""Sarvam adapter: speech-to-text (Saaras) and text-to-speech (Bulbul).

Verified against docs.sarvam.ai on 2026-11-21:
  POST https://api.sarvam.ai/speech-to-text   multipart: file, model=saaras:v4, mode, optional language_code
       -> {request_id, transcript, language_code, language_probability}; sync limit 30 seconds of audio
       modes: transcribe | translate | verbatim | translit | codemix ; 8 kHz telephony audio supported
  POST https://api.sarvam.ai/text-to-speech   json: text (<=2500 chars), language_code, model=bulbul:v3, speaker
       -> {request_id, audios: [base64]}
  auth header: api-subscription-key
  Bulbul languages: hi-IN bn-IN ta-IN te-IN gu-IN kn-IN ml-IN mr-IN pa-IN od-IN en-IN
"""
from __future__ import annotations

import base64

import httpx

BASE_URL = "https://api.sarvam.ai"
TTS_LANGUAGES = ("hi-IN", "bn-IN", "ta-IN", "te-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "od-IN", "en-IN")


class SarvamError(Exception):
    pass


class SarvamLive:
    mode = "live"

    def __init__(self, api_key: str, stt_model: str = "saaras:v4", tts_model: str = "bulbul:v3",
                 speaker: str = "shubh", timeout: float = 40.0, transport: httpx.BaseTransport | None = None):
        self.stt_model, self.tts_model, self.speaker = stt_model, tts_model, speaker
        self._client = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport,
                                    headers={"api-subscription-key": api_key})

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            hint = {402: " (credits exhausted)", 429: " (rate limited)", 403: " (key rejected)"}.get(r.status_code, "")
            raise SarvamError(f"Sarvam returned {r.status_code}{hint}")
        return r.json()

    def transcribe(self, audio: bytes, filename: str = "reply.wav", mode: str = "codemix",
                   language_code: str | None = None) -> dict:
        data = {"model": self.stt_model, "mode": mode}
        if language_code:
            data["language_code"] = language_code
        try:
            r = self._client.post("/speech-to-text", data=data, files={"file": (filename, audio)})
        except httpx.HTTPError as e:
            raise SarvamError(f"network error calling Sarvam: {type(e).__name__}") from e
        j = self._check(r)
        return {"transcript": j.get("transcript", ""), "language_code": j.get("language_code"),
                "language_probability": j.get("language_probability"), "source": "sarvam_saaras"}

    def speak(self, text: str, language_code: str) -> bytes:
        if language_code not in TTS_LANGUAGES:
            raise SarvamError(f"language {language_code} is not in the Bulbul list")
        try:
            r = self._client.post("/text-to-speech", json={"text": text[:2500], "language_code": language_code,
                                                           "model": self.tts_model, "speaker": self.speaker})
        except httpx.HTTPError as e:
            raise SarvamError(f"network error calling Sarvam: {type(e).__name__}") from e
        j = self._check(r)
        audios = j.get("audios") or []
        if not audios:
            raise SarvamError("Sarvam returned no audio")
        return base64.b64decode(audios[0])


class SarvamMock:
    """No key or no network. It cannot hear or speak, and says so."""
    mode = "mock"

    def transcribe(self, audio: bytes, filename: str = "reply.wav", mode: str = "codemix",
                   language_code: str | None = None) -> dict:
        raise SarvamError("Sarvam is not configured (SARVAM_API_KEY missing). Type the borrower's reply instead.")

    def speak(self, text: str, language_code: str) -> bytes:
        raise SarvamError("Sarvam is not configured (SARVAM_API_KEY missing). The script is shown as text.")


def get_adapter(settings):
    if settings.sarvam_mode == "live":
        return SarvamLive(settings.sarvam_api_key, settings.sarvam_stt_model, settings.sarvam_tts_model,
                          settings.sarvam_tts_speaker)
    return SarvamMock()
