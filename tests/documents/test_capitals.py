# type: ignore[reportMissingTypeStubs]
"""The name, an employer and the headline are printed in capitals, the same in both file formats.

Which letters have a capital is Python's own list and grows from one version to the next: Python
3.14 knows capitals for "ƛ" and "ɤ" that no font here holds, and every Python knows one for "ↄ".
Such a letter is printed as it is. The expected texts are spelled out, never made with `upper()`,
so that these tests say the same on every Python.
"""

import json
import zipfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest
from lxml import etree
from pypdf import PdfReader

from resume_kit.contracts.resume import Resume
from resume_kit.documents.docx import render_docx
from resume_kit.documents.pdf import render_pdf

ROOT = Path(__file__).resolve().parents[2]
WORD_TEXT = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"


def _pdf_text(resume: Resume) -> str:
    reader = PdfReader(BytesIO(render_pdf(resume)))
    return "\n".join(page.extract_text() for page in reader.pages)


def _docx_text(resume: Resume) -> str:
    with zipfile.ZipFile(BytesIO(render_docx(resume))) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    return " ".join(node.text or "" for node in root.iter(WORD_TEXT))


@pytest.mark.parametrize("render", [_pdf_text, _docx_text], ids=["pdf", "docx"])
def test_a_letter_whose_capital_the_fonts_lack_is_printed_as_it_is(
    render: Callable[[Resume], str],
) -> None:
    data = json.loads((ROOT / "examples/resume.example.json").read_text(encoding="utf-8"))
    data["person"]["name"] = "Maƛa ɤↄ"
    data["professional_experience"][0]["company_name"] = "ƛiisin ɤↄ"
    data["summary"]["text"] = "Engineer ƛɤↄ"

    text = render(Resume.model_validate(data))

    assert "MAƛA ɤↄ" in text
    assert "ƛIISIN ɤↄ" in text
    assert "ENGINEER ƛɤↄ" in text
