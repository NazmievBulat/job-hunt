"""The verification address ending a certification line, as the PDF and the Word file print it."""

import json
from io import BytesIO
from pathlib import Path

from docx import Document
from pypdf import PdfReader

from resume_kit.contracts.resume import Resume
from resume_kit.contracts.resume_language import printed_date
from resume_kit.documents.certification_dates import printed_certification
from resume_kit.documents.docx import render_docx
from resume_kit.documents.pdf import render_pdf

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/resume.example.json"


def _printed(line: str) -> tuple[str, tuple[str, str] | None]:
    return printed_certification(line, lambda date: printed_date(date, "en"), "Credential ID")


def test_the_address_code_adds_is_printed_as_a_contact_link_and_its_months_left_alone() -> None:
    line = "Permit — 2024-06 — Credential ID BHA-1 — https://www.permits.example/2024-06/BHA-1"

    assert _printed(line) == (
        "Permit — Jun 2024 — Credential ID BHA-1 — ",
        ("permits.example/2024-06/BHA-1", "https://www.permits.example/2024-06/BHA-1"),
    )


def test_an_address_that_does_not_end_the_line_after_the_separator_is_text() -> None:
    assert _printed("Course https://school.example/notes, 2024-06") == (
        "Course https://school.example/notes, Jun 2024",
        None,
    )
    assert _printed("Course, https://school.example/notes") == (
        "Course, https://school.example/notes",
        None,
    )


def test_a_credential_id_is_printed_as_it_was_saved_even_when_it_reads_as_a_month() -> None:
    assert _printed("Permit, 2023-03 — Credential ID 2024-06") == (
        "Permit, Mar 2023 — Credential ID 2024-06",
        None,
    )
    assert _printed("Permit — Credential ID 2024-06 — https://permits.example/v") == (
        "Permit — Credential ID 2024-06 — ",
        ("permits.example/v", "https://permits.example/v"),
    )


def test_two_certifications_keep_their_dot_and_only_the_address_is_a_link() -> None:
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["certifications"] = {
        "certifications": [
            "Permit — Credential ID BHA-1 — https://permits.example/v/BHA-1",
            "First Aid, 2023-03",
        ]
    }
    resume = Resume.model_validate(data)
    printed = "Permit — Credential ID BHA-1 — permits.example/v/BHA-1 · First Aid, Mar 2023"

    pdf = PdfReader(BytesIO(render_pdf(resume)))
    assert printed in " ".join(" ".join(page.extract_text() for page in pdf.pages).split())
    links = [
        annotation.get_object()["/A"]["/URI"]
        for page in pdf.pages
        for annotation in page.get("/Annots", [])
    ]
    assert links.count("https://permits.example/v/BHA-1") == 1
    document = Document(BytesIO(render_docx(resume)))
    (paragraph,) = [item for item in document.paragraphs if "Permit" in item.text]
    assert paragraph.text == f"Certifications: {printed}"
    targets = [
        relation.target_ref
        for relation in document.part.rels.values()
        if relation.reltype.endswith("/hyperlink")
    ]
    assert targets.count("https://permits.example/v/BHA-1") == 1
