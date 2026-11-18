# type: ignore[reportMissingTypeStubs, reportAttributeAccessIssue, reportIndexIssue]
"""Tests for the Python DOCX resume renderer."""

import json
import zipfile
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from docx.shared import Pt, RGBColor, Twips
from lxml import etree
from movable_clock import MovableClock
from pypdf import PdfReader

from resume_kit.contracts.resume import Resume
from resume_kit.documents.docx import (
    DocxStyle,
    render_docx,
    validate_docx,
    with_docx_properties,
)
from resume_kit.documents.pdf import render_pdf

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures" / "documents"
REPOSITORY_ROOT = Path(__file__).parents[2]


# ── helpers ──────────────────────────────────────────────────────────────


def _minimal_resume_data() -> dict[str, object]:
    return {
        "person": {
            "name": "John Doe",
            "email": "john.doe@example.com",
            "phone": "+1 555 123 4567",
            "links": [
                {"label": "GitHub", "url": "https://github.com/john-doe"},
            ],
            "location": "New York, NY",
        },
        "summary": {
            "text": "Software Engineer",
            "summary_description": "Software engineer who builds reliable web applications.",
        },
        "technical_skills": {
            "skills": [
                {"title": "Languages", "bullets": ["JavaScript", "TypeScript", "Python"]},
                {"title": "Frameworks", "bullets": ["FastAPI", "LangGraph", "React"]},
            ],
        },
        "professional_experience": [
            {
                "work_title": "Software Engineer",
                "company_name": "Example Inc.",
                "location": "New York, NY",
                "start_date": "2020-01",
                "end_date": None,
                "bullets": [
                    "Built a web application used by thousands of customers.",
                    "Improved application performance and reliability.",
                ],
            },
        ],
        "education": [
            {
                "school_name": "University of California, Los Angeles",
                "degree": "Bachelor of Science",
                "field_of_study": "Computer Science",
                "start_date": "2010-01",
                "end_date": "2014-01",
                "bullets": ["Graduated with honors."],
            }
        ],
        "certifications": {
            "certifications": ["AWS Certified Developer"],
        },
    }


def _load_canonical_resume() -> Resume:
    """Load the canonical example resume from the examples directory."""
    example_path = REPOSITORY_ROOT / "examples" / "resume.example.json"
    data = json.loads(example_path.read_text(encoding="utf-8"))
    return Resume.model_validate(data)


def _read_docx_xml(buffer: bytes) -> dict[str, str]:
    """Unzip a DOCX buffer and extract key XML files as strings."""
    with zipfile.ZipFile(BytesIO(buffer)) as zf:
        names = zf.namelist()
        document_xml = zf.read("word/document.xml").decode("utf-8")
        footer_entry = next((n for n in names if n.startswith("word/footer")), None)
        footer_xml = zf.read(footer_entry).decode("utf-8") if footer_entry else ""
        return {
            "document_xml": document_xml,
            "footer_xml": footer_xml,
        }


