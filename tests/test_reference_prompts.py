import hashlib
from pathlib import Path


def test_independent_reference_prompt_hashes_are_frozen() -> None:
    prompt_dir = Path(__file__).parents[1] / "prompts" / "reference"
    expected = {
        "upos-independent-alpha-v1.txt": (
            "bfd9ae0e238564ca0552b08e757a13fb66e3d7ae2de7437766829d0808e07381"
        ),
        "upos-independent-beta-v1.txt": (
            "2ab23d2ae5361ff985929de4c75de6df1d8932868498fcc04ff034a8dfb616db"
        ),
        "upos-independent-gamma-v1.txt": (
            "7a273546197734f471bd7699388d62ff3d2537e13d9e0a55f197166985bed191"
        ),
        "upos-independent-delta-v1.txt": (
            "656656fd5ce5d9741f5c481165e56fde74998e761e8ec769a1c879fe7de71213"
        ),
    }

    actual = {
        name: hashlib.sha256((prompt_dir / name).read_bytes()).hexdigest()
        for name in expected
    }

    assert actual == expected
