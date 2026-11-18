# type: ignore[reportUnknownMemberType, reportUnknownVariableType, reportAttributeAccessIssue, reportPrivateUsage]
"""DOCX resume-rendering adapter.

Renders a validated Resume model into an editable, ATS-readable
Word (DOCX) document with classic monochrome Letter layout.
"""

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO

from docx import Document as _Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Twips

from resume_kit.contracts.printable import in_capitals, without_variation_selectors
from resume_kit.contracts.resume import (
    Education,
    ProfessionalExperience,
    Resume,
    TechnicalSkill,
    summary_paragraphs,
    with_single_spaces,
)
from resume_kit.contracts.resume_language import PRINTED_WORDS, ResumeLanguage, printed_date
from resume_kit.contracts.runs import Clock, utc_now
from resume_kit.documents.certification_dates import printed_certification
from resume_kit.documents.degree_line import printed_field_of_study
from resume_kit.documents.printed_address import printed_address
from resume_kit.documents.printed_location import printed_location
from resume_kit.features import feature_on
from resume_kit.zip_safety import checked_zip

# ── Built-in classic visual preset ───────────────────────────────────────


@dataclass(frozen=True)
class DocxStyle:
    """Immutable classic resume appearance; not a user-facing configuration."""

    page_width: Twips = field(default_factory=lambda: Twips(12240))
    page_height: Twips = field(default_factory=lambda: Twips(15840))
    margin_left: Twips = field(default_factory=lambda: Twips(691))
    margin_right: Twips = field(default_factory=lambda: Twips(691))
    margin_top: Twips = field(default_factory=lambda: Twips(490))
    margin_bottom: Twips = field(default_factory=lambda: Twips(576))
    header_margin: Twips = field(default_factory=lambda: Twips(360))
    footer_margin: Twips = field(default_factory=lambda: Twips(240))

    font_name: str = "Arial"
    black: RGBColor = field(default_factory=lambda: RGBColor(0x11, 0x11, 0x11))
    muted: RGBColor = field(default_factory=lambda: RGBColor(0x44, 0x44, 0x44))
    body_size: Pt = field(default_factory=lambda: Pt(9))
    summary_title_size: Pt = field(default_factory=lambda: Pt(9.5))
    header_name_size: Pt = field(default_factory=lambda: Pt(19))
    section_heading_size: Pt = field(default_factory=lambda: Pt(10))
    skill_size: Pt = field(default_factory=lambda: Pt(8.5))
    footer_size: Pt = field(default_factory=lambda: Pt(7))
    contact_size: Pt = field(default_factory=lambda: Pt(8))
    date_muted_size: Pt = field(default_factory=lambda: Pt(9))
    education_date_size: Pt = field(default_factory=lambda: Pt(8.5))
    bullet_size: Pt = field(default_factory=lambda: Pt(9))
    cert_size: Pt = field(default_factory=lambda: Pt(9))
    experience_heading_size: Pt = field(default_factory=lambda: Pt(9.5))

    section_space_before: int = 10
    section_space_after: int = 5
    body_space_after: int = 4
    bullet_space_after: int = 4
    contact_space_after: int = 5
    contact_line_spacing: float = 1.30
    section_line_spacing: float = 1.15
    bullet_line_spacing: float = 1.10
    bullet_left_indent: int = 17
    bullet_hanging_indent: int = 11


DEFAULT_DOCX_STYLE = DocxStyle()


# ── Helpers ──────────────────────────────────────────────────────────────


def _date_range(start_date: str, end_date: str | None, language: ResumeLanguage) -> str:
    """A date range with the en dash the page, the PDF and Copy text put between its dates."""
    start = printed_date(start_date, language)
    end = printed_date(end_date, language) if end_date else PRINTED_WORDS[language].present
    return f"{start} \u2013 {end}"


def _set_paragraph_format(
    paragraph: object,
    *,
    keep_next: bool = False,
    keep_lines: bool = False,
    space_before: int | None = None,
    space_after: int | None = None,
    line_spacing: float | None = None,
    left_indent: int | None = None,
    hanging_indent: int | None = None,
    alignment: int | None = None,
) -> None:
    """Configure paragraph formatting properties."""
    pf = paragraph.paragraph_format  # type: ignore[union-attr]

    if keep_next:
        pf.keep_with_next = True
    if keep_lines:
        pf.keep_together = True
    if space_before is not None:
        pf.space_before = Pt(space_before)
    if space_after is not None:
        pf.space_after = Pt(space_after)
    if line_spacing is not None:
        pf.line_spacing = line_spacing
    if left_indent is not None:
        pf.left_indent = Pt(left_indent)
    if hanging_indent is not None:
        # hanging indent is negative first_line
        pf.first_line_indent = Pt(-hanging_indent)
    if alignment is not None:
        pf.alignment = alignment


