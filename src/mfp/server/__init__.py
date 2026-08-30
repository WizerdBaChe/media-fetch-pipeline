"""Local HTTP service backing the GUI and the Agent-facing Skill.

Loopback-only and unauthenticated by design (O-3). One interface serves both
consumers, so there is no second surface to keep in sync.
"""

from mfp.server.app import API_PREFIX, create_app, create_app_from_paths
from mfp.server.events import EventBroadcaster

__all__ = ["API_PREFIX", "EventBroadcaster", "create_app", "create_app_from_paths"]
