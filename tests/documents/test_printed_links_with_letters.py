"""A web address with letters beyond ASCII prints them on the PDF and Word, its link stays valid."""

from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfReader

from resume_kit.contracts.resume import Resume
from resume_kit.documents.docx import render_docx
from resume_kit.documents.pdf import render_pdf


def _resume(url: str) -> Resume:
    return Resume.model_validate(
        {
            "person": {
                "name": "Józef Kowalski",
                "email": "jozef@example.com",
                "links": [{"url": url, "label": "LinkedIn"}],
            },
            "summary": {"text": "Engineer", "summary_description": "Builds services."},
            "technical_skills": {"skills": []},
            "professional_experience": [],
            "education": [],
            "certifications": {"certifications": []},
        }
    )


@pytest.mark.parametrize(
    ("typed", "printed"),
    [
        ("https://www.linkedin.com/in/józef-kowalski", "linkedin.com/in/józef-kowalski"),
        ("müller-design.de", "müller-design.de"),
    ],
)
def test_a_link_is_printed_as_the_person_typed_it(typed: str, printed: str) -> None:
    resume = _resume(typed)

    pdf_text = "".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(resume))).pages)
    word_text = "\n".join(p.text for p in Document(BytesIO(render_docx(resume))).paragraphs)

    assert printed in pdf_text, f"PDF: {[x for x in pdf_text.splitlines() if '@' in x]}"
    assert printed in word_text, f"Word: {[x for x in word_text.splitlines() if '@' in x]}"


@pytest.mark.parametrize(
    ("typed", "target"),
    [
        (
            "https://www.linkedin.com/in/józef-kowalski",
            "https://www.linkedin.com/in/j%C3%B3zef-kowalski",
        ),
        ("müller-design.de", "https://xn--mller-design-dlb.de/"),
    ],
)
def test_the_link_behind_the_printed_letters_stays_the_valid_address(
    typed: str, target: str
) -> None:
    resume = _resume(typed)

    pdf_targets = [
        annotation.get_object()["/A"]["/URI"]
        for page in PdfReader(BytesIO(render_pdf(resume))).pages
        for annotation in page.get("/Annots", [])
    ]
    word_targets = [
        relation.target_ref
        for relation in Document(BytesIO(render_docx(resume))).part.rels.values()
        if relation.is_external
    ]

    assert pdf_targets == [target]
    assert word_targets == [target]


def test_a_link_naming_a_noncharacter_keeps_it_encoded_and_the_files_are_made() -> None:
    # U+FFFF is in the PDF's fonts, but the XML of a Word file has no place for it.
    resume = _resume("https://example.com/a%EF%BF%BF")

    pdf_text = "".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(resume))).pages)
    word_text = "\n".join(p.text for p in Document(BytesIO(render_docx(resume))).paragraphs)

    assert "example.com/a%EF%BF%BF" in pdf_text
    assert "example.com/a%EF%BF%BF" in word_text
