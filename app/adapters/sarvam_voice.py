"""Sarvam Voice Agents: place one outbound call and read its result.

Verified against docs.sarvam.ai (Voice Agents API) on 2026-11-21:
  POST https://apps.sarvam.ai/api/outbounds/v1/orgs/{org_id}/workspaces/{workspace_id}/outbounds
       body: app_config{app_id, app_version, connection_config{connection_id, agent_phone_number}, agent_variables?},
             user_config{user_phone_number}, webhook_config{url, metadata}?      -> {attempt_id}
  GET  https://apps.sarvam.ai/api/analytics/v1/{org_id}/{workspace_id}/{app_id}/attempts?start_datetime&end_datetime
       -> {items:[{attempt_id, interaction_id, connectivity_status, failure_reason, ended_by, duration_in_seconds, ...}]}
  GET  https://apps.sarvam.ai/api/analytics/v1/{org_id}/{workspace_id}/{app_id}/transcripts/{interaction_id}
       -> {messages:[{turn_id, role: assistant|user, content}]}
  auth header: X-API-Key (the Voice Agents key, not the api.sarvam.ai key)

Errors carry only a status code. The key and phone numbers are never put in an error or a log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

BASE_URL = "https://apps.sarvam.ai"


class SarvamVoiceError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _request(settings, method: str, path: str, transport=None, **kw) -> dict:
    try:
        with httpx.Client(base_url=BASE_URL, timeout=25.0, transport=transport,
                          headers={"X-API-Key": settings.sarvam_voice_api_key}) as c:
            r = c.request(method, path, **kw)
    except httpx.HTTPError as e:
        raise SarvamVoiceError(f"network error calling Sarvam Voice: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise SarvamVoiceError(f"Sarvam Voice returned {r.status_code}", r.status_code)
    try:
        return r.json()
    except ValueError as e:
        raise SarvamVoiceError("Sarvam Voice returned a non-JSON body", r.status_code) from e


def place_call(settings, user_phone_e164: str, agent_variables: dict | None = None, webhook_url: str | None = None,
               metadata: dict | None = None, transport=None) -> dict:
    app_config = {"app_id": settings.sarvam_agent_id, "app_version": int(settings.sarvam_agent_version),
                  "connection_config": {"connection_id": settings.sarvam_connection_id,
                                        "agent_phone_number": settings.sarvam_caller_number}}
    if agent_variables:
        app_config["agent_variables"] = agent_variables
    body: dict = {"app_config": app_config, "user_config": {"user_phone_number": user_phone_e164}}
    if webhook_url:
        body["webhook_config"] = {"url": webhook_url, "metadata": metadata or {}}
    path = f"/api/outbounds/v1/orgs/{settings.sarvam_org_id}/workspaces/{settings.sarvam_workspace_id}/outbounds"
    res = _request(settings, "POST", path, transport, json=body)
    if not res.get("attempt_id"):
        raise SarvamVoiceError("Sarvam Voice did not return an attempt_id")
    return {"attempt_id": res["attempt_id"]}


def find_attempt(settings, attempt_id: str, since: datetime, transport=None) -> dict | None:
    """Look the attempt up in the analytics list. None means Sarvam has not listed it yet."""
    start = (since.astimezone(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None)
    end = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(tzinfo=None)
    path = (f"/api/analytics/v1/{settings.sarvam_org_id}/{settings.sarvam_workspace_id}/"
            f"{settings.sarvam_agent_id}/attempts")
    res = _request(settings, "GET", path, transport, params={
        "start_datetime": start.isoformat(timespec="seconds"), "end_datetime": end.isoformat(timespec="seconds"),
        "limit": 200, "sort_by": "attempted_at", "sort_order": "desc"})
    for item in res.get("items") or []:
        if item.get("attempt_id") == attempt_id:
            return {k: item.get(k) for k in ("attempt_id", "interaction_id", "connectivity_status", "failure_reason",
                                             "ended_by", "duration_in_seconds", "language_name", "num_messages",
                                             "start_datetime", "end_datetime", "user_contact_masked")}
    return None


def get_transcript(settings, interaction_id: str, transport=None) -> list[dict]:
    path = (f"/api/analytics/v1/{settings.sarvam_org_id}/{settings.sarvam_workspace_id}/"
            f"{settings.sarvam_agent_id}/transcripts/{quote(interaction_id, safe='')}")
    res = _request(settings, "GET", path, transport)
    return [{"role": m.get("role"), "content": m.get("content") or ""} for m in (res.get("messages") or [])]
