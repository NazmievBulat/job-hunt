# type: ignore[reportMissingTypeStubs]
"""A resume section the candidate has nothing for is left off the page, in both file formats."""

import json
import re
import zipfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest
from lxml import etree
from pypdf import PdfReader

from resume_kit.contracts.resume import Resume
from resume_kit.documents.docx import render_docx, validate_docx
from resume_kit.documents.pdf import render_pdf

ROOT = Path(__file__).resolve().parents[2]
WORD_TEXT = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
WORD_PARAGRAPH = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"

MASTERS = {
    "school_name": "Stanford University",
    "degree": "Master of Science",
    "field_of_study": "Statistics",
    "start_date": "2014-09",
    "end_date": "2016-06",
    "bullets": [],
}


def _pdf_text(resume: Resume) -> str:
    reader = PdfReader(BytesIO(render_pdf(resume)))
    return "\n".join(page.extract_text() for page in reader.pages)


def _docx_text(resume: Resume) -> str:
    data = render_docx(resume)
    assert validate_docx(data) == {"valid": True, "issues": []}
    with zipfile.ZipFile(BytesIO(data)) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    return " ".join(node.text or "" for node in root.iter(WORD_TEXT))


Render = Callable[[Resume], str]
both_formats = pytest.mark.parametrize("render", [_pdf_text, _docx_text], ids=["pdf", "docx"])


def _resume(**changes: object) -> Resume:
    """The example resume (one degree, one certification) with some sections replaced."""
    data = json.loads((ROOT / "examples/resume.example.json").read_text(encoding="utf-8"))
    data.update(changes)
    return Resume.model_validate(data)


NO_CERTIFICATIONS = {"certifications": []}


@both_formats
def test_the_example_still_shows_its_education_and_certification(render: Render) -> None:
    text = render(_resume())

    assert "EDUCATION & CREDENTIALS" in text
    assert "University of California, Los Angeles" in text
    assert "AWS Certified Developer" in text


@both_formats
def test_no_education_and_no_certifications_means_no_section(render: Render) -> None:
    text = render(_resume(education=[], certifications=NO_CERTIFICATIONS))

    assert "EDUCATION" not in text
    assert "CREDENTIALS" not in text
    assert "PROFESSIONAL EXPERIENCE" in text  # the rest of the page is still there


@both_formats
def test_certifications_are_shown_without_any_education(render: Render) -> None:
    text = render(_resume(education=[]))

    assert "EDUCATION & CREDENTIALS" in text
    assert "AWS Certified Developer" in text
    assert "University of California" not in text


def _certifications(*lines: str) -> dict[str, list[str]]:
    return {"certifications": list(lines)}


@both_formats
def test_a_certification_month_is_printed_the_way_a_role_date_is(render: Render) -> None:
    # The model writes a date with its month as "2023-03". A role's date is printed as
    # "Mar 2023", and a date inside a certification line reads the same on the page.
    text = render(
        _resume(
            certifications=_certifications(
                "AWS Certified Solutions Architect, Amazon Web Services, 2023-03",
                "Certified Kubernetes Administrator (2021-11)",
            )
        )
    )

    assert re.search(r"Amazon Web Services,\s+Mar\s+2023", text)
    assert re.search(r"Certified Kubernetes Administrator\s+\(Nov\s+2021\)", text)
    assert "2023-03" not in text
    assert "2021-11" not in text


@both_formats
def test_a_certification_with_a_year_or_no_date_is_printed_as_written(render: Render) -> None:
    text = render(
        _resume(certifications=_certifications("PMP, PMI, 2021", "Certified Scrum Master"))
    )

    assert re.search(r"PMP, PMI,\s+2021\b", text)
    assert "Certified Scrum Master" in text


@both_formats
@pytest.mark.parametrize(
    "line",
    [
        "ISO/IEC 27001-2013 Lead Auditor",  # a standard's number, not a date
        "Safety course 2023-03-15",  # a full date is left as the person wrote it
        "Programme 2020-2021",  # a range of years
        "Exam 2023-13",  # there is no thirteenth month
        "Licence A2023-03",  # part of a longer code
    ],
)
def test_only_a_year_and_month_is_rewritten_in_a_certification(render: Render, line: str) -> None:
    text = render(_resume(certifications=_certifications(line)))

    assert re.sub(r"\s+", " ", line) in re.sub(r"\s+", " ", text)


