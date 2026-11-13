"""Publish each current analysis decision; heartbeats never advance media time."""
from __future__ import annotations


class AnalysisPublisher:
    def __init__(self, server=None):
        self.server = server
        self.last_revision = None
        self.published_count = 0

    def before_observation(self, session, clip_id, timestamp):
        if self.server is not None:
            for event in self.server.warning_events(clip_id):
                session.confirm_warning(event["warning_request_id"], timestamp, "unity_ui_ack")

    def publish(self, decision, clip_id):
        if self.server is not None:
            self.last_revision = self.server.publish(decision, clip_id)
            self.published_count += 1
        return self.last_revision

    def finish(self, reason="recording_finished"):
        if self.server is not None:
            if self.last_revision is not None and self.server.connected.is_set():
                self.server.wait_until_sent(self.last_revision, timeout=1.0)
            self.server.mark_unavailable(reason)