def _add_run(
    paragraph: object,
    text: str,
    style: DocxStyle,
    *,
    bold: bool = False,
    size: Pt | None = None,
    color: RGBColor | None = None,
) -> None:
    """Add a formatted run to a paragraph."""
    run = paragraph.add_run(text)
    run.bold = bold
    if size is not None:
        run.font.size = size
    run.font.color.rgb = color if color is not None else style.black
    run.font.name = style.font_name
    # Ensure font applies to East Asian text too
    r_element = run._element
    rpr = r_element.find(qn("w:rPr"))
    if rpr is not None:
        rFonts = rpr.find(qn("w:rFonts"))
        if rFonts is not None:
            rFonts.set(qn("w:ascii"), style.font_name)
            rFonts.set(qn("w:hAnsi"), style.font_name)


def _add_hyperlink(
    paragraph: object,
    text: str,
    target: str,
    style: DocxStyle,
    *,
    size: Pt | None = None,
    color: RGBColor | None = None,
) -> None:
    """Write a visible text run backed by an external relationship.

    By default it looks as the contact line does: its size and its muted colour.
    """
    if not target.startswith(("https://", "http://")):
        raise ValueError("Unsupported contact hyperlink scheme")
    part = paragraph.part  # type: ignore[union-attr]
    rel_id = part.relate_to(target, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:ascii"), style.font_name)
    fonts.set(qn("w:hAnsi"), style.font_name)
    properties.append(fonts)
    shade = OxmlElement("w:color")
    shade.set(qn("w:val"), str(color if color is not None else style.muted))
    properties.append(shade)
    points = OxmlElement("w:sz")
    points.set(qn("w:val"), str(int((size if size is not None else style.contact_size).pt * 2)))
    properties.append(points)
    run.append(properties)
    value = OxmlElement("w:t")
    value.text = text
    run.append(value)
    hyperlink.append(run)
    paragraph._element.append(hyperlink)  # type: ignore[union-attr]


def _add_section_heading(doc: _Document, text: str, style: DocxStyle) -> None:
    """Add a section heading line (uppercase, bold, keepNext)."""
    para = doc.add_paragraph()
    _set_paragraph_format(
        para,
        keep_next=True,
        space_before=style.section_space_before,
        space_after=style.section_space_after,
        line_spacing=style.section_line_spacing,
    )
    _add_run(para, text.upper(), style, bold=True, size=style.section_heading_size)


def _add_body_paragraph(doc: _Document, text: str, style: DocxStyle) -> None:
    """Add a regular body paragraph."""
    para = doc.add_paragraph()
    _set_paragraph_format(
        para, space_after=style.body_space_after, line_spacing=style.section_line_spacing
    )
    _add_run(para, text, style, size=style.body_size)


def _add_bullet_paragraph(doc: _Document, text: str, style: DocxStyle) -> None:
    """Add a bullet point paragraph with keepLines."""
    para = doc.add_paragraph()
    _set_paragraph_format(
        para,
        keep_lines=True,
        space_after=style.bullet_space_after,
        line_spacing=style.bullet_line_spacing,
        left_indent=style.bullet_left_indent,
        hanging_indent=style.bullet_hanging_indent,
    )
    _add_run(para, "•  ", style, size=style.bullet_size)
    _add_run(para, text, style, size=style.bullet_size)


def _add_skill_paragraph(doc: _Document, title: str, items: list[str], style: DocxStyle) -> None:
    """Add a skill row: Title: item · item · item."""
    para = doc.add_paragraph()
    _set_paragraph_format(para, space_after=3, line_spacing=1.10)
    _add_run(para, f"{title}:", style, bold=True, size=style.skill_size)
    _add_run(para, f" {' · '.join(items)}", style, size=style.skill_size)


def _add_experience_heading(doc: _Document, resume: Resume, index: int, style: DocxStyle) -> None:
    """Add a professional experience heading with company, title, dates."""
    exp = resume.professional_experience[index]
    para = doc.add_paragraph()
    _set_paragraph_format(
        para,
        keep_next=True,
        space_before=5,
        space_after=3,
        line_spacing=1.15,
    )
    _add_run(
        para,
        f"{in_capitals(exp.company_name)} — {exp.work_title}",
        style,
        bold=True,
        size=style.experience_heading_size,
    )
    dr = _date_range(exp.start_date, exp.end_date, resume.language)
    location_part = f" · {printed_location(exp.location)}" if exp.location else ""
    _add_run(para, f" · {dr}{location_part}", style, size=style.date_muted_size, color=style.muted)


def _add_education(
    doc: _Document, edu: Education, style: DocxStyle, language: ResumeLanguage
) -> None:
    """Add one degree: its line, its dates when it has any, then its bullets."""
    # Degree heading
    para = doc.add_paragraph()
    _set_paragraph_format(para, keep_next=True, space_after=3, line_spacing=1.10)
    _add_run(para, edu.degree, style, bold=True, size=style.body_size)

    school_parts = edu.school_name
    field_of_study = printed_field_of_study(edu.degree, edu.field_of_study)
    if field_of_study:
        school_parts += f" · {field_of_study}"
    _add_run(para, f" — {school_parts}", style, size=style.body_size)

    # Education may provide a graduation date without a start date.
    if edu.start_date or edu.end_date:
        formatted_dates = (
            _date_range(edu.start_date, edu.end_date, language)
            if edu.start_date
            else printed_date(edu.end_date, language)
        )
        para = doc.add_paragraph()
        _set_paragraph_format(para, space_after=3, line_spacing=1.04)
        _add_run(para, formatted_dates, style, size=style.education_date_size, color=style.muted)

    # Education bullets
    for bullet in edu.bullets:
        _add_bullet_paragraph(doc, bullet, style)


def _add_certifications(doc: _Document, resume: Resume, style: DocxStyle) -> None:
    """Add the certifications section if any exist."""
    words = PRINTED_WORDS[resume.language]
    certs = [
        printed_certification(
            line, lambda date: printed_date(date, resume.language), words.credential_id
        )
        for line in resume.certifications.certifications
    ]
    if not certs:
        return

    para = doc.add_paragraph()
    _set_paragraph_format(para, space_before=2, space_after=3, line_spacing=1.10)
    _add_run(para, f"{words.certifications}:", style, bold=True, size=style.cert_size)
    # The verification address that ends a line is a link, as a contact link is.
    for index, (text, link) in enumerate(certs):
        _add_run(para, f"{' · ' if index else ' '}{text}", style, size=style.cert_size)
        if link is not None:
            _add_hyperlink(para, *link, style, size=style.cert_size, color=style.black)


def _add_footer(doc: _Document, resume: Resume, style: DocxStyle) -> None:
    """Add a footer with page number."""
    # Access the footer of the only section
    section = doc.sections[0]
    footer = section.footer
    footer.is_linked_to_previous = False

    para = footer.paragraphs[0]

    # Add top border
    pPr = para._element.find(qn("w:pPr"))
    if pPr is None:
        pPr = OxmlElement("w:pPr")
        para._element.insert(0, pPr)
    pBdr = OxmlElement("w:pBdr")
    top = OxmlElement("w:top")
    top.set(qn("w:val"), "single")
    top.set(qn("w:sz"), "4")
    top.set(qn("w:space"), "1")
    top.set(qn("w:color"), "CCCCCC")
    pBdr.append(top)
    pPr.append(pBdr)

    # Right-aligned page number at the text edge (page width minus side margins).
    text_width = style.page_width.twips - style.margin_left.twips - style.margin_right.twips
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "right")
    tab.set(qn("w:pos"), str(text_width))
    tabs.append(tab)
    pPr.append(tabs)

    # Left-side text
    _add_run(
        para,
        " · ".join(part for part in (resume.person.name, resume.summary.text) if part),
        style,
        size=style.footer_size,
        color=style.muted,
    )

    # Tab to page number
    page = PRINTED_WORDS[resume.language].page
    _add_run(para, f"\t{page} ", style, size=style.footer_size, color=style.muted)

    # Page number field
    run = para.add_run()
    run.font.size = style.footer_size
    run.font.color.rgb = style.muted
    run.font.name = style.font_name
    fldChar_begin = OxmlElement("w:fldChar")
    fldChar_begin.set(qn("w:fldCharType"), "begin")
    run._element.append(fldChar_begin)

    run2 = para.add_run()
    run2.font.size = style.footer_size
    run2.font.color.rgb = style.muted
    run2.font.name = style.font_name
    instrText = OxmlElement("w:instrText")
    instrText.set(qn("xml:space"), "preserve")
    instrText.text = " PAGE "
    run2._element.append(instrText)

    run3 = para.add_run()
    run3.font.size = style.footer_size
    run3.font.color.rgb = style.muted
    run3.font.name = style.font_name
    fldChar_end = OxmlElement("w:fldChar")
    fldChar_end.set(qn("w:fldCharType"), "end")
    run3._element.append(fldChar_end)


