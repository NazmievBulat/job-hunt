"""PDF adapter acceptance and page-boundary regressions."""

import json
import os
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from threading import Thread
from typing import cast

import pypdfium2 as pdfium  # pyright: ignore[reportMissingTypeStubs]
import pytest
from movable_clock import MovableClock
from PIL import Image
from pypdf import PdfReader
from pypdf._page import PageObject
from reportlab.pdfbase import pdfmetrics

from resume_kit.contracts.resume import Resume
from resume_kit.documents import pdf as pdf_module
from resume_kit.documents.pdf import (
    NoSuchPage,
    PdfStyle,
    UnprintableCharacters,
    pdf_page_count,
    pdf_page_picture,
    render_pdf,
    write_pdf,
)

ROOT = Path(__file__).resolve().parents[2]


def _resume() -> Resume:
    return Resume.model_validate_json((ROOT / "examples/resume.example.json").read_text())


def test_pdf_contains_canonical_facts_and_letter_pages() -> None:
    resume = _resume()
    clock = MovableClock()
    data = render_pdf(resume, clock=clock)
    reader = PdfReader(BytesIO(data))
    assert data.startswith(b"%PDF-")
    assert len(reader.pages) == 1
    assert all(
        (float(page.mediabox.width), float(page.mediabox.height)) == (612, 792)
        for page in reader.pages
    )
    text = "\n".join(page.extract_text() for page in reader.pages)
    for expected in (
        "JOHN DOE",
        "john.doe@example.com",
        "github.com/john-doe",
        "linkedin.com/in/john-doe",
        "SOFTWARE ENGINEER",
        "SUMMARY",
        "Software engineer who builds reliable web applications.",
        "SKILLS",
        "FastAPI",
        "PROFESSIONAL EXPERIENCE",
        "EXAMPLE INC.",
        "Built a web application used by thousands of customers.",
        "EDUCATION & CREDENTIALS",
        "University of California, Los Angeles",
        "Graduated with honors.",
        "AWS Certified Developer",
        "Page 1",
    ):
        assert expected in text
    assert reader.metadata is not None
    assert reader.metadata.title == "John Doe - Software Engineer"
    assert reader.metadata.author == "John Doe"
    assert reader.metadata.subject == "Software Engineer resume"
    assert render_pdf(resume, clock=clock) == data


def test_a_year_alone_is_printed_as_it_was_saved() -> None:
    data = json.loads((ROOT / "examples/resume.example.json").read_text())
    data["professional_experience"][0].update(start_date="2020", end_date="2023-06")
    data["education"][0].update(start_date="2010", end_date="2014")

    text = "\n".join(
        page.extract_text()
        for page in PdfReader(BytesIO(render_pdf(Resume.model_validate(data)))).pages
    )

    assert "2020 \u2013 Jun 2023" in text
    assert "2010 \u2013 2014" in text


def test_pdf_style_preset_changes_name_size_without_losing_content() -> None:
    resume = _resume()
    style = replace(PdfStyle(), name_font_size=22)
    original = PdfReader(BytesIO(render_pdf(resume))).pages[0]
    changed = PdfReader(BytesIO(render_pdf(resume, style=style))).pages[0]
    assert changed.extract_text() == original.extract_text()
    sizes: list[float] = []

    def name_size(
        text: str, _cm: list[float], _tm: list[float], _font: object, size: float
    ) -> None:
        if "JOHN DOE" in text:
            sizes.append(size)

    changed.extract_text(visitor_text=name_size)
    assert 22 in sizes


def test_pdf_writer_does_not_overwrite_an_existing_hard_link(tmp_path: Path) -> None:
    private = tmp_path / "private.pdf"
    private.write_bytes(b"private existing content")
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    destination = output_dir / "resume.pdf"
    destination.hardlink_to(private)

    write_pdf(_resume(), output_dir, "resume.pdf")

    assert private.read_bytes() == b"private existing content"
    assert destination.read_bytes().startswith(b"%PDF-")


