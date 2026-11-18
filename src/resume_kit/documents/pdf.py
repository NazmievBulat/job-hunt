"""Searchable Letter PDF from the canonical resume contract."""

from __future__ import annotations

import errno
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import cast
from uuid import uuid4

import pypdfium2 as pdfium  # pyright: ignore[reportMissingTypeStubs]
from PIL import Image
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable, KeepTogether, Paragraph, SimpleDocTemplate

from resume_kit.contracts.printable import (
    in_capitals,
    unprintable_characters,
    without_variation_selectors,
)
from resume_kit.contracts.resume import (
    Education,
    Person,
    ProfessionalExperience,
    Resume,
    summary_paragraphs,
)
from resume_kit.contracts.resume_language import PRINTED_WORDS, ResumeLanguage, printed_date
from resume_kit.contracts.runs import Clock, utc_now
from resume_kit.documents.certification_dates import printed_certification
from resume_kit.documents.degree_line import printed_field_of_study
from resume_kit.documents.pagination import HeadedBlock
from resume_kit.documents.printed_address import printed_address
from resume_kit.documents.printed_location import printed_location
from resume_kit.features import feature_on

_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_REGULAR = "ResumeNotoSans"
_BOLD = "ResumeNotoSans-Bold"


@dataclass(frozen=True)
class PdfStyle:
    """Version-controlled visual preset; page-flow and output safety remain code."""

    ink: str = "#111111"
    muted: str = "#444444"
    rule_color: str = "#cccccc"
    top_margin: float = 24.5
    bottom_margin: float = 40
    side_margin: float = 34.5
    body_font_size: float = 9.05
    body_leading: float = 11.25
    name_font_size: float = 19
    name_leading: float = 20
    contact_font_size: float = 7.8
    contact_leading: float = 9.2
    headline_font_size: float = 9.4
    section_font_size: float = 10.1
    section_space_before: float = 7
    skill_font_size: float = 8.35
    role_font_size: float = 9.5
    bullet_font_size: float = 8.9
    bullet_leading: float = 11.05
    bullet_indent: float = 14
    education_font_size: float = 8.9
    footer_font_size: float = 7


DEFAULT_PDF_STYLE = PdfStyle()


# Rendering runs in worker threads, and two can be the first to need the fonts.
_REGISTERING_FONTS = Lock()


def _fonts() -> None:
    # One at a time: a render arriving while another registers the fonts would find the regular
    # one there, go on, and fail on the bold one that is not registered yet.
    with _REGISTERING_FONTS:
        if _REGULAR not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(_REGULAR, str(_FONT_DIR / "NotoSans-Regular.ttf")))  # pyright: ignore[reportUnknownMemberType]
            pdfmetrics.registerFont(TTFont(_BOLD, str(_FONT_DIR / "NotoSans-Bold.ttf")))  # pyright: ignore[reportUnknownMemberType]
            pdfmetrics.registerFontFamily(_REGULAR, normal=_REGULAR, bold=_BOLD)  # pyright: ignore[reportUnknownMemberType]


def _styles(style: PdfStyle) -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        "body",
        fontName=_REGULAR,
        fontSize=style.body_font_size,
        leading=style.body_leading,
        textColor=colors.HexColor(style.ink),
        spaceAfter=2.6,
    )
    return {
        "body": base,
        "name": ParagraphStyle(
            "name",
            parent=base,
            fontName=_BOLD,
            fontSize=style.name_font_size,
            leading=style.name_leading,
            alignment=TA_CENTER,
            spaceAfter=3,
        ),
        "contact": ParagraphStyle(
            "contact",
            parent=base,
            fontSize=style.contact_font_size,
            leading=style.contact_leading,
            textColor=colors.HexColor(style.muted),
            alignment=TA_CENTER,
            spaceAfter=4,
        ),
        "headline": ParagraphStyle(
            "headline",
            parent=base,
            fontName=_BOLD,
            fontSize=style.headline_font_size,
            leading=11,
            alignment=TA_CENTER,
            spaceAfter=4,
        ),
        "section": ParagraphStyle(
            "section",
            parent=base,
            fontName=_BOLD,
            fontSize=style.section_font_size,
            leading=11.6,
            spaceBefore=style.section_space_before,
            spaceAfter=3.2,
        ),
        "skill": ParagraphStyle(
            "skill", parent=base, fontSize=style.skill_font_size, leading=10.2, spaceAfter=1.8
        ),
        "role": ParagraphStyle(
            "role",
            parent=base,
            fontSize=style.role_font_size,
            leading=11.4,
            spaceBefore=1,
            spaceAfter=2,
        ),
        "bullet": ParagraphStyle(
            "bullet",
            parent=base,
            allowWidows=0,
            allowOrphans=0,
            fontSize=style.bullet_font_size,
            leading=style.bullet_leading,
            leftIndent=style.bullet_indent,
            firstLineIndent=-9,
            spaceAfter=2.5,
        ),
        "education": ParagraphStyle(
            "education", parent=base, fontSize=style.education_font_size, leading=10.9, spaceAfter=2
        ),
    }


