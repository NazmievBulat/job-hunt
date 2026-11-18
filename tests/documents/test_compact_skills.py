"""The opt-in layout keeps all skill groups while sharing their printed lines."""

from datetime import UTC, datetime
from hashlib import sha256
from html import escape
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from pypdf import PdfReader
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph

from resume_kit import features
from resume_kit.contracts.accounts import Account
from resume_kit.contracts.resume import Resume, TechnicalSkill, TechnicalSkills
from resume_kit.documents.docx import render_docx
from resume_kit.documents.pdf import render_pdf
from resume_kit.features import read_features

_GROUPS = (
    ("Programming", ["Python", "TypeScript"]),
    ("Agentic AI", ["LangGraph", "CrewAI"]),
    ("RAG", ["pgvector", "Embeddings"]),
    ("Evaluation", ["Pytest", "Evals"]),
    ("Cloud", ["AWS", "Docker"]),
    ("Delivery", ["GitHub Actions", "Git"]),
    ("Services", ["FastAPI", "PostgreSQL"]),
    ("Agent Security", ["Guardrails", "Sandboxing"]),
)
# The groups and counts of a real resume; the skills are made up.
_TWELVE = tuple(
    (title, [f"{title.split()[0].lower()}{index}" for index in range(1, count + 1)])
    for title, count in (
        ("Languages", 4),
        ("Agent Frameworks", 4),
        ("Agent Capabilities", 4),
        ("Retrieval", 5),
        ("Evaluation", 4),
        ("APIs", 4),
        ("Event Systems", 5),
        ("Workflow Reliability", 3),
        ("AWS Delivery", 4),
        ("Delivery", 4),
        ("Observability", 5),
        ("Engineering", 4),
    )
)
_BOTH = pytest.mark.parametrize("groups", [_GROUPS, _TWELVE], ids=["eight", "twelve"])
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _clock() -> datetime:
    return datetime(2026, 10, 25, tzinfo=UTC)


def _resume(groups: tuple[tuple[str, list[str]], ...] = _GROUPS) -> Resume:
    resume = Resume.model_validate_json(
        (Path(__file__).resolve().parents[2] / "examples/resume.example.json").read_text()
    )
    return resume.model_copy(
        update={
            "technical_skills": TechnicalSkills(
                skills=[TechnicalSkill(title=title, bullets=items) for title, items in groups]
            )
        }
    )


def _flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(
        features,
        "_current",
        read_features({"RESUME_KIT_FEATURE_COMPACT_SKILLS": "on" if enabled else "off"}),
    )


def _pdf_skills(data: bytes) -> tuple[str, int, list[str]]:
    reader = PdfReader(BytesIO(data))
    text = "\n".join(page.extract_text() for page in reader.pages)
    section = text.split("SKILLS\n", 1)[1].split("PROFESSIONAL EXPERIENCE", 1)[0].strip()
    bold: list[str] = []

    def visit(
        text: str,
        _cm: list[float],
        _tm: list[float],
        font: dict[str, object] | None,
        _size: float,
    ) -> None:
        if font is not None and "NotoSans-Bold" in str(font.get("/BaseFont", "")):
            bold.append(text)

    for page in reader.pages:
        page.extract_text(visitor_text=visit)
    return section, len(section.splitlines()), bold