def _text_from_xml(xml: str) -> str:
    """Extract all w:t text nodes from an OOXML fragment."""
    root = etree.fromstring(xml.encode("utf-8"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    return " ".join(t.text or "" for t in root.iter(f"{{{ns['w']}}}t"))


def _replace_package_part(buffer: bytes, name: str, content: bytes) -> bytes:
    """Repackage with one changed member while retaining a valid ZIP CRC."""
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(buffer)) as source, zipfile.ZipFile(output, "w") as target:
        for member in source.infolist():
            target.writestr(member, content if member.filename == name else source.read(member))
    return output.getvalue()


# ── tests ────────────────────────────────────────────────────────────────


class TestRenderDocx:
    """Test suite for render_docx()."""

    @pytest.mark.parametrize(
        ("name", "headline"),
        [
            ("John Doe", "Software Engineer"),
            ("John Doe", None),
            ("André & María", "R&D <Engineer>"),
            ("Alex\ufe0f Morgan", "Engineer\ufe0f"),
            ("Long name " * 30, "Engineer"),
        ],
    )
    def test_document_properties_belong_to_the_resume(
        self, name: str, headline: str | None
    ) -> None:
        resume = Resume.model_validate(_minimal_resume_data())
        resume = resume.model_copy(
            update={
                "person": resume.person.model_copy(update={"name": name}),
                "summary": resume.summary.model_copy(update={"text": headline}),
            }
        )
        clock = MovableClock(datetime(2031, 4, 5, 10, 20, 30, tzinfo=timezone(timedelta(hours=3))))
        pdf = PdfReader(BytesIO(render_pdf(resume))).metadata
        assert pdf is not None

        for _ in range(2):
            with zipfile.ZipFile(BytesIO(render_docx(resume, clock=clock))) as package:
                core = etree.fromstring(package.read("docProps/core.xml"))
                app = etree.fromstring(package.read("docProps/app.xml"))
            # Check serialized properties, including absence of all inherited template fields.
            properties = {etree.QName(child).localname: child.text for child in core}
            stamp = clock.now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            assert properties == {
                "creator": pdf.author,
                "title": pdf.title,
                "created": stamp,
                "modified": stamp,
            }
            # Word's own prefixes, and the dates' type names a prefix bound to the dates' namespace.
            assert core.prefix == "cp"
            for date in core.findall("{http://purl.org/dc/terms/}*"):
                kind = date.get("{http://www.w3.org/2001/XMLSchema-instance}type")
                assert kind == "dcterms:W3CDTF"
                assert date.prefix == "dcterms"
                assert date.nsmap["dcterms"] == "http://purl.org/dc/terms/"
            assert {etree.QName(child).localname: child.text for child in app} == {
                "Application": "job-hunt"
            }
            document = Document(BytesIO(render_docx(resume, clock=clock)))
            assert document.core_properties.author == pdf.author
            assert document.core_properties.title == pdf.title
            assert document.core_properties.created == clock.now.astimezone(UTC)
            assert document.core_properties.modified == clock.now.astimezone(UTC)
            clock.advance(days=2)

    def test_returns_valid_zip_buffer(self) -> None:
        resume = _load_canonical_resume()
        buffer = render_docx(resume)

        assert isinstance(buffer, bytes)
        assert buffer[:2] == b"PK"
        assert len(buffer) > 0

        # Unzip and check required parts exist
        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            names = zf.namelist()
        assert "[Content_Types].xml" in names
        assert "word/document.xml" in names
        assert any(n.startswith("word/footer") for n in names)
        assert "word/_rels/document.xml.rels" in names

    def test_default_style_is_frozen_and_retains_classic_layout(self) -> None:
        style = DocxStyle()
        assert style.font_name == "Arial"
        assert style.body_size == Pt(9)
        assert style.header_name_size == Pt(19)
        assert style.page_width == Twips(12240)
        assert style.margin_left == Twips(691)
        assert style.muted == RGBColor(0x44, 0x44, 0x44)
        assert style.section_space_before == 10
        assert style.contact_line_spacing == 1.30
        assert {item.name for item in fields(style)} >= {
            "body_size",
            "margin_left",
            "muted",
            "section_space_before",
        }
        with pytest.raises(FrozenInstanceError):
            style.font_name = "Times New Roman"

        buffer = render_docx(_load_canonical_resume())
        document = Document(BytesIO(buffer))
        section = document.sections[0]
        assert section.page_width == style.page_width
        assert section.left_margin == style.margin_left
        assert document.styles["Normal"].font.name == style.font_name
        assert document.styles["Normal"].font.size == style.body_size
        assert document.styles["Normal"].font.color.rgb == style.black
        assert document.paragraphs[0].runs[0].font.size == style.header_name_size
        summary = next(p for p in document.paragraphs if p.text == "SUMMARY")
        assert summary.paragraph_format.space_before == Pt(style.section_space_before)

    def test_custom_style_applies_to_each_render_without_changing_default(self) -> None:
        custom = replace(DocxStyle(), font_name="Georgia", section_space_before=14)
        custom_doc = Document(BytesIO(render_docx(_load_canonical_resume(), style=custom)))
        standard_doc = Document(BytesIO(render_docx(_load_canonical_resume())))
        assert custom_doc.styles["Normal"].font.name == "Georgia"
        assert custom_doc.paragraphs[0].runs[0].font.name == "Georgia"
        summary = next(p for p in custom_doc.paragraphs if p.text == "SUMMARY")
        assert summary.paragraph_format.space_before == Pt(14)
        assert standard_doc.styles["Normal"].font.name == "Arial"

    def test_letter_page_size(self) -> None:
        """DOCX uses Letter (8.5 x 11) page dimensions."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        xml = _read_docx_xml(buffer)["document_xml"]

        # Letter in twips: 12240 x 15840
        assert 'w:w="12240"' in xml
        assert 'w:h="15840"' in xml

    def test_section_headings_rendered(self) -> None:
        """Section heading labels appear uppercase in the document."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "SUMMARY" in text
        assert "SKILLS" in text
        assert "PROFESSIONAL EXPERIENCE" in text
        assert "EDUCATION & CREDENTIALS" in text

    def test_candidate_header_rendered(self) -> None:
        """Candidate name and contact details appear."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "JOHN DOE" in text
        assert "john.doe@example.com" in text
        assert "github.com/john-doe" in text
        assert "GitHub" not in text
        assert "https://" not in text
        assert "New York, NY" in text

    def test_contact_links_have_external_targets_and_readable_text(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            rels = etree.fromstring(zf.read("word/_rels/document.xml.rels"))
            doc = etree.fromstring(zf.read("word/document.xml"))
        pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
        word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        targets = {
            r.get("Id"): r.get("Target")
            for r in rels.findall(f"{{{pkg}}}Relationship")
            if r.get("Type", "").endswith("/hyperlink") and r.get("TargetMode") == "External"
        }
        linked = {
            targets[h.get(f"{{{rel}}}id")]: "".join(h.itertext())
            for h in doc.iter(f"{{{word}}}hyperlink")
        }
        assert "https://github.com/john-doe" in linked
        assert linked == {
            "https://github.com/john-doe": "github.com/john-doe",
            "https://www.linkedin.com/in/john-doe": "linkedin.com/in/john-doe",
        }
        assert "mailto:john.doe@example.com" not in str(targets)
        assert "tel:" not in str(targets)
        assert all(target.startswith(("https://", "http://")) for target in linked)
        contact = next(
            paragraph
            for paragraph in doc.iter(f"{{{word}}}p")
            if any(
                (node.text or "") == "john.doe@example.com"
                for node in paragraph.iter(f"{{{word}}}t")
            )
        )
        plain_runs = [
            node.text
            for run in contact.findall(f"{{{word}}}r")
            for node in run.findall(f"{{{word}}}t")
        ]
        assert "john.doe@example.com" in plain_runs
        assert "+1 555 123 4567" in plain_runs

    def test_summary_rendered(self) -> None:
        """Summary description text appears."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "builds reliable web applications" in text

    @pytest.mark.parametrize(
        "summary",
        [
            "Builds Python services.\n\nWorks with design teams.",
            "Builds Python services.\nWorks with design teams.",
            "Builds Python services. \r\n \r\n\n  Works with design teams.",
            "Builds Python services.\n\u0085\n\ufeff\u200b\nWorks with design teams.",
        ],
    )
    def test_each_line_break_of_the_summary_starts_a_paragraph(self, summary: str) -> None:
        # The editor shows a paragraph after each Enter; the Word file printed them all as one.
        data = _load_canonical_resume().model_dump(mode="json")
        data["summary"]["summary_description"] = summary
        document = Document(BytesIO(render_docx(Resume.model_validate(data))))
        texts = [p.text for p in document.paragraphs]
        start = texts.index("SUMMARY") + 1

        assert texts[start : start + 3] == [
            "Builds Python services.",
            "Works with design teams.",
            "SKILLS",
        ]

    def test_skills_rendered(self) -> None:
        """Skill groups appear with title and delimiter-separated items."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "Languages:" in text
        assert "JavaScript" in text
        assert "TypeScript" in text
        assert "Python" in text

    def test_experience_rendered(self) -> None:
        """Experience heading includes company, title, and date range."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "EXAMPLE INC." in text
        assert "Software Engineer" in text
        assert "Jan 2020" in text
        assert "Present" in text

    def test_education_end_date_without_start_date_is_not_lost(self) -> None:
        data = json.loads((REPOSITORY_ROOT / "examples" / "resume.example.json").read_text())
        data["education"][0]["start_date"] = None
        data["education"][0]["end_date"] = "2014-01"
        buffer = render_docx(Resume.model_validate(data))
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])
        assert "Jan 2014" in text

    def test_a_year_alone_is_printed_as_it_was_saved(self) -> None:
        data = json.loads((REPOSITORY_ROOT / "examples" / "resume.example.json").read_text())
        data["professional_experience"][0].update(start_date="2020", end_date="2023-06")
        data["education"][0].update(start_date="2010", end_date="2014")
        buffer = render_docx(Resume.model_validate(data))
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])
        assert "2020 \u2013 Jun 2023" in text
        assert "2010 \u2013 2014" in text

    def test_experience_bullets_rendered(self) -> None:
        """Experience bullet text appears in the document."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "Built a web application" in text
        assert "Improved application performance" in text

    def test_education_rendered(self) -> None:
        """Education section includes degree, school, and dates."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "Bachelor of Science" in text
        assert "University of California, Los Angeles" in text
        assert "Computer Science" in text

    def test_certifications_rendered(self) -> None:
        """Certification entries appear in the document."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "AWS Certified Developer" in text

    def test_footer_contains_name_and_page_number(self) -> None:
        """Footer includes candidate name and page number field."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        footer = _read_docx_xml(buffer)["footer_xml"]

        assert "John Doe" in footer or "JOHN DOE" in footer
        # Page number field (fldChar begin)
        assert 'w:fldChar w:fldCharType="begin"' in footer

    @pytest.mark.parametrize("margin", [None, Twips(1440)])
    def test_footer_page_number_tab_stops_at_right_margin(self, margin: Twips | None) -> None:
        """The right-aligned page number sits at the text edge for any page margins."""
        style = (
            DocxStyle()
            if margin is None
            else replace(DocxStyle(), margin_left=margin, margin_right=margin)
        )
        buffer = render_docx(_load_canonical_resume(), style=style)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        footer = etree.fromstring(_read_docx_xml(buffer)["footer_xml"].encode())
        positions = footer.xpath("//w:tab[@w:val='right']/@w:pos", namespaces=ns)

        text_width = style.page_width.twips - style.margin_left.twips - style.margin_right.twips
        assert positions == [str(text_width)]

    def test_keep_controls_are_paragraph_properties_and_roundtrip(self) -> None:
        """Keep controls survive OOXML serialization and python-docx reopening."""
        buffer = render_docx(_load_canonical_resume())
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        root = etree.fromstring(_read_docx_xml(buffer)["document_xml"].encode())
        assert len(root.xpath("//w:p/w:pPr/w:keepNext", namespaces=ns)) >= 7
        assert len(root.xpath("//w:p/w:pPr/w:keepLines", namespaces=ns)) >= 4
        assert not root.xpath("//w:p/w:keepNext | //w:p/w:keepLines", namespaces=ns)
        reopened = Document(BytesIO(buffer))
        assert sum(p.paragraph_format.keep_with_next is True for p in reopened.paragraphs) >= 7
        assert sum(p.paragraph_format.keep_together is True for p in reopened.paragraphs) >= 4

    def test_no_tables_or_textboxes(self) -> None:
        """DOCX has no tables or text boxes that reduce ATS readability."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        xml = _read_docx_xml(buffer)["document_xml"]

        assert "<w:tbl>" not in xml

    def test_multiple_experiences(self) -> None:
        """Renders a resume with multiple professional experiences."""
        # Canonical example already has 2 experiences
        resume = _load_canonical_resume()
        assert len(resume.professional_experience) >= 2

        buffer = render_docx(resume)
        assert isinstance(buffer, bytes)
        assert buffer[:2] == b"PK"

        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])
        assert "ANOTHER INC." in text
        assert "Delivered features" in text
        assert "Junior Software Engineer" in text

    def test_no_professional_experience(self) -> None:
        """Renders a resume without professional experience section."""
        data = _minimal_resume_data()
        data["professional_experience"] = []
        resume = Resume.model_validate(data)

        buffer = render_docx(resume)
        assert isinstance(buffer, bytes)
        assert buffer[:2] == b"PK"

        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])
        # Should still render other sections
        assert "SUMMARY" in text
        assert "SKILLS" in text
        # Should NOT have professional experience section
        assert "PROFESSIONAL EXPERIENCE" not in text

    def test_empty_certifications(self) -> None:
        """Renders a resume with no certifications."""
        data = _minimal_resume_data()
        data["certifications"] = {}
        resume = Resume.model_validate(data)

        buffer = render_docx(resume)
        assert isinstance(buffer, bytes)
        assert buffer[:2] == b"PK"

    def test_no_contact_email(self) -> None:
        """Renders a resume without optional contact fields."""
        data = _minimal_resume_data()
        data["person"]["email"] = None
        data["person"]["phone"] = None
        data["person"]["links"] = []
        resume = Resume.model_validate(data)

        buffer = render_docx(resume)
        assert isinstance(buffer, bytes)
        assert buffer[:2] == b"PK"

        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])
        # Name is uppercased by the renderer
        assert "JOHN DOE" in text
        assert "New York, NY" in text

    def test_archive_timestamps_are_stable(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        with zipfile.ZipFile(BytesIO(buffer)) as archive:
            assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())

    def test_deterministic_output(self) -> None:
        """Same input and clock produce identical byte output."""
        resume = _load_canonical_resume()
        clock = MovableClock()
        first = render_docx(resume, clock=clock)
        second = render_docx(resume, clock=clock)

        assert first == second

    def test_render_returns_bytes_not_string(self) -> None:
        """render_docx returns bytes, not str."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)

        assert isinstance(buffer, bytes)

    def test_education_dates_rendered(self) -> None:
        """Education start and end dates appear in the document."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "Jan 2010" in text
        assert "Jan 2014" in text

    def test_experience_location_rendered(self) -> None:
        """Experience location appears next to the heading."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)
        text = _text_from_xml(_read_docx_xml(buffer)["document_xml"])

        assert "New York, NY" in text


