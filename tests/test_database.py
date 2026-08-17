from pai_loop.database import normalize_database_url


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