def _bullet(value: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(f"•  {escape(value)}", style)


def _role(
    experience: ProfessionalExperience,
    styles: dict[str, ParagraphStyle],
    style: PdfStyle,
    language: ResumeLanguage,
) -> list[Paragraph]:
    words = PRINTED_WORDS[language]
    end = printed_date(experience.end_date, language) if experience.end_date else words.present
    location = (
        f" · {escape(printed_location(experience.location) or '')}" if experience.location else ""
    )
    heading = Paragraph(
        f"<b>{escape(in_capitals(experience.company_name))} — {escape(experience.work_title)}</b>"
        f'<font color="{escape(style.muted, quote=True)}">'
        f" · {printed_date(experience.start_date, language)} \u2013 {escape(end)}{location}</font>",
        styles["role"],
    )
    return [heading, *(_bullet(bullet, styles["bullet"]) for bullet in experience.bullets)]


def _fit_footer(text: str, width: float, style: PdfStyle) -> str:
    """Keep the footer selectable while limiting it to its allotted width."""
    if pdfmetrics.stringWidth(text, _REGULAR, style.footer_font_size) <= width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if (
            pdfmetrics.stringWidth(text[:middle].rstrip() + "…", _REGULAR, style.footer_font_size)
            <= width
        ):
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def _education(
    education: Education, styles: dict[str, ParagraphStyle], language: ResumeLanguage
) -> list[Paragraph]:
    """One degree: its line, its dates when it has any, then its bullets."""
    line = f"<b>{escape(education.degree)}</b> — {escape(education.school_name)}"
    field_of_study = printed_field_of_study(education.degree, education.field_of_study)
    if field_of_study:
        line += f" · {escape(field_of_study)}"
    # A start with no end is still going on, as for a job; an end alone is a graduation date.
    dates = printed_date(education.end_date, language) if education.end_date else ""
    if education.start_date:
        present = PRINTED_WORDS[language].present
        dates = f"{printed_date(education.start_date, language)} \u2013 {dates or present}"
    items: list[Paragraph] = [Paragraph(line, styles["education"])]
    if dates:
        items.append(Paragraph(escape(dates), styles["education"]))
    items.extend(_bullet(bullet, styles["bullet"]) for bullet in education.bullets)
    return items


class UnprintableCharacters(ValueError):
    """The resume has characters the bundled font lacks: each one once, as they first appear."""

    def __init__(self, characters: tuple[str, ...]) -> None:
        super().__init__("Bundled PDF font cannot render every character in the resume.")
        self.characters = characters


def _check_font_coverage(resume: Resume) -> None:
    """Reject unsupported glyphs rather than silently omitting candidate facts.

    The last guard. A model's answer and a hand edit are refused earlier, by the same list of
    printable characters, so this is reached only by a resume that came in some other way.
    """
    fields: list[str] = [resume.person.name]
    fields.extend(
        str(value)
        for value in (
            resume.summary.text,
            resume.summary.summary_description,
            resume.person.location,
            resume.person.phone,
            resume.person.email,
        )
        if value
    )
    fields.extend(printed_address(str(link.url)) for link in resume.person.links)
    for skill in resume.technical_skills.skills:
        fields.extend((skill.title, *skill.bullets))
    for role in resume.professional_experience:
        fields.extend((role.company_name, role.work_title, role.start_date))
        fields.extend(str(value) for value in (role.location, role.end_date) if value)
        fields.extend(role.bullets)
    for education in resume.education:
        fields.extend((education.degree, education.school_name, *education.bullets))
        fields.extend(
            str(value)
            for value in (education.field_of_study, education.start_date, education.end_date)
            if value
        )
    fields.extend(resume.certifications.certifications)
    # Joined by a line break, which is never named, so each character is named once overall.
    missing = unprintable_characters("\n".join(fields))
    if missing:
        raise UnprintableCharacters(missing)


def _as_drawn[Value](value: Value) -> Value:
    """A copy with every variation selector left out of every text.

    A selector only asks for one of two looks of the character before it and has no shape of its
    own; drawn as it is, it would be an empty box. The stored resume keeps it.
    """
    drawn: object = value
    if isinstance(value, str):
        drawn = without_variation_selectors(value)
    elif isinstance(value, BaseModel):
        # Copied without validation: the texts were valid as stored, and only lose a mark here.
        fields = {name: _as_drawn(getattr(value, name)) for name in type(value).model_fields}
        drawn = value.model_copy(update=fields)
    elif isinstance(value, list):
        drawn = [_as_drawn(item) for item in cast("list[object]", value)]
    return cast("Value", drawn)


def _certification(line: str, language: ResumeLanguage) -> str:
    """A certification line as paragraph markup: its verification address is a link."""
    text, link = printed_certification(
        line,
        lambda date: printed_date(date, language),
        PRINTED_WORDS[language].credential_id,
    )
    if link is None:
        return escape(text)
    shown, address = link
    return f'{escape(text)}<link href="{escape(address, quote=True)}">{escape(shown)}</link>'


def render_pdf(
    resume: Resume,
    *,
    style: PdfStyle = DEFAULT_PDF_STYLE,
    clock: Clock = utc_now,
    compact_skills: bool | None = None,
) -> bytes:
    """Render a deterministic, selectable-text PDF without an external service."""
    rendered_at = clock()
    _fonts()
    _check_font_coverage(resume)
    resume = _as_drawn(resume)
    styles = _styles(style)
    language: ResumeLanguage = resume.language
    words = PRINTED_WORDS[language]
    stream = BytesIO()
    # A headline or a summary the person cleared is left off the page, heading included.
    headline = resume.summary.text
    summary = resume.summary.summary_description
    doc = SimpleDocTemplate(
        stream,
        pagesize=letter,
        topMargin=style.top_margin,
        bottomMargin=style.bottom_margin,
        leftMargin=style.side_margin,
        rightMargin=style.side_margin,
        title=f"{resume.person.name} - {headline}" if headline else resume.person.name,
        author=resume.person.name,
        subject=f"{headline} resume" if headline else "Resume",
        invariant=1,
        pageCompression=1,
    )
    contact = [
        escape(str(value))
        for value in (
            printed_location(resume.person.location),
            resume.person.phone,
            resume.person.email,
        )
        if value
    ]
    contact.extend(
        f'<link href="{escape(str(link.url), quote=True)}">'
        f"{escape(printed_address(str(link.url)))}</link>"
        for link in resume.person.links
    )
    story: list[Flowable] = [Paragraph(escape(in_capitals(resume.person.name)), styles["name"])]
    if contact:
        story.append(Paragraph(" · ".join(contact), styles["contact"]))
    if headline:
        story.append(Paragraph(escape(in_capitals(headline)), styles["headline"]))
    if summary:
        summary_heading = Paragraph(escape(words.summary), styles["section"])
        first, *rest = (
            Paragraph(escape(paragraph), styles["body"])
            for paragraph in summary_paragraphs(summary)
        )
        _, first_height = first.wrap(doc.width, doc.height)
        # A first paragraph of ordinary length stays whole under its heading. A very long one is
        # kept with the heading for two lines only: kept whole, it would not fit under the name,
        # move to page 2 and leave page 1 nearly empty.
        first_lines = (
            first.split(doc.width, styles["body"].leading * 2)
            if first_height >= doc.height * 0.45
            else []
        )
        if len(first_lines) == 2:
            story.append(KeepTogether([summary_heading, first_lines[0]]))
            story.append(first_lines[1])
        else:
            story.append(KeepTogether([summary_heading, first]))
        story.extend(rest)
    skill_groups = [
        f"<b>{escape(skill.title)}:</b> {escape(' · '.join(skill.bullets))}"
        for skill in resume.technical_skills.skills
    ]
    if compact_skills is None:
        compact_skills = feature_on("compact_skills")
    if compact_skills and skill_groups:
        skill_groups = [" · ".join(skill_groups)]
    skills = [Paragraph(group, styles["skill"]) for group in skill_groups]
    if skills:  # a candidate who listed no skills gets no skills section
        heading = Paragraph(escape(words.skills), styles["section"])
        _, height = skills[0].wrap(doc.width, doc.height)
        # A long compact paragraph can span pages. Keep only its first two lines with the
        # heading, as for a long summary, so it does not empty the first page.
        first_lines = (
            skills[0].split(doc.width, styles["skill"].leading * 2)
            if compact_skills and height >= doc.height * 0.45
            else []
        )
        if len(first_lines) == 2:
            story.append(KeepTogether([heading, first_lines[0]]))
            story.append(first_lines[1])
        else:
            story.append(KeepTogether([heading, skills[0]]))
        story.extend(skills[1:])
    if resume.professional_experience:
        # SimpleDocTemplate's frame has six points of padding at each end. Test against the
        # usable frame, rather than an arbitrary fraction of a page.
        page_height = doc.height - 12
        for index, experience in enumerate(resume.professional_experience):
            heading, *bullets = _role(experience, styles, style, language)
            _, heading_height = heading.wrap(doc.width - 12, page_height)
            section_height = (
                styles["section"].leading + styles["section"].spaceAfter if index == 0 else 0
            )
            if heading_height + styles["bullet"].leading * 2 + section_height > page_height:
                raise ValueError("PDF role heading is too tall to render with its first bullet")
            introduction = (
                [Paragraph(escape(words.experience), styles["section"])] if index == 0 else []
            )
            story.append(HeadedBlock(heading, bullets, page_height, introduction=introduction))
    # One section for degrees and certifications. It is left out when there are neither.
    degree_blocks = [_education(education, styles, language) for education in resume.education]
    credentials = [paragraph for degree in degree_blocks for paragraph in degree]
    certification: Paragraph | None = None
    if resume.certifications.certifications:
        certification = Paragraph(
            f"<b>{escape(words.certifications)}:</b> "
            + " · ".join(
                _certification(line, language) for line in resume.certifications.certifications
            ),
            styles["education"],
        )
        credentials.append(certification)
    if credentials:
        heading = Paragraph(escape(words.education), styles["section"])
        page_height = doc.height - 12
        whole = HeadedBlock(heading, credentials, page_height)
        if whole.wrap(doc.width - 12, page_height)[1] <= page_height:
            story.append(whole)
        else:
            for index, degree in enumerate(degree_blocks):
                introduction = [heading] if index == 0 else []
                first_content = HeadedBlock(
                    degree[0], degree[1:3], page_height, introduction=introduction
                )
                # A school or degree name has no length limit. When its heading and first
                # useful content cannot share a page, flow the degree as content under the
                # short section heading instead of leaving an unsplittable giant heading.
                if first_content.wrap(doc.width - 12, page_height)[1] > page_height:
                    story.append(HeadedBlock(heading, degree, page_height))
                else:
                    story.append(
                        HeadedBlock(degree[0], degree[1:], page_height, introduction=introduction)
                    )
            if certification is not None:
                if degree_blocks:
                    story.append(certification)
                else:
                    story.append(HeadedBlock(heading, [certification], page_height))

    def footer(canvas: Canvas, _doc: SimpleDocTemplate) -> None:
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor(style.rule_color))
        canvas.setLineWidth(0.35)
        canvas.line(style.side_margin, 22.3, letter[0] - style.side_margin, 22.3)
        canvas.setFillColor(colors.HexColor(style.muted))
        canvas.setFont(_REGULAR, style.footer_font_size)
        page_right = letter[0] - style.side_margin
        page_label = f"{words.page} {canvas.getPageNumber()}"
        label_width = (
            page_right
            - pdfmetrics.stringWidth(page_label, _REGULAR, style.footer_font_size)
            - 8
            - style.side_margin
        )
        owner = f"{resume.person.name} · {headline}" if headline else resume.person.name
        label = _fit_footer(owner, label_width, style)
        canvas.drawString(style.side_margin, 13, label)
        canvas.drawRightString(page_right, 13, page_label)
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return with_pdf_properties(stream.getvalue(), modified_at=rendered_at)