# ── Public API ───────────────────────────────────────────────────────────


_PRINTED_SPACE_RUNS = re.compile(r"\s{2,}")


def _single_spaced(value: str) -> str:
    """Fold printed whitespace runs while keeping single nonbreaking spaces in edited text."""
    return _PRINTED_SPACE_RUNS.sub(" ", value)


def _single_spaced_or_none(value: str | None) -> str | None:
    return _single_spaced(value) if value is not None else None


def _printed_job(job: ProfessionalExperience) -> ProfessionalExperience:
    return job.model_copy(
        update={
            "work_title": _single_spaced(job.work_title),
            "company_name": _single_spaced(job.company_name),
            "location": _single_spaced_or_none(job.location),
            "bullets": [_single_spaced(bullet) for bullet in job.bullets],
        }
    )


def _printed_degree(degree: Education) -> Education:
    return degree.model_copy(
        update={
            "school_name": _single_spaced(degree.school_name),
            "degree": _single_spaced(degree.degree),
            "field_of_study": _single_spaced_or_none(degree.field_of_study),
            "bullets": [_single_spaced(bullet) for bullet in degree.bullets],
        }
    )


def _printed_skill(skill: TechnicalSkill) -> TechnicalSkill:
    return skill.model_copy(
        update={
            "title": _single_spaced(skill.title),
            "bullets": [_single_spaced(item) for item in skill.bullets],
        }
    )