def test_output_directory_swap_cannot_redirect_pdf_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    relocated = tmp_path / "relocated"
    external = tmp_path / "external"
    external.mkdir()
    original_replace = os.replace

    def swap_before_replace(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        output_dir.rename(relocated)
        output_dir.symlink_to(external, target_is_directory=True)
        original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", swap_before_replace)
    with pytest.raises(ValueError, match="changed"):
        write_pdf(_resume(), output_dir, "resume.pdf")
    assert not (external / "resume.pdf").exists()
    assert (relocated / "resume.pdf").read_bytes().startswith(b"%PDF-")


def test_long_valid_bullet_does_not_leave_a_header_only_first_page() -> None:
    # Taller than a fresh page: a necessary split must still start under the heading.
    data = _resume().model_dump(mode="json")
    data["professional_experience"][0]["bullets"][0] = "X" * 12000
    resume = Resume.model_validate(data)
    pages = PdfReader(BytesIO(render_pdf(resume))).pages
    first_page = pages[0].extract_text()
    assert "PROFESSIONAL EXPERIENCE" in first_page
    assert "EXAMPLE INC." in first_page.upper()
    assert first_page.count("X") > 0


def test_long_bullet_does_not_orphan_role_heading_at_page_bottom() -> None:
    data = _resume().model_dump(mode="json")
    first_role = data["professional_experience"][0]
    next_role = deepcopy(first_role)
    first_role["bullets"] = ["X" * 4580]
    next_role["company_name"] = "SECONDROLEMARKER"
    next_role["bullets"] = ["SECONDSTART " + "Z" * 6000]
    data["professional_experience"] = [first_role, next_role]
    pdf = render_pdf(Resume.model_validate(data))
    pages = [page.extract_text() for page in PdfReader(BytesIO(pdf)).pages]
    assert any("SECONDROLEMARKER" in page for page in pages)
    assert "".join(pages).count("Z") == 6000
    assert all("SECONDSTART" in page for page in pages if "SECONDROLEMARKER" in page)


def test_role_title_taller_than_page_is_rejected_without_losing_facts() -> None:
    data = _resume().model_dump(mode="json")
    data["professional_experience"][0]["work_title"] = "Engineer " * 1000
    resume = Resume.model_validate(data)
    with pytest.raises(ValueError, match="role heading"):
        render_pdf(resume)


def test_characters_the_font_lacks_are_named_once_each_in_the_order_they_appear() -> None:
    data = _resume().model_dump(mode="json")
    data["summary"]["summary_description"] = "Cut latency → 40 ms ✓, then → 20 ms."

    with pytest.raises(UnprintableCharacters) as caught:
        render_pdf(Resume.model_validate(data))

    assert caught.value.characters == ("→", "✓")


def test_pdf_prints_each_links_address_and_links_it() -> None:
    data = _resume().model_dump(mode="json")
    data["person"]["links"][0]["label"] = "Portfolio <work> & projects"
    resume = Resume.model_validate(data)
    reader = PdfReader(BytesIO(render_pdf(resume)))
    text = "\n".join(page.extract_text() for page in reader.pages)
    contact = next(line for line in text.splitlines() if "github.com" in line)
    # On paper a label leads nowhere: the line holds the address, which a reader can type.
    assert contact.endswith(" · github.com/john-doe · linkedin.com/in/john-doe")
    assert "Portfolio" not in text
    assert "LinkedIn" not in contact
    assert "https://" not in text
    assert resume.person.email is not None and str(resume.person.email) in text
    assert resume.person.phone is not None and resume.person.phone in text
    targets = [
        str(annotation.get_object()["/A"]["/URI"])
        for page in reader.pages
        for annotation in page.get("/Annots", [])
        if annotation.get_object().get("/Subtype") == "/Link"
    ]
    assert sorted(targets) == sorted(str(link.url) for link in resume.person.links)
    assert not any(target.startswith(("mailto:", "tel:")) for target in targets)


def test_an_address_with_an_ampersand_is_printed_as_saved() -> None:
    data = _resume().model_dump(mode="json")
    data["person"]["links"] = [{"label": "Work", "url": "https://example.com/?a=1&b=2"}]
    text = "\n".join(
        page.extract_text()
        for page in PdfReader(BytesIO(render_pdf(Resume.model_validate(data)))).pages
    )
    assert "example.com/?a=1&b=2" in text


def test_a_label_the_font_lacks_is_not_printed_and_so_not_refused() -> None:
    data = _resume().model_dump(mode="json")
    data["person"]["links"][0]["label"] = "Portfolio ✓"
    assert render_pdf(Resume.model_validate(data))


def test_long_footer_stays_inside_page_and_clear_of_page_number() -> None:
    data = _resume().model_dump(mode="json")
    data["person"]["name"] = "Alexandria " * 18
    # The longest headline the contract allows: fifteen words, each of them long.
    data["summary"]["text"] = "Infrastructure-Reliability-Engineering " * 15
    resume = Resume.model_validate(data)
    reader = PdfReader(BytesIO(render_pdf(resume)))
    assert len(reader.pages) >= 1

    def footer_fragments(page: PageObject) -> list[tuple[str, float, float, float]]:
        footer: list[tuple[str, float, float, float]] = []

        def collect_footer(
            text: str, _cm: list[float], tm: list[float], _font: object, size: float
        ) -> None:
            if text.strip() and abs(tm[5] - 13) < 0.1:
                footer.append((text.strip(), tm[4], tm[5], size))

        page.extract_text(visitor_text=collect_footer)
        return footer

    for number, page in enumerate(reader.pages, start=1):
        footer = footer_fragments(page)
        assert len(footer) == 2
        label, number_label = sorted(footer, key=lambda item: item[1])
        assert label[0].startswith("Alexandria") and label[0].endswith("…")
        assert number_label[0] == f"Page {number}"
        assert label[2] == number_label[2] == 13
        assert label[3] == number_label[3] == 7
        label_right = label[1] + pdfmetrics.stringWidth(label[0], "ResumeNotoSans", 7)
        assert label[1] >= 34.5
        assert label_right + 8 <= number_label[1]
        number_right = number_label[1] + pdfmetrics.stringWidth(
            number_label[0], "ResumeNotoSans", 7
        )
        assert number_right <= 577.5


def test_output_name_cannot_escape_directory(tmp_path: Path) -> None:
    resume = _resume()
    for name in ("../escape.pdf", "/tmp/escape.pdf", "sub/file.pdf", "..", "", "resume.docx"):
        with pytest.raises(ValueError):  # noqa: PT011 - only the refusal type is the contract here
            write_pdf(resume, tmp_path, name)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"untouched")
    (tmp_path / "link.pdf").symlink_to(outside)
    with pytest.raises(ValueError):  # noqa: PT011 - only the refusal type is the contract here
        write_pdf(resume, tmp_path, "link.pdf")
    assert outside.read_bytes() == b"untouched"
    result = write_pdf(resume, tmp_path, "resume.pdf")
    assert result == tmp_path / "resume.pdf"
    assert result.read_bytes().startswith(b"%PDF-")


