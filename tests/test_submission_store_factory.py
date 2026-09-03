from pathlib import Path

import pytest

from linguistic_oj.postgres_submission_store import PostgresSubmissionStore
from linguistic_oj.submission_store import SubmissionStore
from linguistic_oj.submission_store_factory import build_submission_store


def test_build_submission_store_uses_sqlite_path(tmp_path: Path) -> None:
    store = build_submission_store(
        database_path=tmp_path / "submissions.db",
        postgres_database_url=None,
    )

    assert isinstance(store, SubmissionStore)


def test_build_submission_store_uses_postgres_url() -> None:
    store = build_submission_store(
        database_path=None,
        postgres_database_url="postgresql://judge@/linguistic_oj?host=/private/socket",
    )

    assert isinstance(store, PostgresSubmissionStore)


@pytest.mark.parametrize(
    ("database_path", "postgres_database_url"),
    ((None, None), (Path("submissions.db"), "postgresql://judge@/linguistic_oj")),
)
def test_build_submission_store_requires_exactly_one_backend(
    database_path: Path | None,
    postgres_database_url: str | None,
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        build_submission_store(
            database_path=database_path,
            postgres_database_url=postgres_database_url,
        )