def _printed(resume: Resume) -> Resume:
    """The resume as Word prints it: one space for each run of spaces in every printed line.

    Word prints every space, unlike the page and the PDF, and a line can still hold a run: a name
    saved before saving made it one, or a title put back from the career profile as it was stored.
    A summary keeps its paragraphs."""
    summary = resume.summary
    description = summary.summary_description
    return resume.model_copy(
        update={
            "person": with_single_spaces(resume.person),
            "summary": summary.model_copy(
                update={
                    "text": _single_spaced_or_none(summary.text),
                    "summary_description": (
                        "\n".join(map(_single_spaced, summary_paragraphs(description)))
                        if description
                        else description
                    ),
                }
            ),
            "technical_skills": resume.technical_skills.model_copy(
                update={
                    "skills": [_printed_skill(skill) for skill in resume.technical_skills.skills]
                }
            ),
            "professional_experience": [
                _printed_job(job) for job in resume.professional_experience
            ],
            "education": [_printed_degree(degree) for degree in resume.education],
            "certifications": resume.certifications.model_copy(
                update={
                    "certifications": [
                        _single_spaced(line) for line in resume.certifications.certifications
                    ]
                }
            ),
        }
    )


def _core_properties(resume: Resume, now: datetime) -> bytes:
    """Fresh properties, with the same name and title as the PDF and no template history."""
    # Written with Word's own prefixes: "dcterms:W3CDTF" is a prefix inside an attribute value,
    # which ElementTree's generated ns0, ns1 names would not match.
    root = ET.Element(
        "cp:coreProperties",
        {
            "xmlns:cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
            "xmlns:dc": "http://purl.org/dc/elements/1.1/",
            "xmlns:dcterms": "http://purl.org/dc/terms/",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        },
    )
    name = without_variation_selectors(resume.person.name)
    headline = without_variation_selectors(resume.summary.text or "")
    ET.SubElement(root, "dc:creator").text = name
    ET.SubElement(root, "dc:title").text = f"{name} - {headline}" if headline else name
    stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for field_name in ("created", "modified"):
        ET.SubElement(root, f"dcterms:{field_name}", {"xsi:type": "dcterms:W3CDTF"}).text = stamp
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def render_docx(
    resume: Resume,
    *,
    style: DocxStyle = DEFAULT_DOCX_STYLE,
    clock: Clock = utc_now,
    compact_skills: bool | None = None,
) -> bytes:
    """Render a Resume model into a DOCX byte buffer.

    Args:
        resume: The validated Resume model to render.
        style: Immutable appearance preset (classic layout by default).
        clock: Time the file is rendered, on generation, edit or restoration for download.
        compact_skills: The owning account's resolved flag, or the global flag for a CLI call.

    Returns:
        DOCX file contents as raw bytes (ZIP archive).
    """
    rendered_at = clock()
    resume = _printed(resume)
    doc = _Document()

    # ── Page setup ─────────────────────────────────────────────────────
    section = doc.sections[0]
    section.page_width = style.page_width
    section.page_height = style.page_height
    section.left_margin = style.margin_left
    section.right_margin = style.margin_right
    section.top_margin = style.margin_top
    section.bottom_margin = style.margin_bottom
    section.header_distance = style.header_margin
    section.footer_distance = style.footer_margin

    # Remove default empty paragraph
    if doc.paragraphs:
        p = doc.paragraphs[0]._element
        p.getparent().remove(p)

    # Set default document font
    styles = doc.styles
    if styles:
        default_style = styles["Normal"]
        font = default_style.font
        font.name = style.font_name
        font.size = style.body_size
        font.color.rgb = style.black

    # ── Header: Name ───────────────────────────────────────────────────
    para = doc.add_paragraph()
    _set_paragraph_format(
        para,
        alignment=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=2,
        line_spacing=2.0,
    )
    _add_run(para, in_capitals(resume.person.name), style, bold=True, size=style.header_name_size)

    # ── Contact details ───────────────────────────────────────────────
    contact_parts: list[tuple[str, str | None]] = []
    if resume.person.location:
        contact_parts.append((printed_location(resume.person.location) or "", None))
    if resume.person.phone:
        contact_parts.append((resume.person.phone, None))
    if resume.person.email:
        email = str(resume.person.email)
        contact_parts.append((email, None))
    for link in resume.person.links:
        contact_parts.append((printed_address(str(link.url)), str(link.url)))

    if contact_parts:
        para = doc.add_paragraph()
        _set_paragraph_format(
            para,
            alignment=WD_ALIGN_PARAGRAPH.CENTER,
            space_after=style.contact_space_after,
            line_spacing=style.contact_line_spacing,
        )
        for index, (text, target) in enumerate(contact_parts):
            if index:
                _add_run(para, " · ", style, size=style.contact_size, color=style.muted)
            if target:
                _add_hyperlink(para, text, target, style)
            else:
                _add_run(para, text, style, size=style.contact_size, color=style.muted)

    # ── Summary title (profile badge) ──────────────────────────────────
    # A headline the person cleared is left off, and so is a cleared summary with its heading.
    if resume.summary.text:
        para = doc.add_paragraph()
        _set_paragraph_format(
            para,
            alignment=WD_ALIGN_PARAGRAPH.CENTER,
            space_after=9,
            line_spacing=1.15,
        )
        _add_run(
            para, in_capitals(resume.summary.text), style, bold=True, size=style.summary_title_size
        )

    words = PRINTED_WORDS[resume.language]

    # ── Summary section ────────────────────────────────────────────────
    if resume.summary.summary_description:
        _add_section_heading(doc, words.summary, style)
        for paragraph in summary_paragraphs(resume.summary.summary_description):
            _add_body_paragraph(doc, paragraph, style)

    # ── Skills ─────────────────────────────────────────────────────────
    # A candidate who listed no skills gets no skills section.
    if resume.technical_skills.skills:
        _add_section_heading(doc, words.skills, style)
        if compact_skills is None:
            compact_skills = feature_on("compact_skills")
        if compact_skills:
            para = doc.add_paragraph()
            _set_paragraph_format(para, space_after=3, line_spacing=1.10)
            for index, skill in enumerate(resume.technical_skills.skills):
                if index:
                    _add_run(para, " · ", style, size=style.skill_size)
                _add_run(para, f"{skill.title}:", style, bold=True, size=style.skill_size)
                _add_run(para, f" {' · '.join(skill.bullets)}", style, size=style.skill_size)
        else:
            for skill in resume.technical_skills.skills:
                _add_skill_paragraph(doc, skill.title, skill.bullets, style)

    # ── Professional Experience ────────────────────────────────────────
    if resume.professional_experience:
        _add_section_heading(doc, words.experience, style)
        for i in range(len(resume.professional_experience)):
            first = len(doc.paragraphs)
            _add_experience_heading(doc, resume, i, style)
            for bullet in resume.professional_experience[i].bullets:
                _add_bullet_paragraph(doc, bullet, style)
            # Link the job's lines so Word moves a job that fits a page whole. Word breaks a
            # chain taller than a page at the page end, so a longer job runs on without tables.
            for paragraph in doc.paragraphs[first:-1]:
                paragraph.paragraph_format.keep_with_next = True

    # ── Education & Credentials ────────────────────────────────────────
    # One section for degrees and certifications. It is left out when there are neither.
    if resume.education or resume.certifications.certifications:
        _add_section_heading(doc, words.education, style)
        for index, education in enumerate(resume.education):
            degree_start = len(doc.paragraphs)
            _add_education(doc, education, style, resume.language)
            if index == len(resume.education) - 1:
                _add_certifications(doc, resume, style)
            # Link each degree's lines, rather than an unbounded whole credentials section.
            for paragraph in doc.paragraphs[degree_start:-1]:
                paragraph.paragraph_format.keep_with_next = True
                paragraph.paragraph_format.keep_together = True
        if not resume.education:
            _add_certifications(doc, resume, style)

    # ── Footer ─────────────────────────────────────────────────────────
    _add_footer(doc, resume, style)

    # ── Serialize ──────────────────────────────────────────────────────
    buffer = BytesIO()
    doc.save(buffer)
    return with_docx_properties(buffer.getvalue(), resume, modified_at=rendered_at)