def test_output_directory_symlinks_are_rejected(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(external, target_is_directory=True)
    for output_dir in (link, link / "nested"):
        with pytest.raises(ValueError, match="symlink"):
            write_pdf(_resume(), output_dir, "resume.pdf")
    assert not (external / "resume.pdf").exists()
    assert not (external / "nested").exists()


def test_unicode_and_escaped_user_text_remains_searchable() -> None:
    data = _resume().model_dump(mode="json")
    data["person"]["name"] = "Zoë Müller"
    data["summary"]["summary_description"] = "Built <safe> & reliable systems in München."
    data["professional_experience"][0]["bullets"][0] = "Improved café reliability for Zürich teams."
    reader = PdfReader(BytesIO(render_pdf(Resume.model_validate(data))))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "ZOË MÜLLER" in text
    assert "Built <safe> & reliable systems in München." in text
    assert "Improved café reliability for Zürich teams." in text


@pytest.mark.parametrize(
    "summary",
    [
        "Builds Python services.\n\nWorks with design teams.",
        "Builds Python services.\nWorks with design teams.",
        "Builds Python services. \r\n \r\n\n  Works with design teams.",
        "Builds Python services.\n\u0085\n\ufeff\u200b\nWorks with design teams.",
    ],
)
def test_each_line_break_of_the_summary_starts_a_paragraph(summary: str) -> None:
    # The editor shows a paragraph after each Enter; the PDF ran them together into one line.
    data = _resume().model_dump(mode="json")
    data["summary"]["summary_description"] = summary
    page = PdfReader(BytesIO(render_pdf(Resume.model_validate(data)))).pages[0]
    heights: dict[str, float] = {}

    def line_height(text: str, cm: list[float], tm: list[float], font: object, size: float) -> None:
        heights.setdefault(text.strip(), cm[5] + tm[5])

    lines = page.extract_text(visitor_text=line_height).splitlines()
    start = lines.index("SUMMARY") + 1

    assert lines[start : start + 3] == [
        "Builds Python services.",
        "Works with design teams.",
        "SKILLS",
    ]
    # One paragraph under the other, the second set apart by the gap after a paragraph, as on
    # the page in the browser: more than the step from one line to the next.
    step = heights["Builds Python services."] - heights["Works with design teams."]
    assert step > PdfStyle().body_leading


@pytest.mark.parametrize("field", ["name", "bullet"])
def test_unsupported_font_glyphs_fail_before_losing_candidate_facts(field: str) -> None:
    data = _resume().model_dump(mode="json")
    if field == "name":
        data["person"]["name"] = "李明"
    else:
        data["professional_experience"][0]["bullets"][0] = "علي built systems."
    resume = Resume.model_validate(data)
    with pytest.raises(ValueError, match="font cannot render") as caught:
        render_pdf(resume)
    assert "李明" not in str(caught.value)
    assert "علي" not in str(caught.value)


def test_final_role_heading_stays_with_first_bullet_on_two_page_boundary() -> None:
    fixture = ROOT / "tests/documents/fixtures/two_page_boundary.json"
    resume = Resume.model_validate(json.loads(fixture.read_text()))
    reader = PdfReader(BytesIO(render_pdf(resume)))
    assert len(reader.pages) == 2
    pages = [page.extract_text() for page in reader.pages]
    title = "BOUNDARY COMPANY — Final Role"
    first = "Boundary first bullet anchors its role heading on the next page."
    assert title in pages[1] and first in pages[1]
    assert title not in pages[0] and first not in pages[0]
    assert "Boundary continuation bullet" in pages[1]
    assert "EDUCATION & CREDENTIALS" in pages[1]
    assert "Page 1" in pages[0] and "Page 2" in pages[1]


def _with_emoji_style_marks(value: object) -> object:
    """Every text of a dumped resume with "©" and the invisible mark a phone adds after it."""
    if isinstance(value, dict):
        kept_as_they_are = ("url", "email", "start_date", "end_date", "language")
        return {
            key: item if key in kept_as_they_are else _with_emoji_style_marks(item)
            for key, item in cast("dict[str, object]", value).items()
        }
    if isinstance(value, list):
        return [_with_emoji_style_marks(item) for item in cast("list[object]", value)]
    return f"{value} ©\ufe0f" if isinstance(value, str) else value


def test_the_invisible_mark_a_phone_adds_after_a_sign_is_left_out_of_every_text() -> None:
    # Drawn as it is, the mark would be an empty box: the font has no shape for it.
    data = _with_emoji_style_marks(_resume().model_dump(mode="json"))
    assert isinstance(data, dict)
    # Row limits are counted in characters, so the skills keep their size.
    data["technical_skills"] = {"skills": [{"title": "Tools ™️", "bullets": ["Go ®️"]}]}
    resume = Resume.model_validate(data)

    reader = PdfReader(BytesIO(render_pdf(resume)))
    text = "\n".join(page.extract_text() for page in reader.pages)

    assert "\x00" not in text
    assert "️" not in text
    assert text.count("©") >= 10
    assert "Tools ™" in text and "Go ®" in text


@pytest.mark.parametrize(("summary_words", "pages"), [(20, 1), (700, 2), (2500, 3)])
def test_the_pages_of_a_pdf_are_counted(summary_words: int, pages: int) -> None:
    data = _resume().model_dump(mode="json")
    data["summary"]["summary_description"] = " ".join(["latency"] * summary_words)
    pdf = render_pdf(Resume.model_validate(data))

    assert pdf_page_count(pdf) == pages
    # Read by a second library, so the count is not only checked against itself.
    assert len(pdfium.PdfDocument(pdf)) == pages


# 915 words are a few lines taller than the room under the name and shorter than a whole page;
# 1,000 words are taller than a whole page.
@pytest.mark.parametrize("words", [915, 1000])
def test_a_summary_too_tall_for_the_first_page_starts_there_and_runs_on(words: int) -> None:
    # Kept whole with its heading, such a summary moved to page 2 and left page 1 with only the
    # name on it.
    data = _resume().model_dump(mode="json")
    data["summary"]["summary_description"] = " ".join(["latency"] * words)
    pages = PdfReader(BytesIO(render_pdf(Resume.model_validate(data)))).pages

    first = pages[0].extract_text()
    assert "SUMMARY" in first
    assert first.count("latency") > 500
    assert sum(page.extract_text().count("latency") for page in pages) == words
    assert len(pages) == 2


def test_a_second_paragraph_after_a_summary_too_tall_for_the_first_page_comes_after_it() -> None:
    data = _resume().model_dump(mode="json")
    data["summary"]["summary_description"] = " ".join(["latency"] * 915) + "\n\nSecond paragraph."
    pages = PdfReader(BytesIO(render_pdf(Resume.model_validate(data)))).pages
    text = "\n".join(page.extract_text() for page in pages)

    assert "SUMMARY" in pages[0].extract_text()
    assert text.index("latency\nSecond paragraph.\n") == text.rindex("latency")


def test_two_first_renders_at_once_both_find_both_fonts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rendering runs in worker threads: the worker, a hand edit and a download can each be the
    # first to need the fonts after the server starts. One that arrives while another is still
    # registering them must wait, not go on with only the regular font registered.
    registered: list[str] = []

    def slow_register(font: object) -> None:
        time.sleep(0.05)
        registered.append(cast("str", font))

    monkeypatch.setattr(pdf_module.pdfmetrics, "getRegisteredFontNames", lambda: list(registered))
    monkeypatch.setattr(pdf_module.pdfmetrics, "registerFont", slow_register)

    def no_family(*names: object, **faces: object) -> None:
        return None

    def font_by_name(name: str, path: str) -> str:
        return name

    monkeypatch.setattr(pdf_module.pdfmetrics, "registerFontFamily", no_family)
    monkeypatch.setattr(pdf_module, "TTFont", font_by_name)
    seen_on_return: list[list[str]] = []

    def first_render() -> None:
        pdf_module._fonts()  # pyright: ignore[reportPrivateUsage]
        seen_on_return.append(list(registered))

    first, second = Thread(target=first_render), Thread(target=first_render)
    first.start()
    deadline = time.monotonic() + 5
    while registered != ["ResumeNotoSans"] and time.monotonic() < deadline:
        time.sleep(0.001)
    # The regular font is registered and the bold one is not yet: the second render arrives.
    second.start()
    first.join(5)
    second.join(5)

    fonts = ["ResumeNotoSans", "ResumeNotoSans-Bold"]
    assert seen_on_return == [fonts, fonts]


def _pdf_text(resume: Resume) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(resume))).pages)