class TestValidateDocx:
    """Test suite for validate_docx()."""

    def test_valid_docx_passes_validation(self) -> None:
        """A well-formed DOCX passes all health checks."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)

        result = validate_docx(buffer)
        assert result["valid"] is True
        assert len(result["issues"]) == 0

    def test_required_zip_parts_present(self) -> None:
        """Validator confirms required ZIP entries are present."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)

        result = validate_docx(buffer)
        assert result["valid"] is True

    def test_invalid_buffer_fails(self) -> None:
        """Validator rejects non-DOCX data."""
        result = validate_docx(b"not a zip file")
        assert result["valid"] is False

    def test_styles_xml_present(self) -> None:
        """Validator checks for styles.xml."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)

        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            assert "word/styles.xml" in zf.namelist()

    def test_rejects_missing_content_type_declarations(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        broken = _replace_package_part(
            buffer,
            "[Content_Types].xml",
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        result = validate_docx(broken)
        assert result["valid"] is False
        assert "content type" in str(result["issues"]).lower()

    def test_rejects_wrong_main_document_content_type(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            content_types = zf.read("[Content_Types].xml")
        broken = _replace_package_part(
            buffer,
            "[Content_Types].xml",
            content_types.replace(
                b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                b"application/xml",
                1,
            ),
        )
        assert validate_docx(broken)["valid"] is False

    def test_accepts_explicit_xml_overrides_without_xml_default(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        with zipfile.ZipFile(BytesIO(buffer)) as archive:
            root = etree.fromstring(archive.read("[Content_Types].xml"))
            xml_parts = [
                name
                for name in archive.namelist()
                if name.endswith(".xml") and name != "[Content_Types].xml"
            ]
        namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
        defaults = root.findall(f"{{{namespace}}}Default")
        xml_default = next(node for node in defaults if node.get("Extension") == "xml")
        declared = {node.get("PartName") for node in root.findall(f"{{{namespace}}}Override")}
        for name in xml_parts:
            if f"/{name}" not in declared:
                etree.SubElement(
                    root,
                    f"{{{namespace}}}Override",
                    PartName=f"/{name}",
                    ContentType="application/xml",
                )
        root.remove(xml_default)
        variant = _replace_package_part(buffer, "[Content_Types].xml", etree.tostring(root))
        assert validate_docx(variant)["valid"] is True

    def test_rejects_undeclared_binary_package_part(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        modified = BytesIO()
        with zipfile.ZipFile(BytesIO(buffer)) as source, zipfile.ZipFile(modified, "w") as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            target.writestr("word/media/undeclared.png", b"\x89PNG\r\n\x1a\n")
        result = validate_docx(modified.getvalue())
        assert result["valid"] is False
        assert any("Missing content type" in issue for issue in result["issues"])

    def test_rejects_wrong_relationship_namespace(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        broken = _replace_package_part(buffer, "_rels/.rels", b"<Relationships/>")
        assert validate_docx(broken)["valid"] is False

    def test_rejects_unsafe_external_relationship(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            rels = zf.read("word/_rels/document.xml.rels")
        assert b"https://github.com/john-doe" in rels
        broken = _replace_package_part(
            buffer,
            "word/_rels/document.xml.rels",
            rels.replace(b"https://github.com/john-doe", b"javascript:alert(1)", 1),
        )
        assert validate_docx(broken)["valid"] is False

    def test_rejects_broken_relationship_xml(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        broken = _replace_package_part(buffer, "word/_rels/document.xml.rels", b"<Relationships")
        assert validate_docx(broken)["valid"] is False

    def test_rejects_dangling_relationship_target(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            rels = zf.read("word/_rels/document.xml.rels")
        broken = _replace_package_part(
            buffer, "word/_rels/document.xml.rels", rels.replace(b"styles.xml", b"missing.xml", 1)
        )
        assert validate_docx(broken)["valid"] is False

    def test_rejects_unresolved_document_relationship(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            document = zf.read("word/document.xml")
        broken = _replace_package_part(
            buffer, "word/document.xml", document.replace(b'r:id="rId10"', b'r:id="missing"', 1)
        )
        assert validate_docx(broken)["valid"] is False

    def test_rejects_unknown_style(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            document = zf.read("word/document.xml")
        broken = _replace_package_part(
            buffer,
            "word/document.xml",
            document.replace(
                b"<w:body>",
                b'<w:body><w:p><w:pPr><w:pStyle w:val="NonexistentStyle"/></w:pPr></w:p>',
                1,
            ),
        )
        assert validate_docx(broken)["valid"] is False

    def test_rejects_undefined_font_reference(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            document = zf.read("word/document.xml")
        broken = _replace_package_part(
            buffer,
            "word/document.xml",
            document.replace(b'w:ascii="Arial"', b'w:ascii="NeverDefinedFont"', 1),
        )
        assert validate_docx(broken)["valid"] is False

    def test_rejects_malformed_styles_part(self) -> None:
        buffer = render_docx(_load_canonical_resume())
        broken = _replace_package_part(buffer, "word/styles.xml", b"<styles")
        assert validate_docx(broken)["valid"] is False

    def test_relationships_xml_present(self) -> None:
        """Validator checks for .rels files."""
        resume = _load_canonical_resume()
        buffer = render_docx(resume)

        with zipfile.ZipFile(BytesIO(buffer)) as zf:
            names = zf.namelist()
            rels = [n for n in names if n.endswith(".rels")]
            assert len(rels) >= 2  # .rels + document.xml.rels


class TestOutputName:
    """Output-name/path-traversal safety tests."""

    def test_simple_name_is_accepted(self) -> None:
        """A plain filename passes validation."""
        name = "resume.docx"
        assert not name.startswith(".")
        assert "/" not in name
        assert "\\" not in name

    def test_rejects_dot_prefix(self) -> None:
        """A filename starting with '.' is suspicious."""
        name = ".hidden.docx"
        assert name.startswith(".")

    def test_rejects_path_traversal(self) -> None:
        """Path-traversal sequences are detected."""
        for bad in ["../resume.docx", "sub/../resume.docx", "../../etc/passwd"]:
            assert "../" in bad or "..\\" in bad

    def test_rejects_absolute_path(self) -> None:
        """Absolute paths are rejected."""
        for bad in ["/etc/resume.docx", "/tmp/resume.docx"]:
            assert bad.startswith("/")


def _german_resume() -> Resume:
    path = REPOSITORY_ROOT / "examples" / "resume-page" / "german.json"
    return Resume.model_validate_json(path.read_text(encoding="utf-8"))


def test_a_resume_in_german_prints_its_headings_dates_and_footer_in_german() -> None:
    xml = _read_docx_xml(render_docx(_german_resume()))
    text = _text_from_xml(xml["document_xml"])

    for heading in ("PROFIL", "KENNTNISSE", "BERUFSERFAHRUNG", "AUSBILDUNG & ZERTIFIKATE"):
        assert heading in text
    assert "März 2019 \u2013 heute" in text
    assert "Okt. 2016 \u2013 Feb. 2019" in text
    assert "Okt. 2022 \u2013 heute" in text
    assert "Zertifikate:" in text
    assert "Praxisanleitung, DBfK, März 2023" in text
    assert "Seite" in _text_from_xml(xml["footer_xml"])
    for english in ("SUMMARY", "SKILLS", "EXPERIENCE", "EDUCATION", "Present", "Mar ", "Certif"):
        assert english not in text
    assert "Page" not in _text_from_xml(xml["footer_xml"])


def test_a_resume_in_french_prints_the_apostrophe_of_its_word_for_present() -> None:
    resume = _german_resume().model_copy(update={"language": "fr"})
    text = _text_from_xml(_read_docx_xml(render_docx(resume))["document_xml"])

    assert "mars 2019 \u2013 aujourd'hui" in text
    assert "EXPÉRIENCE PROFESSIONNELLE" in text


@pytest.mark.parametrize("space", ["\u00a0", "\u2007", "\u202f"])
def test_word_keeps_nonbreaking_spaces_in_edited_resume_text(space: str) -> None:
    data = _minimal_resume_data()
    summary = data["summary"]
    jobs = data["professional_experience"]
    assert isinstance(summary, dict) and isinstance(jobs, list)
    headline = f"Engineer at 1{space}000 sites"
    description = f"Improved throughput by 20{space}%."
    bullet = f"Saved 1{space}000 hours."
    summary.update(text=headline, summary_description=description)
    jobs[0].update(bullets=[bullet])

    text = _text_from_xml(_read_docx_xml(render_docx(Resume.model_validate(data)))["document_xml"])

    assert headline.upper() in text
    assert description in text
    assert bullet in text


def test_a_name_saved_with_double_spaces_is_printed_with_single_spaces() -> None:
    # A name saved before saving collapsed the gaps: Word prints every space.
    data = _minimal_resume_data()
    person = data["person"]
    assert isinstance(person, dict)
    person.update(name="Ana  María   Núñez", location="Madrid,  Spain", phone="+34  600 000 000")
    buffer = render_docx(Resume.model_validate(data))
    xml = _read_docx_xml(buffer)
    with zipfile.ZipFile(BytesIO(buffer)) as package:
        core = package.read("docProps/core.xml").decode("utf-8")

    text = _text_from_xml(xml["document_xml"])
    for gapped in ("ANA  MARÍA", "MARÍA   NÚÑEZ", "Madrid,  Spain", "+34  600"):
        assert gapped not in text
    assert "  " not in _text_from_xml(xml["footer_xml"])
    assert "  " not in core
    assert "ANA MARÍA NÚÑEZ" in _text_from_xml(xml["document_xml"])
    assert "Madrid, Spain" in _text_from_xml(xml["document_xml"])
    assert "+34 600 000 000" in _text_from_xml(xml["document_xml"])
    assert "Ana María Núñez" in _text_from_xml(xml["footer_xml"])
    assert "<dc:creator>Ana María Núñez</dc:creator>" in core


def test_a_stored_word_file_downloaded_for_a_name_saved_with_gaps_names_it_with_single_spaces() -> (
    None
):
    # A download sets fresh properties on the stored file, with the run's resume as it was kept.
    data = _minimal_resume_data()
    person = data["person"]
    assert isinstance(person, dict)
    person.update(name="Ana  María   Núñez")
    stored = render_docx(Resume.model_validate(_minimal_resume_data()))

    buffer = with_docx_properties(
        stored, Resume.model_validate(data), modified_at=datetime(2026, 10, 21, tzinfo=UTC)
    )

    with zipfile.ZipFile(BytesIO(buffer)) as package:
        core = package.read("docProps/core.xml").decode("utf-8")
    assert "<dc:creator>Ana María Núñez</dc:creator>" in core


def _pdf_text(resume: Resume) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(resume))).pages)


def test_a_saved_title_put_back_prints_with_one_space_in_word_as_in_the_pdf() -> None:
    # "In your profile: … Use it" puts back the title as the career profile stored it, and Word
    # prints every space, unlike the page and the PDF. Every other printed line can hold a run too.
    data = _minimal_resume_data()
    jobs = data["professional_experience"]
    degrees = data["education"]
    summary = data["summary"]
    skills = data["technical_skills"]
    assert isinstance(jobs, list) and isinstance(degrees, list)
    assert isinstance(summary, dict) and isinstance(skills, dict)
    jobs[0].update(
        work_title="Senior   Backend Engineer",
        company_name="Example  Inc.",
        location="New  York, NY",
        bullets=["Built  a web application."],
    )
    degrees[0].update(
        school_name="University  of California",
        degree="Bachelor  of Science",
        field_of_study="Computer  Science",
        bullets=["Dean's   list."],
    )
    summary.update(
        text="Senior  Engineer", summary_description="Builds  reliable apps.\n\nLeads   a team."
    )
    skills["skills"] = [{"title": "Back  end", "bullets": ["Fast  API", "Python"]}]
    data["certifications"] = {"certifications": ["AWS   Developer"]}
    resume = Resume.model_validate(data)

    document = Document(BytesIO(render_docx(resume)))
    # Each paragraph as Word prints it, its runs joined; a bullet's own mark is set apart by two.
    paragraphs = [paragraph.text.removeprefix("•  ") for paragraph in document.paragraphs]
    footer = document.sections[0].footer.paragraphs[0].text

    assert not [paragraph for paragraph in paragraphs if "  " in paragraph]
    assert "  " not in footer
    assert (
        "EXAMPLE INC. — Senior Backend Engineer · Jan 2020 \u2013 Present · New York, NY"
        in paragraphs
    )
    assert "Senior Backend Engineer" in _pdf_text(resume)
    for line in (
        "SENIOR ENGINEER",
        "Back end: Fast API · Python",
        "Built a web application.",
        "Bachelor of Science — University of California · Computer Science",
        "Dean's list.",
        "Certifications: AWS Developer",
        # The summary keeps its two paragraphs.
        "Builds reliable apps.",
        "Leads a team.",
    ):
        assert line in paragraphs


def test_a_job_and_a_degree_are_dated_with_the_same_dash_in_word_as_in_the_pdf() -> None:
    resume = Resume.model_validate(_minimal_resume_data())
    word = _text_from_xml(_read_docx_xml(render_docx(resume))["document_xml"])
    pdf = _pdf_text(resume)

    for dates in ("Jan 2020 \u2013 Present", "Jan 2010 \u2013 Jan 2014"):
        assert dates in word
        assert dates in pdf
    assert " - " not in word
