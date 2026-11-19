"""The career cleaned from an export, and a resume built from a worker's content."""

from __future__ import annotations

import pytest
from pypdf import PdfReader
from io import BytesIO

from resume_kit.documents.pdf import render_pdf
from job_hunt import cli
from job_hunt.career import career_from_export, write_career
from job_hunt.resume import ContentError, build_resume

EXPORT = {
    "profile": {"person": {"name": "Ada Example", "email": "ada@example.com", "location": "Durham, NC"}},
    "summary": "Engineer.",
    "roles": [
        {"kind": "job", "organization": "Acme", "title": "Lead SDET", "start_date": "2020-01",
         "end_date": "2023-05", "location": "Remote", "bullets": [{"text": "Built the test framework."}]},
        {"kind": "education", "organization": "Example State University", "title": "Bachelor's degree, Physics",
         "start_date": "2010-09", "end_date": "2014-06", "bullets": []},
        {"kind": "job", "organization": "ACME, INC.", "title": "Automation Lead", "start_date": "2020-01",
         "end_date": "2023-05", "bullets": [{"text": "Built the test framework."}, {"text": "Ran CI."}]},
        {"kind": "job", "organization": "Initech", "title": "AI Engineer", "start_date": "2023-06",
         "end_date": None, "bullets": [{"text": "Shipped an agent."}]},
        {"kind": "education", "organization": "Example Federal University", "title": "Bachelor of Physics",
         "bullets": []},
    ],
    "skills": [{"name": "Python"}, {"name": "python"}],
    "titles": [{"text": "AI Engineer"}],
    "certifications": [{"name": "CSM"}, {"name": "CSM."}],
}

CONTENT = {
    "headline": "AI Engineer",
    "summary": "Builds agents and the tests that keep them honest.",
    "skills": [{"title": "Languages", "items": ["Python"]}],
    "experience": [
        {"job": "initech-2023-06", "bullets": ["Shipped an agent that answers support tickets."]},
        {"job": "acme-2020-01", "bullets": ["Built the test framework the whole team used."]},
    ],
}


def test_the_same_job_saved_twice_is_one_job_printed_as_first_saved_with_all_bullets() -> None:
    career = career_from_export(EXPORT)
    assert [job.id for job in career.jobs] == ["initech-2023-06", "acme-2020-01"]
    acme = career.jobs[1]
    assert (acme.company, acme.title, acme.location) == ("Acme", "Lead SDET", "Remote")
    assert acme.material == ["Built the test framework.", "Ran CI."]


def test_the_same_school_and_certification_saved_twice_are_kept_once() -> None:
    career = career_from_export(EXPORT)
    assert len(career.education) == 1
    school = career.education[0]
    assert (school.school, school.degree, school.field_of_study, school.start_date) == (
        "Example State University", "Bachelor's degree", "Physics", "2010-09")
    assert career.certifications == ["CSM"]
    assert career.skills == ["Python"]


def test_employers_titles_and_dates_come_from_the_career_not_the_content() -> None:
    resume = build_resume(career_from_export(EXPORT), CONTENT)
    first = resume.professional_experience[0]
    assert (first.company_name, first.work_title, first.start_date, first.end_date) == (
        "Initech", "AI Engineer", "2023-06", None)
    assert resume.person.name == "Ada Example"
    assert resume.certifications.certifications == ["CSM"]
    text = "".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(resume))).pages)
    assert "INITECH" in text.upper() and "Shipped an agent" in text


def test_a_job_the_career_does_not_have_is_refused() -> None:
    content = {**CONTENT, "experience": CONTENT["experience"] + [{"job": "globex-2019-01", "bullets": ["x"]}]}
    with pytest.raises(ContentError, match="globex-2019-01"):
        build_resume(career_from_export(EXPORT), content)


def test_a_job_left_without_bullets_is_refused() -> None:
    content = {**CONTENT, "experience": CONTENT["experience"][:1]}
    with pytest.raises(ContentError, match="acme-2020-01"):
        build_resume(career_from_export(EXPORT), content)


def test_a_contract_problem_is_named_so_the_worker_can_fix_it() -> None:
    with pytest.raises(ContentError, match="summary"):
        build_resume(career_from_export(EXPORT), {**CONTENT, "headline": ""})


def test_a_resume_pdf_is_replaced_whole_or_not_at_all(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from job_hunt import resume

    monkeypatch.setattr(resume, "build_resume", lambda career, content: None)
    monkeypatch.setattr(resume, "render_pdf", lambda built: b"%PDF new")
    (tmp_path / "resume-content.json").write_text("{}")
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF old")

    def stopped(source: str, target: object) -> None:
        raise OSError(28, "No space left on device")

    # A build that stops never leaves a cut-off resume.pdf that a later run would take as built.
    with monkeypatch.context() as broken:
        broken.setattr(os, "replace", stopped)
        with pytest.raises(OSError):
            resume.write_pdf(None, tmp_path / "resume-content.json", pdf)
    assert pdf.read_bytes() == b"%PDF old"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["resume-content.json", "resume.pdf"]
    resume.write_pdf(None, tmp_path / "resume-content.json", pdf)
    assert pdf.read_bytes() == b"%PDF new"


def test_hunt_resume_builds_in_an_absolute_folder_from_another_working_folder(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The resume worker runs it from the empty run folder, with the job folder's absolute path.
    import json

    write_career(career_from_export(EXPORT), tmp_path / "career.json")
    monkeypatch.setattr(cli, "CAREER", tmp_path / "career.json")
    folder = tmp_path / "My Projects" / "jobs" / "4"
    folder.mkdir(parents=True)
    (folder / "resume-content.json").write_text(json.dumps(CONTENT))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli.main(["resume", str(folder)]) == 0
    assert (folder / "resume.pdf").read_bytes().startswith(b"%PDF")
    assert list(elsewhere.iterdir()) == []