def test_a_resume_in_french_prints_the_apostrophe_of_its_word_for_present() -> None:
    # The word goes through the same escaping as the profile's text, and must come out whole.
    path = Path(__file__).resolve().parents[2] / "examples" / "resume-page" / "german.json"
    resume = Resume.model_validate_json(path.read_text(encoding="utf-8"))
    text = _pdf_text(resume.model_copy(update={"language": "fr"}))

    assert "mars 2019 \u2013 aujourd'hui" in text
    assert "&#x27;" not in text
    assert "Page 1" in text


def test_the_footer_of_a_resume_in_german_says_seite() -> None:
    path = Path(__file__).resolve().parents[2] / "examples" / "resume-page" / "german.json"
    text = _pdf_text(Resume.model_validate_json(path.read_text(encoding="utf-8")))

    assert "Seite 1" in text
    assert "Page 1" not in text


def test_pdf_dates_follow_the_movable_clock_in_utc() -> None:
    clock = MovableClock(datetime(2026, 10, 19, 15, 4, 5, tzinfo=timezone(timedelta(hours=3))))
    resume = _resume()
    first = render_pdf(resume, clock=clock)
    assert render_pdf(resume, clock=clock) == first
    for data, expected in ((first, clock.now), (render_pdf(resume, clock=clock), clock.now)):
        metadata = PdfReader(BytesIO(data)).metadata
        assert metadata is not None
        assert metadata.creation_date == expected.astimezone(UTC)
        assert metadata.modification_date == expected.astimezone(UTC)
        assert metadata.creation_date_raw == "D:20261019120405+00'00'"
    clock.advance(days=1)
    later = render_pdf(resume, clock=clock)
    assert later != first
    metadata = PdfReader(BytesIO(later)).metadata
    assert metadata is not None
    assert metadata.creation_date == clock.now.astimezone(UTC)
    assert metadata.modification_date == clock.now.astimezone(UTC)


