"""A career the read-resume skill wrote from a resume, checked and saved as `hunt career` saves one.

The skill writes a draft with no ids; the jobs get the ids an export's jobs get, so reading the
same resume again gives the same ids. A draft missing what a resume prints is refused, naming
each thing missing, and nothing is saved."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from job_hunt.career import Career, CareerError, career_from_resume, read_career, with_ready_answers, write_career
from job_hunt.cli import main

DRAFT = {
    "person": {"name": "Ada Example", "email": "ada@example.com", "phone": "+1 555 010 0000",
               "location": "Durham, NC", "links": [{"url": "https://example.com/ada", "label": "Site"}]},
    "summary": "Test engineer.",
    "titles": ["QA Automation Engineer"],
    "skills": ["Python", "Playwright"],
    "jobs": [
        {"title": "QA Engineer", "company": "Acme", "location": "Remote", "start_date": "2018-03",
         "end_date": "2020-12", "material": ["Wrote the first tests."]},
        {"title": "Lead SDET", "company": "Initech, Inc.", "location": None, "start_date": "2021-01",
         "end_date": None, "material": ["Built the test framework.", "Ran CI."]},
    ],
    "education": [{"school": "Example State University", "degree": "Bachelor's degree",
                   "field_of_study": "Physics", "start_date": "2010", "end_date": "2014"}],
    "certifications": ["ISTQB Foundation"],
}


def draft(**changes: object) -> dict:
    copied = copy.deepcopy(DRAFT)
    copied.update(changes)
    return copied


def test_the_jobs_get_the_ids_an_export_gives_them_newest_first() -> None:
    career = career_from_resume(DRAFT)

    assert [job.id for job in career.jobs] == ["initech-inc-2021-01", "acme-2018-03"]
    assert (career.jobs[0].company, career.jobs[0].title, career.jobs[0].end_date) == (
        "Initech, Inc.", "Lead SDET", None)
    assert career.jobs[0].material == ["Built the test framework.", "Ran CI."]
    assert career.person["name"] == "Ada Example"
    assert career.education[0].field_of_study == "Physics"
    assert career.certifications == ["ISTQB Foundation"]


def test_reading_the_same_resume_again_gives_the_same_ids_whatever_the_order() -> None:
    again = career_from_resume(draft(jobs=list(reversed(DRAFT["jobs"]))))

    assert [job.id for job in again.jobs] == [job.id for job in career_from_resume(DRAFT).jobs]


def test_two_jobs_at_one_employer_from_the_same_month_get_ids_of_their_own() -> None:
    jobs = [*DRAFT["jobs"], {**DRAFT["jobs"][0], "title": "QA Lead"}]

    ids = [job.id for job in career_from_resume(draft(jobs=jobs)).jobs]

    assert sorted(ids) == ["acme-2018-03", "acme-2018-03-2", "initech-inc-2021-01"]


def test_a_job_without_an_employer_is_refused_by_its_title() -> None:
    jobs = [DRAFT["jobs"][0], {**DRAFT["jobs"][1], "company": "  "}]

    with pytest.raises(CareerError, match=r'job 2 \("Lead SDET"\) has no employer'):
        career_from_resume(draft(jobs=jobs))


def test_a_job_without_a_start_date_is_refused() -> None:
    jobs = [{**DRAFT["jobs"][0], "start_date": None}, DRAFT["jobs"][1]]

    with pytest.raises(CareerError, match=r'job 1 \("QA Engineer" at Acme\) has no start date'):
        career_from_resume(draft(jobs=jobs))


@pytest.mark.parametrize("date", ["March 2018", "2018-13", "03/2018"])
def test_a_date_not_written_as_a_year_and_month_is_refused(date: str) -> None:
    jobs = [{**DRAFT["jobs"][0], "start_date": date}, DRAFT["jobs"][1]]

    with pytest.raises(CareerError, match="start date .* is not a year or a year and month"):
        career_from_resume(draft(jobs=jobs))


def test_a_job_that_ends_before_it_starts_is_refused() -> None:
    jobs = [{**DRAFT["jobs"][0], "end_date": "2017-01"}, DRAFT["jobs"][1]]

    with pytest.raises(CareerError, match="ends before it starts"):
        career_from_resume(draft(jobs=jobs))


def test_every_problem_is_named_at_once() -> None:
    jobs = [{**DRAFT["jobs"][0], "company": ""}, {**DRAFT["jobs"][1], "start_date": ""}]

    with pytest.raises(CareerError) as refused:
        career_from_resume(draft(jobs=jobs, person={"name": "Ada Example"}))

    said = str(refused.value)
    assert "has no employer" in said and "has no start date" in said and "no email" in said


def test_a_field_the_career_does_not_have_is_refused_not_dropped() -> None:
    jobs = [{**DRAFT["jobs"][0], "employer": "Acme"}, DRAFT["jobs"][1]]

    with pytest.raises(CareerError, match="employer"):
        career_from_resume(draft(jobs=jobs))


def test_a_draft_without_jobs_is_refused() -> None:
    with pytest.raises(CareerError, match="no jobs"):
        career_from_resume(draft(jobs=[]))


def test_the_form_answers_of_the_career_it_replaces_are_kept() -> None:
    kept = career_from_resume(DRAFT)
    kept.form_answers = {"ready": {"authorized": "yes"}, "saved": [{"question": "Q?", "answer": "A", "company": "Acme"}]}

    assert career_from_resume(DRAFT, kept=kept).form_answers == kept.form_answers


def test_the_saved_career_reads_back_as_it_was_written(tmp_path: Path) -> None:
    path = tmp_path / "career.json"
    career = career_from_resume(DRAFT)

    write_career(career, path)

    assert read_career(path) == career


def test_the_standing_answers_are_saved_beside_the_answers_saved_from_forms() -> None:
    career = career_from_resume(DRAFT)
    career.form_answers = {"saved": [{"question": "Q?", "answer": "A", "company": "Acme"}],
                           "ready": {"authorized": "yes", "salary": "120000"}}

    changed = with_ready_answers(career, {"sponsorship": "no", "notice": "two weeks", "relocate": "no"})

    assert changed.form_answers == {
        "saved": [{"question": "Q?", "answer": "A", "company": "Acme"}],
        "ready": {"authorized": "yes", "salary": "120000", "sponsorship": "no", "notice": "two weeks",
                  "relocate": "no"},
    }


@pytest.mark.parametrize("answers, named", [
    ({"authorized": "maybe"}, "authorized"),
    ({"gender": "x"}, "gender"),
    ({"notice": "x" * 101}, "notice"),
])
def test_a_standing_answer_the_forms_cannot_use_is_refused(answers: dict, named: str) -> None:
    with pytest.raises(CareerError, match=named):
        with_ready_answers(career_from_resume(DRAFT), answers)


def test_the_command_shows_what_was_read_and_saves_only_when_told(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from job_hunt import cli

    career_file = tmp_path / "career.json"
    monkeypatch.setattr(cli, "CAREER", career_file)
    written = tmp_path / "career-draft.json"
    written.write_text(json.dumps(DRAFT))

    assert main(["career-from-resume", str(written)]) == 0
    shown = capsys.readouterr().out
    assert "initech-inc-2021-01: Lead SDET at Initech, Inc., 2021-01 to now (2 lines of what was done)" in shown
    assert "acme-2018-03: QA Engineer at Acme, Remote, 2018-03 to 2020-12 (1 line of what was done)" in shown
    assert "Bachelor's degree, Physics at Example State University" in shown
    assert "not saved" in shown
    assert not career_file.exists()

    assert main(["career-from-resume", str(written), "--save"]) == 0
    assert f"saved to {career_file}" in capsys.readouterr().out
    assert [job.id for job in read_career(career_file).jobs] == ["initech-inc-2021-01", "acme-2018-03"]


def test_the_command_refuses_a_draft_and_leaves_the_career_as_it_was(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from job_hunt import cli

    career_file = tmp_path / "career.json"
    write_career(career_from_resume(DRAFT), career_file)
    before = career_file.read_text()
    monkeypatch.setattr(cli, "CAREER", career_file)
    written = tmp_path / "career-draft.json"
    written.write_text(json.dumps(draft(jobs=[{**DRAFT["jobs"][0], "company": None}])))

    assert main(["career-from-resume", str(written), "--save"]) == 1
    assert "has no employer" in capsys.readouterr().err
    assert career_file.read_text() == before


def test_the_command_saves_the_standing_answers_into_the_career(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from job_hunt import cli

    career_file = tmp_path / "career.json"
    write_career(career_from_resume(DRAFT), career_file)
    monkeypatch.setattr(cli, "CAREER", career_file)

    assert main(["form-answers", '{"authorized": "yes", "sponsorship": "no"}']) == 0
    assert read_career(career_file).form_answers == {"ready": {"authorized": "yes", "sponsorship": "no"}}
    assert main(["form-answers", '{"authorized": "perhaps"}']) == 1
    assert "authorized" in capsys.readouterr().err
    assert read_career(career_file).form_answers == {"ready": {"authorized": "yes", "sponsorship": "no"}}


def test_the_career_type_is_the_one_hunt_reads() -> None:
    assert isinstance(career_from_resume(DRAFT), Career)


def test_the_text_of_a_word_resume_is_read_with_its_table_cells(tmp_path: Path) -> None:
    from docx import Document

    from job_hunt.career import resume_text

    document = Document()
    document.add_paragraph("Ada Example")
    document.add_table(rows=1, cols=2).rows[0].cells[0].text = "Acme, 2018-03 to 2020-12"
    document.save(tmp_path / "resume.docx")

    text = resume_text(tmp_path / "resume.docx")

    assert "Ada Example" in text and "Acme, 2018-03 to 2020-12" in text


def test_the_text_of_a_pdf_resume_is_read(tmp_path: Path) -> None:
    from reportlab.pdfgen.canvas import Canvas

    from job_hunt.career import resume_text

    canvas = Canvas(str(tmp_path / "resume.pdf"))
    canvas.drawString(72, 720, "Ada Example, QA Engineer at Acme")
    canvas.save()

    assert "Ada Example, QA Engineer at Acme" in resume_text(tmp_path / "resume.pdf")


def test_a_file_that_is_not_a_pdf_or_word_resume_is_refused(tmp_path: Path) -> None:
    from job_hunt.career import resume_text

    (tmp_path / "resume.pages").write_bytes(b"x")

    with pytest.raises(CareerError, match="PDF or a Word"):
        resume_text(tmp_path / "resume.pages")
