"""Retention contract for replayable Redis Streams."""

from src.infra.messaging import redis_streams


def test_xadd_json_frame_sets_stream_ttl(monkeypatch) -> None:
    calls: list[tuple] = []

    class FakePipeline:
        def xadd(self, *args, **kwargs):
            calls.append(("xadd", args, kwargs))
            return self

        def expire(self, *args):
            calls.append(("expire", *args))
            return self

        def execute(self):
            return ["1-0", True]

    class FakeRedis:
        def xadd(self, *args, **kwargs):
            calls.append(("xadd", args, kwargs))
            return "1-0"

        def pipeline(self, *, transaction: bool):
            assert transaction is True
            return FakePipeline()

    monkeypatch.setattr(redis_streams, "get_redis_client", lambda: FakeRedis())

    entry_id = redis_streams.xadd_json_frame("events:test", {"type": "progress"})

    assert entry_id == "1-0"
    assert ("expire", "events:test", 1800) in calls
