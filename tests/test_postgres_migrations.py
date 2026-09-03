import pytest

from linguistic_oj.postgres_migrations import validate_postgres_url


@pytest.mark.parametrize(
    "database_url",
    (
        "postgresql://judge@/linguistic_oj?host=/private/socket",
        "postgres://judge:secret@127.0.0.1:5433/linguistic_oj",
    ),
)
def test_validate_postgres_url_accepts_database_urls(database_url: str) -> None:
    assert validate_postgres_url(database_url) == database_url


@pytest.mark.parametrize("database_url", ("", "sqlite:///submissions.db", "postgresql://judge@/"))
def test_validate_postgres_url_rejects_non_database_urls(database_url: str) -> None:
    with pytest.raises(ValueError):
        validate_postgres_url(database_url)