def test_pdf_default_dates_are_the_time_of_rendering() -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    metadata = PdfReader(BytesIO(render_pdf(_resume()))).metadata
    after = datetime.now(UTC)
    assert metadata is not None
    assert metadata.creation_date is not None
    assert before <= metadata.creation_date <= after
    assert metadata.modification_date == metadata.creation_date


def _ink(picture: bytes) -> int:
    """How many dots of a page picture are not white."""
    with Image.open(BytesIO(picture)) as image:
        histogram = image.convert("L").histogram()
    return sum(histogram[:250])


def test_each_page_of_a_pdf_is_drawn_as_a_picture_of_a_letter_page() -> None:
    fixture = ROOT / "tests/documents/fixtures/two_page_boundary.json"
    pdf = render_pdf(Resume.model_validate(json.loads(fixture.read_text())))

    first = pdf_page_picture(pdf, 1)
    second = pdf_page_picture(pdf, 2)

    assert (first.pages, second.pages) == (2, 2)
    for picture in (first, second):
        with Image.open(BytesIO(picture.png)) as image:
            assert image.format == "PNG"
            # Letter at 144 dots an inch: 8.5 by 11 inches.
            assert image.size == (1224, 1584)
    # Page 1 is full, page 2 holds the last role and the education: the right page is drawn.
    assert _ink(first.png) > 3 * _ink(second.png) > 0


