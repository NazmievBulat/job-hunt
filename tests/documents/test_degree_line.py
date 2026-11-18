"""The field of study is printed after the school unless the degree's name already says it."""

import json
from pathlib import Path

import pytest

from resume_kit.documents.degree_line import printed_field_of_study

# Shared with the browser's test.
CASES = json.loads(
    (Path(__file__).resolve().parents[2] / "examples/degree-lines.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case['degree']}|{case['field']}")
def test_the_field_is_printed_unless_the_degree_names_it(case: dict[str, str | None]) -> None:
    assert printed_field_of_study(case["degree"] or "", case["field"]) == case["printed"]


def test_the_cases_cover_what_the_ticket_names() -> None:
    printed = {(case["degree"], case["field"]): case["printed"] for case in CASES}

    assert printed["Bachelor of Science in Computer Science", "Computer Science"] is None
    assert printed["Bachelor of Science", "Computer Science"] == "Computer Science"
    assert printed["Bachelor of Science", None] is None
    assert printed["", "Computer Science"] == "Computer Science"
    assert printed["Bachelor of Sciences", "Science"] == "Science"
