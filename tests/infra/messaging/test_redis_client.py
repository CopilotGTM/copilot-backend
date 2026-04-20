import ssl


def test_sync_redis_tls_defaults_to_cert_required(monkeypatch) -> None:
    import src.infra.messaging.redis_client as redis_client

    captured_params: dict[str, object] = {}

    class _FakeConfig:
        env = "prod"

        @property
        def get_redis_client_details(self):
            return ("redis.example.com", 6380, "secret")

    class _FakeConnectionPool:
        def __init__(self, **kwargs):
            captured_params.update(kwargs)

        def disconnect(self):
            return None

    class _FakeRedis:
        def __init__(self, connection_pool):
            self.connection_pool = connection_pool

        def ping(self):
            return True

    redis_client.RedisConnectionManager._instance = None
    monkeypatch.setattr(redis_client, "Config", lambda: _FakeConfig())
    monkeypatch.setattr(redis_client.redis, "ConnectionPool", _FakeConnectionPool)
    monkeypatch.setattr(redis_client.redis, "Redis", _FakeRedis)
    monkeypatch.delenv("REDIS_SSL_CERT_REQS", raising=False)

    manager = redis_client.RedisConnectionManager()
    manager._sync_pool = None
    manager._initialized = False

    manager._initialize_pool()

    assert captured_params["ssl_cert_reqs"] == ssl.CERT_REQUIRED