def with_pdf_properties(buffer: bytes, *, modified_at: datetime) -> bytes:
    """Stamp rendered or cached PDFs with the saved resume change time in UTC."""
    stamp = modified_at.astimezone(UTC).strftime("D:%Y%m%d%H%M%S+00'00'")
    writer = PdfWriter(clone_from=BytesIO(buffer))
    writer.add_metadata({"/CreationDate": stamp, "/ModDate": stamp})
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def pdf_page_count(pdf: bytes) -> int:
    """How many pages a PDF made by `render_pdf` has."""
    return len(PdfReader(BytesIO(pdf)).pages)


class NoSuchPage(Exception):
    """The PDF has no page with this number."""


@dataclass(frozen=True)
class PagePicture:
    """One page of a PDF drawn as a PNG picture, and how many pages the PDF has."""

    png: bytes
    pages: int


# 144 dots an inch: sharp on a phone held close, and when a page is zoomed in.
_PICTURE_SCALE = 2
# pdfium may not be called from two threads at once, and pictures are drawn in worker threads.
_DRAWING_PAGES = Lock()


def pdf_page_picture(pdf: bytes, page: int) -> PagePicture:
    """A picture of one page, counted from 1, of a PDF made by `render_pdf`.

    It is drawn in shades of grey: the PDF prints only greys (`PdfStyle`), and a grey picture is
    a third of the size of a colour one.
    """
    with _DRAWING_PAGES:
        document = pdfium.PdfDocument(pdf)
        try:
            pages = len(document)
            if not 1 <= page <= pages:
                raise NoSuchPage(page)
            drawn = document[page - 1].render(scale=_PICTURE_SCALE, grayscale=True)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            try:
                # A copy: the picture reads pdfium's memory, which is freed here, under the lock,
                # and not later by the garbage collector in some other thread.
                image = cast("Image.Image", drawn.to_pil()).copy()  # pyright: ignore[reportUnknownMemberType]
            finally:
                drawn.close()  # pyright: ignore[reportUnknownMemberType]
        finally:
            document.close()
    output = BytesIO()
    image.save(output, format="PNG")
    return PagePicture(png=output.getvalue(), pages=pages)