def with_docx_properties(buffer: bytes, resume: Resume, *, modified_at: datetime) -> bytes:
    """Set fresh properties on a rendered or cached file, preserving every other part.

    Downloads use the run's last change from the application's clock, including when the
    stored file predates this metadata fix or has been restored after retention deleted it.
    """
    properties = _core_properties(_printed(resume), modified_at)
    stable = BytesIO()
    # python-docx stamps ZIP members with wall-clock time; normalize just the
    # ZIP metadata so identical resume input and clock yield identical DOCX bytes.
    with checked_zip(buffer) as source, zipfile.ZipFile(stable, "w") as output:
        for member in source.infolist():
            contents = source.read(member)
            if member.filename == "docProps/core.xml":
                contents = properties
            elif member.filename == "docProps/app.xml":
                # The default template also claims to come from Macintosh Word and Normal.dotm.
                contents = (
                    b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
                    b'2006/extended-properties"><Application>job-hunt</Application></Properties>'
                )
            member.date_time = (1980, 1, 1, 0, 0, 0)
            output.writestr(member, contents)
    return stable.getvalue()


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def _relationship_source(path: str) -> str:
    """Map a .rels member back to the package part that owns it."""
    if path == "_rels/.rels":
        return ""
    folder, filename = posixpath.split(path)
    return posixpath.join(posixpath.dirname(folder), filename.removesuffix(".rels"))


