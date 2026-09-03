from pathlib import Path

import pytest

from linguistic_oj.postgres_migrations import resolve_postgres_url, validate_postgres_url


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


@pytest.mark.parametrize("sslmode", ("", "disable", "allow", "prefer"))
def test_validate_postgres_url_requires_remote_tls(sslmode: str) -> None:
    suffix = f"?sslmode={sslmode}" if sslmode else ""
    with pytest.raises(ValueError, match="secure sslmode"):
        validate_postgres_url(f"postgresql://judge@db.example/linguistic_oj{suffix}")


def test_validate_postgres_url_cannot_bypass_tls_with_query_host() -> None:
    with pytest.raises(ValueError, match="Unix socket"):
        validate_postgres_url(
            "postgresql://judge@/linguistic_oj?host=db.example&sslmode=disable"
        )


@pytest.mark.parametrize(
    "database_url",
    (
        "postgresql:///linguistic_oj",
        "postgresql:///linguistic_oj?service=remote",
    ),
)
def test_validate_postgres_url_rejects_ambient_host_resolution(
    database_url: str,
) -> None:
    with pytest.raises(ValueError, match="explicit host|service indirection"):
        validate_postgres_url(database_url)


def test_validate_postgres_url_rejects_multihost_authority() -> None:
    with pytest.raises(ValueError, match="exactly one host"):
        validate_postgres_url(
            "postgresql://judge@127.0.0.1:5432,db.example:5432/linguistic_oj"
        )


@pytest.mark.parametrize(
    "database_url",
    (
        "postgresql:///linguistic_oj?host=&sslmode=require",
        "postgresql://judge@127.0.0.1/expected?dbname=",
        "postgresql://judge@127.0.0.1/linguistic_oj?host=db.example&sslmode=require",
        "postgresql://judge@127.0.0.1/linguistic_oj?hostaddr=203.0.113.10",
        "postgresql://judge@127.0.0.1/linguistic_oj?port=6432",
    ),
)
def test_validate_postgres_url_rejects_target_overrides(database_url: str) -> None:
    with pytest.raises(ValueError, match="target parameters|Unix socket"):
        validate_postgres_url(database_url)


def test_validate_postgres_url_rejects_libpq_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.10")

    with pytest.raises(ValueError, match=r"PG\* connection environment"):
        validate_postgres_url("postgresql://judge@127.0.0.1/linguistic_oj")


def test_resolve_postgres_url_reads_production_credentials_from_file(
    tmp_path: Path,
) -> None:
    credential = tmp_path / "postgres-url"
    credential.write_text(
        "postgresql://judge:secret@db.example/linguistic_oj?sslmode=verify-full\n",
        encoding="utf-8",
    )

    assert resolve_postgres_url(
        inline_url=None,
        credential_file=credential,
        allow_inline_credentials=False,
    ).endswith("sslmode=verify-full")


def test_resolve_postgres_url_rejects_inline_production_password() -> None:
    with pytest.raises(ValueError, match="credential"):
        resolve_postgres_url(
            inline_url="postgresql://judge:secret@127.0.0.1/linguistic_oj",
            credential_file=None,
            allow_inline_credentials=False,
        )


@pytest.mark.parametrize(
    "secret_parameter",
    ("sslpassword=secret", "oauth_client_secret=secret", "scram_client_key=secret"),
)
def test_resolve_postgres_url_rejects_inline_extended_secrets(
    secret_parameter: str,
) -> None:
    with pytest.raises(ValueError, match="credential"):
        resolve_postgres_url(
            inline_url=f"postgresql://judge@127.0.0.1/linguistic_oj?{secret_parameter}",
            credential_file=None,
            allow_inline_credentials=False,
        )