def _open_output_directory(output_dir: Path) -> int:
    """Create/open each path component without following substituted symlinks."""
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in Path(os.path.abspath(output_dir)).parts[1:]:
            with suppress(FileExistsError):
                os.mkdir(component, dir_fd=directory_fd)
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except OSError as exc:
        os.close(directory_fd)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("PDF output directory cannot contain a symlink") from exc
        raise


def write_pdf(resume: Resume, output_dir: Path, name: str) -> Path:
    """Atomically write a plain PDF name, anchored to a non-symlink directory."""
    if (
        not name
        or name in {".", ".."}
        or Path(name).name != name
        or not name.lower().endswith(".pdf")
        or "\\" in name
    ):
        raise ValueError("Output name must be a plain .pdf filename")
    pdf_data = render_pdf(resume)
    directory_fd = _open_output_directory(output_dir)
    temp_name = f".resume-{uuid4().hex}.tmp"
    temp_created = False
    try:
        try:
            if stat.S_ISLNK(os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode):
                raise ValueError("PDF output cannot be a symlink")
        except FileNotFoundError:
            pass
        file_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        temp_created = True
        with os.fdopen(file_fd, "wb") as handle:
            handle.write(pdf_data)
        # Both lookups are relative to the same opened directory, even if
        # another process replaces a directory name during the write.
        os.replace(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temp_created = False
        try:
            current = os.stat(output_dir, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError("PDF output directory changed during write") from exc
        opened = os.fstat(directory_fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("PDF output directory changed during write")
    finally:
        if temp_created:
            os.unlink(temp_name, dir_fd=directory_fd)
        os.close(directory_fd)
    return output_dir / name


def render_letter_pdf(
    person: Person, text: str, *, style: PdfStyle = DEFAULT_PDF_STYLE, clock: Clock = utc_now
) -> bytes:
    """A cover letter in the resume's look: the person's name and contact details at the top,
    as the resume prints them, then the letter, a paragraph for each block of lines (pull
    request 657). Raises `UnprintableCharacters` for characters the bundled font lacks."""
    rendered_at = clock()
    _fonts()
    contact_values = [
        str(value) for value in (person.location, person.phone, person.email) if value
    ]
    links = [(str(link.url), printed_address(str(link.url))) for link in person.links]
    shown = [address for _, address in links]
    missing = unprintable_characters("\n".join((person.name, *contact_values, *shown, text)))
    if missing:
        raise UnprintableCharacters(missing)
    text = without_variation_selectors(text)
    styles = _styles(style)
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=letter,
        topMargin=style.top_margin + 12,
        bottomMargin=style.bottom_margin,
        leftMargin=style.side_margin + 24,
        rightMargin=style.side_margin + 24,
        title=f"{person.name} - Cover letter",
        author=person.name,
        subject="Cover letter",
        invariant=1,
        pageCompression=1,
    )
    contact = [escape(value) for value in contact_values]
    contact.extend(
        f'<link href="{escape(url, quote=True)}">{escape(address)}</link>' for url, address in links
    )
    story: list[Flowable] = [Paragraph(escape(in_capitals(person.name)), styles["name"])]
    if contact:
        story.append(Paragraph(" · ".join(contact), styles["contact"]))
    letter_style = ParagraphStyle(
        "letter", parent=styles["body"], fontSize=10, leading=13.5, spaceAfter=9
    )
    story.append(Paragraph("", ParagraphStyle("gap", parent=letter_style, spaceAfter=12)))
    for block in text.split("\n\n"):
        lines = [escape(line.strip()) for line in block.splitlines() if line.strip()]
        if lines:
            story.append(Paragraph("<br/>".join(lines), letter_style))
    doc.build(story)
    return with_pdf_properties(stream.getvalue(), modified_at=rendered_at)