@pytest.mark.parametrize("page", [0, 3, -1])
def test_a_page_the_pdf_does_not_have_is_not_drawn(page: int) -> None:
    fixture = ROOT / "tests/documents/fixtures/two_page_boundary.json"
    pdf = render_pdf(Resume.model_validate(json.loads(fixture.read_text())))

    with pytest.raises(NoSuchPage):
        pdf_page_picture(pdf, page)


def test_the_pdf_prints_only_greys_so_its_page_pictures_are_drawn_in_greys() -> None:
    # The pictures are drawn in shades of grey, a third of the size of colour ones. A colour in
    # the PDF's style would be lost in them.
    style = PdfStyle()
    for colour in (style.ink, style.muted, style.rule_color):
        red, green, blue = (colour[1:3], colour[3:5], colour[5:7])
        assert red == green == blue, colour


def test_a_drawn_page_is_let_go_of_while_no_other_page_can_be_drawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pdfium may not be called from two threads at once. Left to the garbage collector, the
    # drawn page would be freed later, in whichever thread, while another page is drawn.
    lock = pdf_module._DRAWING_PAGES  # pyright: ignore[reportPrivateUsage]
    held_when_freed: list[bool] = []
    close = pdfium.PdfBitmap.close  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def recording_close(bitmap: pdfium.PdfBitmap) -> None:
        held_when_freed.append(lock.locked())
        close(bitmap)

    monkeypatch.setattr(pdfium.PdfBitmap, "close", recording_close)
    picture = pdf_page_picture(render_pdf(_resume()), 1)

    assert held_when_freed == [True]
    # The picture was copied out before its memory was freed.
    with Image.open(BytesIO(picture.png)) as image:
        assert image.size == (1224, 1584)
    assert _ink(picture.png) > 0
