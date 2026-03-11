from pathlib import Path

from molde_maestro import pipeline


def test_repo_has_no_tracked_hygiene_violations() -> None:
    repo = Path(__file__).resolve().parents[1]
    assert pipeline.tracked_hygiene_violations(repo) == []
