"""How a link's address is printed on the contact line of the PDF and the Word file.

The cases in `examples/printed-addresses.json` are shared with the resume page's tests
, so the page and the files print one address
the same way.
"""

import json
from pathlib import Path

import pytest

from resume_kit.contracts.resume import Link
from resume_kit.documents.printed_address import printed_address

ROOT = Path(__file__).resolve().parents[2]
CASES: list[dict[str, str]] = json.loads(
    (ROOT / "examples/printed-addresses.json").read_text("utf-8")
)


def test_the_shared_cases_are_more_than_a_handful() -> None:
    assert len(CASES) > 10


@pytest.mark.parametrize("case", CASES, ids=[case["case"] for case in CASES])
def test_an_address_is_printed_as_the_page_prints_it(case: dict[str, str]) -> None:
    assert printed_address(case["address"]) == case["printed"]


@pytest.mark.parametrize(
    ("typed", "printed"),
    [
        ("anapena.es", "anapena.es"),
        ("www.linkedin.com/in/ana-pena-rn/", "linkedin.com/in/ana-pena-rn"),
        ("https://GitHub.com/john-doe", "github.com/john-doe"),
    ],
)
def test_an_address_as_a_person_saves_it_is_printed_short(typed: str, printed: str) -> None:
    # A saved link holds the address as the contract stores it, not as it was typed.
    link = Link.model_validate({"label": "Website", "url": typed})
    assert printed_address(str(link.url)) == printed
