from pai_loop.database import normalize_database_url, session_dependency


def test_normalize_database_url_selects_psycopg_v3_for_provider_urls() -> None:
    authority = "user:pass" + "@" + "db.example/pai"
    assert (
        normalize_database_url("postgresql://" + authority)
        == "postgresql+psycopg://" + authority
    )
    assert (
        normalize_database_url("postgres://" + authority)
        == "postgresql+psycopg://" + authority
    )


def test_normalize_database_url_preserves_explicit_or_non_postgres_urls() -> None:
    authority = "user:pass" + "@" + "db.example/pai"
    assert (
        normalize_database_url("postgresql+psycopg://" + authority)
        == "postgresql+psycopg://" + authority
    )
    assert normalize_database_url("sqlite:///:memory:") == "sqlite:///:memory:"


def test_session_dependency_always_closes_the_request_session() -> None:
    class DummySession:
        closed = False

        def close(self) -> None:
            self.closed = True

    session = DummySession()
    dependency = session_dependency(lambda: session)  # type: ignore[arg-type]
    iterator = dependency()

    assert next(iterator) is session
    try:
        next(iterator)
    except StopIteration:
        pass
    else:  # pragma: no cover - generator contract assertion
        raise AssertionError("session dependency must yield exactly once")
    assert session.closed is True
