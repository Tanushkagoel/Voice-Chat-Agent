from livekit.api import AccessToken, VideoGrants

from app.config import Settings


def room_name(session_id: str) -> str:
    return session_id


def _jwt(settings: Settings, identity: str, room: str) -> str:
    grant = VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    return (
        AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grant)
        .to_jwt()
    )


def caller_token(settings: Settings, session_id: str) -> str:
    return _jwt(settings, f"caller-{session_id}", room_name(session_id))


def agent_token(settings: Settings, session_id: str) -> str:
    return _jwt(settings, f"agent-{session_id}", room_name(session_id))
