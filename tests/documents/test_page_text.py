"""The words on the PDF page, in order, are the words written in `examples/resume-page/`.

The preview in the browser is held to the same files by
the web page's copy, so the PDF and the preview cannot come to say
different things unnoticed. White space is ignored: the PDF wraps lines where the browser does not.
"""

import re
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from resume_kit.contracts.resume import Resume
from resume_kit.contracts.resume_language import PRINTED_WORDS
from resume_kit.documents.pdf import render_pdf

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
PAGES = [
    ("resume.example.json", "resume-page/full.txt"),
    ("resume-page/cleared.json", "resume-page/cleared.txt"),
    ("resume-page/german.json", "resume-page/german.txt"),
]


def _squeezed(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _page_text(resume: Resume) -> str:
    """Everything printed on the page except the bullet marks and the footer."""
    footer = " · ".join(part for part in (resume.person.name, resume.summary.text) if part)
    reader = PdfReader(BytesIO(render_pdf(resume)))
    lines = [line.strip() for page in reader.pages for line in page.extract_text().splitlines()]
    page = PRINTED_WORDS[resume.language].page
    kept = [line for line in lines if line != footer and not re.fullmatch(rf"{page} \d+", line)]
    return "\n".join(kept).replace("•", "")


@pytest.mark.parametrize(("resume_file", "text_file"), PAGES)
def test_the_pdf_says_what_the_page_text_file_says(resume_file: str, text_file: str) -> None:
    resume = Resume.model_validate_json((EXAMPLES / resume_file).read_text(encoding="utf-8"))
    expected = (EXAMPLES / text_file).read_text(encoding="utf-8")

    assert _squeezed(_page_text(resume)) == _squeezed(expected)