@both_formats
def test_every_education_entry_is_shown_in_the_order_given(render: Render) -> None:
    example = json.loads((ROOT / "examples/resume.example.json").read_text(encoding="utf-8"))
    text = render(_resume(education=[MASTERS, *example["education"]]))

    assert text.count("EDUCATION & CREDENTIALS") == 1
    assert "Master of Science" in text
    assert "Stanford University" in text
    assert "Sep 2014" in text
    assert text.index("Stanford University") < text.index("University of California")
    assert "Graduated with honors." in text


@both_formats
def test_an_education_entry_without_bullets_is_shown(render: Render) -> None:
    text = render(_resume(education=[MASTERS], certifications=NO_CERTIFICATIONS))

    assert "Master of Science" in text
    assert "Statistics" in text


@both_formats
def test_education_with_a_start_and_no_end_is_printed_as_still_going_on(render: Render) -> None:
    # Same rule as for a job, and the same words in both files: no end date means "Present".
    unfinished = {**MASTERS, "end_date": None}

    text = render(_resume(education=[unfinished]))

    assert re.search(r"Sep 2014\s*[-\u2013]\s*Present", text)


def _pdf_lines(resume: Resume) -> list[str]:
    return [line.strip() for line in _pdf_text(resume).splitlines()]


def _docx_paragraphs(resume: Resume) -> list[str]:
    with zipfile.ZipFile(BytesIO(render_docx(resume))) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    return [
        "".join(node.text or "" for node in p.iter(WORD_TEXT)) for p in root.iter(WORD_PARAGRAPH)
    ]


@pytest.mark.parametrize("lines", [_pdf_lines, _docx_paragraphs], ids=["pdf", "docx"])
def test_the_skills_heading_is_skills_and_names_no_trade(
    lines: Callable[[Resume], list[str]],
) -> None:
    printed = lines(_resume())

    assert "SKILLS" in printed  # a line of its own: the heading, word for word
    assert not any("TECHNICAL" in line for line in printed)  # a nurse's skills are not technical


@both_formats
def test_no_skills_means_no_skills_section(render: Render) -> None:
    text = render(_resume(technical_skills={"skills": []}))

    assert "SKILLS" not in text
    assert "SUMMARY" in text
    assert "PROFESSIONAL EXPERIENCE" in text


@both_formats
def test_a_resume_with_only_a_name_and_a_summary_still_renders(render: Render) -> None:
    text = render(
        _resume(
            technical_skills={"skills": []},
            professional_experience=[],
            education=[],
            certifications=NO_CERTIFICATIONS,
        )
    )

    assert "JOHN DOE" in text
    assert "SUMMARY" in text
    for heading in ("SKILLS", "PROFESSIONAL EXPERIENCE", "EDUCATION"):
        assert heading not in text


SUMMARY_TEXT = "Software engineer who builds reliable web applications."


@both_formats
def test_a_cleared_summary_takes_its_heading_with_it(render: Render) -> None:
    text = render(_resume(summary={"text": "Software Engineer"}))

    assert "SUMMARY" not in text
    assert SUMMARY_TEXT not in text
    assert "SOFTWARE ENGINEER" in text  # the headline is still printed
    assert "SKILLS" in text  # and so is the rest of the page


@both_formats
def test_a_cleared_headline_leaves_the_summary_in_place(render: Render) -> None:
    with_headline = render(_resume())
    without = render(_resume(summary={"summary_description": SUMMARY_TEXT}))

    assert SUMMARY_TEXT in without
    assert "SUMMARY" in without
    # The headline is printed in capitals once in the Word file, and once more in the PDF's
    # footer in its own case; every one of those is gone.
    assert without.count("SOFTWARE ENGINEER") == with_headline.count("SOFTWARE ENGINEER") - 1
    assert "John Doe · Software Engineer" not in without


def test_a_pdf_without_a_headline_is_titled_with_the_name_alone() -> None:
    resume = _resume(summary={"summary_description": SUMMARY_TEXT})

    reader = PdfReader(BytesIO(render_pdf(resume)))

    assert reader.metadata is not None
    assert reader.metadata.title == "John Doe"
    assert reader.metadata.subject == "Resume"


@both_formats
def test_a_field_the_degree_already_names_is_printed_once(render: Render) -> None:
    named = {**MASTERS, "degree": "Master of Science in Statistics", "field_of_study": "statistics"}
    text = render(_resume(education=[named]))

    assert re.search(r"Master of Science in Statistics\s*— Stanford University", text)
    assert text.lower().count("statistics") == 1


@both_formats
def test_a_field_the_degree_does_not_name_is_printed_after_the_school(render: Render) -> None:
    text = render(_resume(education=[MASTERS]))

    assert re.search(r"Stanford University\s*·\s*Statistics", text)