def _docx_skills(data: bytes) -> list[ET.Element]:
    with ZipFile(BytesIO(data)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(f"{_W}body")
    assert body is not None
    result: list[ET.Element] = []
    in_skills = False
    for para in body.findall(f"{_W}p"):
        text = "".join(para.itertext())
        if text == "SKILLS":
            in_skills = True
        elif text == "PROFESSIONAL EXPERIENCE":
            break
        elif in_skills:
            result.append(para)
    return result


def _word_line_count(data: bytes) -> int:
    # Word's Arial and the PDF core Helvetica face have compatible Latin advance widths.
    # Measure the serialized sizes, bold runs and available width, so a font-size or margin
    # regression cannot pass just because the OOXML still contains a single paragraph.
    with ZipFile(BytesIO(data)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    page = root.find(f".//{_W}sectPr/{_W}pgSz")
    margins = root.find(f".//{_W}sectPr/{_W}pgMar")
    assert page is not None and margins is not None
    width = (
        int(page.attrib[f"{_W}w"])
        - int(margins.attrib[f"{_W}left"])
        - int(margins.attrib[f"{_W}right"])
    ) / 20
    lines = 0
    for paragraph in _docx_skills(data):
        indent = paragraph.find(f"{_W}pPr/{_W}ind")
        inset = (
            0
            if indent is None
            else sum(int(indent.get(f"{_W}{side}", "0")) for side in ("left", "right", "firstLine"))
            / 20
        )
        markup: list[str] = []
        for run in paragraph.findall(f"{_W}r"):
            font = run.find(f"{_W}rPr/{_W}rFonts")
            size = run.find(f"{_W}rPr/{_W}sz")
            assert font is not None and font.get(f"{_W}ascii") == "Arial"
            assert size is not None
            points = int(size.attrib[f"{_W}val"]) / 2
            mark = run.find(f"{_W}rPr/{_W}b")
            bold = mark is not None and mark.get(f"{_W}val", "1") != "0"
            face = "Helvetica-Bold" if bold else "Helvetica"
            markup.append(
                f'<font name="{face}" size="{points}">{escape("".join(run.itertext()))}</font>'
            )
        measured = Paragraph("".join(markup), ParagraphStyle("measure", leading=10))
        _, height = measured.wrap(width - inset, 10000)
        lines += round(height / 10)
    return lines


def test_compact_flag_is_on_and_told_to_the_web_app() -> None:
    # The resume page draws the skills as the files print them, so it must know the value.
    flag = next(flag for flag in features.FLAGS if flag.name == "compact_skills")
    assert flag.default
    assert flag.web
    values = read_features({})
    assert values.on("compact_skills")
    assert values.for_web()["compact_skills"]
    off = read_features({"RESUME_KIT_FEATURE_COMPACT_SKILLS_OFF_FOR": "a@example.com"})
    assert not off.for_web(Account(id="1", email="a@example.com", name="A"))["compact_skills"]


@_BOTH
def test_pdf_groups_share_lines_and_keep_every_bold_title(
    monkeypatch: pytest.MonkeyPatch, groups: tuple[tuple[str, list[str]], ...]
) -> None:
    resume = _resume(groups)
    _flag(monkeypatch, False)
    _, old_lines, _ = _pdf_skills(render_pdf(resume, clock=_clock))
    _flag(monkeypatch, True)
    text, lines, bold = _pdf_skills(render_pdf(resume, clock=_clock))
    assert old_lines == len(groups)
    assert 1 < lines < old_lines  # Content wraps naturally, with no forced group breaks.
    plain = " ".join(text.split())
    assert plain == " · ".join(f"{title}: {' · '.join(items)}" for title, items in groups)
    # A name of two words may wrap between them, as the page wraps it.
    bold_text = "".join(bold).replace("\n", " ")
    for title, _ in groups:
        assert f"{title}:" in bold_text


@_BOTH
def test_word_groups_share_one_paragraph_and_keep_every_bold_title(
    monkeypatch: pytest.MonkeyPatch, groups: tuple[tuple[str, list[str]], ...]
) -> None:
    resume = _resume(groups)
    _flag(monkeypatch, False)
    original = render_docx(resume, clock=_clock)
    assert len(_docx_skills(original)) == len(groups)
    assert _word_line_count(original) == len(groups)
    _flag(monkeypatch, True)
    compact = render_docx(resume, clock=_clock)
    paragraphs = _docx_skills(compact)
    assert 1 < _word_line_count(compact) < len(groups)
    assert len(paragraphs) == 1
    paragraph = paragraphs[0]
    assert not paragraph.findall(f".//{_W}br")
    assert "".join(paragraph.itertext()) == " · ".join(
        f"{title}: {' · '.join(items)}" for title, items in groups
    )
    runs = paragraph.findall(f"{_W}r")
    bold = [
        "".join(run.itertext())
        for run in runs
        if (mark := run.find(f"{_W}rPr/{_W}b")) is not None
        and mark.get(f"{_W}val", "1") not in {"0", "false", "off"}
    ]
    assert bold == [f"{title}:" for title, _ in groups]


@pytest.mark.parametrize("enabled", [False, True])
def test_empty_and_single_group_keep_existing_behavior(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    _flag(monkeypatch, enabled)
    empty = _resume(())
    pdf_text = "".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(empty))).pages)
    assert "SKILLS" not in pdf_text
    assert not _docx_skills(render_docx(empty))
    single = _resume((("Tools & <systems>", ["C++", "R&D"]),))
    text, lines, bold = _pdf_skills(render_pdf(single))
    assert text == "Tools & <systems>: C++ · R&D"
    assert lines == 1
    assert "Tools & <systems>:" in "".join(bold)
    paragraphs = _docx_skills(render_docx(single))
    assert len(paragraphs) == 1
    assert "".join(paragraphs[0].itertext()) == text


def test_flag_off_matches_print_revision_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Printing revision 11: the eight separate skill groups remain unchanged with the flag off.
    _flag(monkeypatch, False)
    resume = _resume()
    assert (
        sha256(render_pdf(resume, clock=_clock)).hexdigest()
        == "9ba982386c3bce7f50c762a9bb1058eae346acb7da323c9f7dc47c0ca7d331e6"
    )
    assert (
        sha256(render_docx(resume, clock=_clock)).hexdigest()
        == "9272ab2c8e9a19f6ca00abc9cff3b83899418189a3d33692ebb9b617279ce2d9"
    )


def test_compact_skills_can_span_pdf_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    _flag(monkeypatch, True)
    groups = tuple(
        (f"Group {index}", ["Python", "TypeScript", "PostgreSQL"]) for index in range(200)
    )
    resume = _resume(groups)
    pages = PdfReader(BytesIO(render_pdf(resume))).pages
    assert len(pages) > 1
    assert "SKILLS" in pages[0].extract_text()
    assert "Group 0:" in pages[0].extract_text()
    text = " ".join(" ".join(page.extract_text().split()) for page in pages)
    for title, _ in groups:
        assert f"{title}:" in text
    for item in ("Python", "TypeScript", "PostgreSQL"):
        assert text.count(item) == len(groups)
