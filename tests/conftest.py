from pathlib import Path
import pytest

TESTING_DIR = Path(__file__).resolve().parents[1] / "Testing"


def _fixture_or_skip(name: str) -> Path:
    p = TESTING_DIR / name
    if not p.exists():
        pytest.skip(f"fixture not present: {p}")
    return p


@pytest.fixture
def clean_pdf() -> Path:
    return _fixture_or_skip("clean.pdf")


@pytest.fixture
def table_heavy_pdf() -> Path:
    return _fixture_or_skip("table_heavy.pdf")


@pytest.fixture
def encrypted_pdf() -> Path:
    return _fixture_or_skip("encrypted.pdf")


@pytest.fixture
def long_book_pdf() -> Path:
    return _fixture_or_skip("long_book.pdf")


@pytest.fixture
def scanned_pdf() -> Path:
    return _fixture_or_skip("scanned.pdf")


@pytest.fixture
def tmp_output(tmp_path) -> Path:
    out = tmp_path / "output"
    out.mkdir()
    return out
