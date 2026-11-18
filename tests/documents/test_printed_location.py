"""The approved explicit-US-only display rule is shared by every resume output."""

import json
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader

from resume_kit.contracts.resume import Resume
from resume_kit.documents import pdf
from resume_kit.documents.docx import render_docx
from resume_kit.documents.printed_location import (
    _US_STATES,  # pyright: ignore[reportPrivateUsage]
    printed_location,
)

_ROOT = Path(__file__).resolve().parents[2]
_CASES = json.loads((_ROOT / "examples/printed-locations.json").read_text())


@pytest.mark.parametrize("case", _CASES)
def test_shared_location_rule(case: dict[str, str | None]) -> None:
    assert printed_location(case["input"]) == case["printed"]


def test_state_table_matches_the_browser() -> None:
    states: dict[str, str] = json.loads(
        (_ROOT / "examples/packages-ui/us-states.json").read_text()
    )
    for name, code in states.items():
        assert printed_location(f"Example City, {name}, US") == f"Example City, {code}"
    assert len(states) == 51
    # Nothing more either: a territory only one side knows would print differently.
    assert {name.lower(): code for name, code in states.items()} == _US_STATES


def _resume() -> Resume:
    return Resume.model_validate_json(
        (_ROOT / "examples/resume-page/long-locations.json").read_text()
    )


def _pdf_text(resume: Resume) -> str:
    return "\n".join(
        page.extract_text() for page in PdfReader(BytesIO(pdf.render_pdf(resume))).pages
    )


@pytest.mark.parametrize(
    ("location", "printed"),
    [
        ("Seattle, Washington, United States", "Seattle, WA"),
        ("Seattle, Washington, United  States", "Seattle, WA"),
        ("Durham, North  Carolina, United States", "Durham, NC"),
        ("Durham, North Carolina, United States", "Durham, NC"),
        ("Loveland, Colorado, United States", "Loveland, CO"),
        ("Seattle, WA", "Seattle, WA"),
        (None, None),
        ("Remote (United States)", "Remote (United States)"),
        ("Paris", "Paris"),
        ("London, England, United Kingdom", "London, England, United Kingdom"),
        (
            "Seattle (Hybrid), Washington, United States",
            "Seattle (Hybrid), Washington, United States",
        ),
    ],
)
def test_pdf_and_word_agree_and_leave_the_saved_strings(
    location: str | None, printed: str | None
) -> None:
    data = _resume().model_dump(mode="json")
    data["person"]["location"] = location
    for job in data["professional_experience"]:
        job["location"] = location
    resume = Resume.model_validate(data)
    before = resume.model_dump_json()
    pdf_text = " ".join(_pdf_text(resume).split())
    word = Document(BytesIO(render_docx(resume)))
    word_text = " ".join(paragraph.text for paragraph in word.paragraphs)
    if printed:
        # Once in contacts and once in each of the five jobs.
        assert pdf_text.count(printed) == 6
        assert word_text.count(printed) == 6
    else:
        assert "None" not in pdf_text + word_text
    if location and printed != location:
        assert location not in pdf_text
        assert location not in word_text
    assert resume.model_dump_json() == before


def _full_location(location: str | None) -> str | None:
    return location


def test_anonymized_long_headers_wrap_less_in_the_actual_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume = _resume()
    assert [
        (len(job.company_name), len(job.work_title)) for job in resume.professional_experience
    ] == [(18, 38), (2, 35), (6, 53), (29, 54), (12, 47)]
    compact = _pdf_text(resume)
    monkeypatch.setattr(pdf, "printed_location", _full_location)
    full = _pdf_text(resume)
    # Compare actual extracted lines, preserving the same fonts, margins, employer, title and dates.
    assert len(compact.splitlines()) < len(full.splitlines())
    for location in ("Seattle, WA", "Durham, NC", "Loveland, CO"):
        assert location in compact