def validate_docx(buffer: bytes) -> dict[str, object]:
    """Health-check ZIP integrity, OOXML parts, references, styles and font names.

    This is not full OOXML schema validation or a visual rendering check.
    Returns {"valid": bool, "issues": list[str]}.
    """
    issues: list[str] = []
    try:
        with checked_zip(buffer) as zf:
            names = set(zf.namelist())
            if len(names) != len(zf.namelist()):
                issues.append("Duplicate ZIP member names")
            for required in (
                "[Content_Types].xml",
                "_rels/.rels",
                "word/document.xml",
                "word/styles.xml",
                "word/fontTable.xml",
                "word/_rels/document.xml.rels",
            ):
                if required not in names:
                    issues.append(f"Missing required part: {required}")
            if not any(n.startswith("word/footer") and n.endswith(".xml") for n in names):
                issues.append("Missing footer part")
            bad = zf.testzip()
            if bad:
                issues.append(f"Corrupt ZIP member: {bad}")
            if issues:
                return {"valid": False, "issues": issues}

            roots: dict[str, ET.Element] = {}
            for name in names:
                if name.endswith((".xml", ".rels")):
                    try:
                        roots[name] = ET.fromstring(zf.read(name))
                    except ET.ParseError as exc:
                        issues.append(f"Malformed XML in {name}: {exc}")
            if issues:
                return {"valid": False, "issues": issues}

            content_types = roots["[Content_Types].xml"]
            if content_types.tag != f"{{{_CT}}}Types":
                issues.append("Invalid content types root")
            defaults = {
                node.get("Extension"): node.get("ContentType")
                for node in content_types.findall(f"{{{_CT}}}Default")
            }
            overrides = {
                node.get("PartName"): node.get("ContentType")
                for node in content_types.findall(f"{{{_CT}}}Override")
            }
            required_types = {
                "/word/document.xml": "document.main+xml",
                "/word/styles.xml": "styles+xml",
                "/word/fontTable.xml": "fontTable+xml",
            }
            required_types.update(
                (f"/{name}", "footer+xml")
                for name in names
                if name.startswith("word/footer") and name.endswith(".xml")
            )
            for part_name, suffix in required_types.items():
                expected = (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml." + suffix
                )
                if overrides.get(part_name, defaults.get("xml")) != expected:
                    issues.append(f"Missing or incorrect content type: {part_name}")
            for name in names:
                if name == "[Content_Types].xml" or name.endswith("/"):
                    continue
                extension = name.rsplit(".", 1)[-1] if "." in name else ""
                effective = overrides.get(f"/{name}", defaults.get(extension))
                if not effective:
                    issues.append(f"Missing content type: {name}")
                elif extension == "rels" and effective != (
                    "application/vnd.openxmlformats-package.relationships+xml"
                ):
                    issues.append(f"Incorrect content type: {name}")
            if issues:
                return {"valid": False, "issues": issues}

            relationships: dict[str, set[str]] = {}
            for name, root in roots.items():
                if not name.endswith(".rels"):
                    continue
                source = _relationship_source(name)
                if root.tag != f"{{{_PR}}}Relationships":
                    issues.append(f"Invalid relationships root in {name}")
                    continue
                ids: set[str] = set()
                for relation in root.findall(f"{{{_PR}}}Relationship"):
                    rid = relation.get("Id")
                    target = relation.get("Target")
                    if not rid or not target or rid in ids:
                        issues.append(f"Invalid relationship in {name}: {rid}")
                        continue
                    ids.add(rid)
                    if relation.get("TargetMode") == "External":
                        if not relation.get("Type", "").endswith(
                            "/hyperlink"
                        ) or not target.startswith(("https://", "http://", "mailto:")):
                            issues.append(f"Unsafe external relationship in {name}: {target}")
                    else:
                        resolved = posixpath.normpath(
                            posixpath.join(posixpath.dirname(source), target.lstrip("/"))
                        )
                        if target.startswith("/"):
                            resolved = target.lstrip("/")
                        if resolved not in names:
                            issues.append(f"Dangling relationship in {name}: {rid} -> {target}")
                relationships[source] = ids

            for name, root in roots.items():
                if not name.startswith("word/") or not name.endswith(".xml"):
                    continue
                available = relationships.get(name, set())
                for element in root.iter():
                    for attribute in ("id", "embed", "link"):
                        rid = element.get(f"{{{_R}}}{attribute}")
                        if rid and rid not in available:
                            issues.append(f"Unresolved relationship in {name}: {rid}")

            styles = {
                s.get(f"{{{_W}}}styleId") for s in roots["word/styles.xml"].iter(f"{{{_W}}}style")
            }
            fonts = {
                f.get(f"{{{_W}}}name") for f in roots["word/fontTable.xml"].iter(f"{{{_W}}}font")
            }
            for name, root in roots.items():
                if name != "word/document.xml" and not name.startswith("word/footer"):
                    continue
                for element in root.iter():
                    if element.tag in (f"{{{_W}}}pStyle", f"{{{_W}}}rStyle", f"{{{_W}}}tblStyle"):
                        style_id = element.get(f"{{{_W}}}val")
                        if style_id and style_id not in styles:
                            issues.append(f"Undefined style in {name}: {style_id}")
                    if element.tag == f"{{{_W}}}rFonts":
                        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
                            font = element.get(f"{{{_W}}}{attr}")
                            if font and font not in fonts:
                                issues.append(f"Undefined font in {name}: {font}")
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as exc:
        issues.append(f"Unreadable DOCX package: {exc}")
    return {"valid": not issues, "issues": issues}
