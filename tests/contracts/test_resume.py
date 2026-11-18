import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from resume_kit.contracts.resume import (
    MAX_BULLET_WORDS,
    MAX_HEADLINE_WORDS,
    Person,
    Resume,
    ResumeBody,
    assemble_resume,
    headline_is_too_long,
)

REPOSITORY_ROOT = Path(__file__).parents[2]


def _minimal_resume_data() -> dict[str, object]:
    return {
        "person": {
            "name": "John Doe",
        },
        "summary": {
            "text": "Software Engineer",
            "summary_description": "Builds reliable web applications.",
        },
        "technical_skills": {
            "skills": [
                {
                    "title": "Languages",
                    "bullets": ["Python"],
                }
            ]
        },
        "education": [_education()],
        "certifications": {},
    }


def _education(**overrides: object) -> dict[str, object]:
    education: dict[str, object] = {
        "school_name": "Example University",
        "degree": "Bachelor of Science",
        "bullets": ["Graduated with honors."],
    }
    education.update(overrides)
    return education


def _professional_experience(**overrides: object) -> dict[str, object]:
    experience: dict[str, object] = {
        "work_title": "Software Engineer",
        "company_name": "Example Inc.",
        "start_date": "2020-01",
        "end_date": None,
        "bullets": ["Built reliable software."],
    }
    experience.update(overrides)
    return experience


def test_resume_rejects_unknown_fields() -> None:
    data = _minimal_resume_data()
    data["unexpected"] = "nope"
    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_blank_skill_item() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {
        "skills": [
            {
                "title": "Languages",
                "bullets": [" "],
            }
        ]
    }
    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_blank_experience_bullet() -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [
        {
            "work_title": "Engineer",
            "company_name": "Example Inc.",
            "start_date": "2020-01",
            "end_date": None,
            "bullets": ["   "],
        }
    ]
    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_blank_nonblank_string_field() -> None:
    data = _minimal_resume_data()
    data["person"] = {"name": "   "}
    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_blank_optional_string_fields() -> None:
    for field_name in ("phone", "location"):
        data = _minimal_resume_data()
        data["person"] = {"name": "John Doe", field_name: "   "}
        with pytest.raises(ValidationError):
            Resume.model_validate(data)

    data = _minimal_resume_data()
    data["education"] = {
        "school_name": "Example University",
        "degree": "Bachelor of Science",
        "field_of_study": "   ",
        "bullets": ["Graduated with honors."],
    }
    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_allows_empty_certifications() -> None:
    resume = Resume.model_validate(_minimal_resume_data())
    assert resume.certifications.certifications == []


def test_resume_allows_optional_education_start_date() -> None:
    resume = Resume.model_validate(_minimal_resume_data())
    assert resume.education[0].start_date is None


# A section the candidate profile has nothing for is left out, not invented.


@pytest.mark.parametrize("without", [{"education": []}, {}])
def test_resume_may_have_no_education(without: dict[str, object]) -> None:
    data = _minimal_resume_data()
    del data["education"]
    data.update(without)

    assert Resume.model_validate(data).education == []


def test_resume_keeps_every_education_entry_in_order() -> None:
    data = _minimal_resume_data()
    data["education"] = [
        _education(degree="Master of Science"),
        _education(degree="Bachelor of Science"),
    ]

    degrees = [entry.degree for entry in Resume.model_validate(data).education]

    assert degrees == ["Master of Science", "Bachelor of Science"]


def test_resume_refuses_education_that_is_not_a_list() -> None:
    data = _minimal_resume_data()
    data["education"] = _education()  # the shape before education became a list

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_an_education_entry_may_have_no_bullets() -> None:
    data = _minimal_resume_data()
    data["education"] = [_education(bullets=[])]
    without_the_field = _education()
    del without_the_field["bullets"]

    assert Resume.model_validate(data).education[0].bullets == []
    data["education"] = [without_the_field]
    assert Resume.model_validate(data).education[0].bullets == []


def test_resume_may_have_no_technical_skills() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {"skills": []}

    assert Resume.model_validate(data).technical_skills.skills == []


def test_a_skill_row_still_needs_at_least_one_skill() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {"skills": [{"title": "Languages", "bullets": []}]}

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


@pytest.mark.parametrize("end_date", ["2020-01", "2024-06"])
def test_resume_accepts_experience_ending_on_or_after_start(end_date: str) -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [_professional_experience(end_date=end_date)]

    resume = Resume.model_validate(data)

    assert resume.professional_experience[0].end_date == end_date


def test_resume_rejects_experience_ending_before_start() -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [
        _professional_experience(start_date="2024-05", end_date="2019-01")
    ]

    with pytest.raises(ValidationError, match="End date cannot be before start date"):
        Resume.model_validate(data)


@pytest.mark.parametrize(
    "start_date,end_date", [("2019", "2019-01"), ("2019-12", "2019"), ("2019-06", "2020")]
)
def test_resume_takes_a_year_alone_as_any_month_of_it(start_date: str, end_date: str) -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [
        _professional_experience(start_date=start_date, end_date=end_date)
    ]
    data["education"] = [_education(start_date=start_date, end_date=end_date)]

    resume = Resume.model_validate(data)

    assert resume.professional_experience[0].end_date == end_date
    assert resume.education[0].start_date == start_date


def test_resume_rejects_a_year_alone_that_ends_before_its_start() -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [
        _professional_experience(start_date="2020", end_date="2019-12")
    ]

    with pytest.raises(ValidationError, match="End date cannot be before start date"):
        Resume.model_validate(data)


def test_resume_rejects_education_ending_before_start() -> None:
    data = _minimal_resume_data()
    data["education"] = [_education(start_date="2024-05", end_date="2019-01")]

    with pytest.raises(ValidationError, match="End date cannot be before start date"):
        Resume.model_validate(data)


def test_resume_accepts_canonical_example() -> None:
    example_path = REPOSITORY_ROOT / "examples" / "resume.example.json"
    data = json.loads(example_path.read_text(encoding="utf-8"))

    resume = Resume.model_validate(data)

    assert resume.person.name == "John Doe"
    assert len(resume.professional_experience) == 2


def test_resume_accepts_ten_short_items_in_one_skill_row() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {"skills": [{"title": "Tools", "bullets": ["AI"] * 10}]}

    resume = Resume.model_validate(data)

    assert len(resume.technical_skills.skills[0].bullets) == 10


def test_resume_rejects_more_than_ten_items_in_one_skill_row() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {"skills": [{"title": "Tools", "bullets": ["AI"] * 11}]}

    with pytest.raises(ValidationError, match="10 items"):
        Resume.model_validate(data)


def test_resume_rejects_more_than_seventeen_words_in_one_skill_row() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {"skills": [{"title": "Tools", "bullets": [" ".join(["x"] * 17)]}]}

    with pytest.raises(ValidationError, match="17 words"):
        Resume.model_validate(data)


def test_resume_accepts_exactly_one_hundred_characters_in_one_skill_row() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {"skills": [{"title": "T", "bullets": ["x" * 97]}]}

    resume = Resume.model_validate(data)

    assert resume.technical_skills.skills[0].title == "T"


def test_resume_rejects_more_than_one_hundred_characters_in_one_skill_row() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {"skills": [{"title": "T", "bullets": ["x" * 98]}]}

    with pytest.raises(ValidationError, match="100 characters"):
        Resume.model_validate(data)


def test_resume_rejects_unknown_nested_fields() -> None:
    data = _minimal_resume_data()
    data["person"] = {"name": "John Doe", "unexpected": "nope"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Resume.model_validate(data)


@pytest.mark.parametrize("date", ["2020-01", "2020-12", "2020"])
def test_resume_accepts_valid_date_boundaries(date: str) -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [_professional_experience(start_date=date)]

    resume = Resume.model_validate(data)

    assert resume.professional_experience[0].start_date == date


@pytest.mark.parametrize(
    "date",
    ["2020-00", "2020-13", "20-01", "2020-1", "January 2020", "2020-01-15", ""],
)
def test_resume_rejects_invalid_dates(date: str) -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [_professional_experience(start_date=date)]

    with pytest.raises(ValidationError, match="Expected YYYY or YYYY-MM"):
        Resume.model_validate(data)


# The same rule as a saved date: ASCII digits and a plausible year.


@pytest.mark.parametrize(
    "date", ["\uff12\uff10\uff12\uff10-01", "\u0662\u0660\u0662\u0660-01", "2020-\u0660\u0661"]
)
def test_resume_dates_use_only_ascii_digits(date: str) -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [_professional_experience(start_date=date)]

    with pytest.raises(ValidationError, match="Expected YYYY or YYYY-MM"):
        Resume.model_validate(data)


@pytest.mark.parametrize("date", ["1900-01", "2100-12"])
def test_resume_accepts_the_first_and_last_plausible_year(date: str) -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [_professional_experience(start_date=date, end_date=None)]

    assert Resume.model_validate(data).professional_experience[0].start_date == date


@pytest.mark.parametrize("date", ["0000-01", "1899-12", "2101-01", "9999-12"])
def test_resume_refuses_an_implausible_year(date: str) -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [_professional_experience(start_date=date)]

    with pytest.raises(ValidationError, match="between 1900 and 2100"):
        Resume.model_validate(data)


def test_resume_requires_professional_experience_end_date() -> None:
    data = _minimal_resume_data()
    experience = _professional_experience()
    del experience["end_date"]
    data["professional_experience"] = [experience]

    with pytest.raises(ValidationError, match="end_date"):
        Resume.model_validate(data)


def test_resume_accepts_exactly_twenty_words_in_experience_bullet() -> None:
    data = _minimal_resume_data()
    bullet = " ".join(["word"] * 20)
    data["professional_experience"] = [_professional_experience(bullets=[bullet])]

    resume = Resume.model_validate(data)

    assert resume.professional_experience[0].bullets == [bullet]


def test_resume_rejects_more_than_twenty_words_in_experience_bullet() -> None:
    data = _minimal_resume_data()
    bullet = " ".join(["word"] * 21)
    data["professional_experience"] = [_professional_experience(bullets=[bullet])]

    with pytest.raises(ValidationError, match="20 words"):
        Resume.model_validate(data)


def test_resume_strips_skill_items_and_experience_bullets() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {"skills": [{"title": "Languages", "bullets": ["  Python  "]}]}
    data["professional_experience"] = [
        _professional_experience(bullets=["  Built reliable software.  "])
    ]

    resume = Resume.model_validate(data)

    assert resume.technical_skills.skills[0].bullets == ["Python"]
    assert resume.professional_experience[0].bullets == ["Built reliable software."]


def test_resume_rejects_invalid_email() -> None:
    data = _minimal_resume_data()
    data["person"] = {"name": "John Doe", "email": "not-an-email"}

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_invalid_link_url() -> None:
    data = _minimal_resume_data()
    data["person"] = {
        "name": "John Doe",
        "links": [{"label": "Portfolio", "url": "not a url"}],
    }

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_empty_professional_experience_bullets() -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [_professional_experience(bullets=[])]

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_uses_independent_default_lists() -> None:
    first = Resume.model_validate(_minimal_resume_data())
    second = Resume.model_validate(_minimal_resume_data())

    assert first.person.links is not second.person.links
    assert first.professional_experience is not second.professional_experience
    assert first.certifications.certifications is not second.certifications.certifications


def test_resume_serializes_with_canonical_field_names() -> None:
    serialized = Resume.model_validate(_minimal_resume_data()).model_dump(mode="json")

    assert "person" in serialized
    assert "technical_skills" in serialized
    assert "professional_experience" in serialized
    assert "basic" not in serialized
    assert "technicalSkills" not in serialized
    assert "professionalExperience" not in serialized


def test_resume_accepts_exactly_seventeen_words_in_one_skill_row() -> None:
    data = _minimal_resume_data()
    data["technical_skills"] = {"skills": [{"title": "Tools", "bullets": [" ".join(["x"] * 16)]}]}

    resume = Resume.model_validate(data)

    assert len(resume.technical_skills.skills[0].bullets[0].split()) == 16


def test_resume_accepts_null_for_optional_fields() -> None:
    data = _minimal_resume_data()
    data["person"] = {
        "name": "John Doe",
        "email": None,
        "phone": None,
        "location": None,
    }
    data["education"] = [_education(field_of_study=None, start_date=None, end_date=None)]

    resume = Resume.model_validate(data)

    assert resume.person.email is None
    assert resume.person.phone is None
    assert resume.person.location is None
    assert resume.education[0].field_of_study is None
    assert resume.education[0].start_date is None
    assert resume.education[0].end_date is None


def test_resume_rejects_non_web_link_scheme() -> None:
    data = _minimal_resume_data()
    data["person"] = {
        "name": "John Doe",
        "links": [{"label": "Files", "url": "ftp://example.com/resume.pdf"}],
    }

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_bytes_for_string_field() -> None:
    data = _minimal_resume_data()
    data["person"] = {"name": b"John Doe"}

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_bytes_for_email() -> None:
    data = _minimal_resume_data()
    data["person"] = {"name": "John Doe", "email": b"john@example.com"}

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_bytes_for_date() -> None:
    data = _minimal_resume_data()
    data["professional_experience"] = [_professional_experience(start_date=b"2020-01")]

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


def test_resume_rejects_bytes_for_url() -> None:
    data = _minimal_resume_data()
    data["person"] = {
        "name": "John Doe",
        "links": [{"label": "Portfolio", "url": b"https://example.com"}],
    }

    with pytest.raises(ValidationError):
        Resume.model_validate(data)


# The body is what is written for one job. The person is who it is about, and is copied.


def _body_data() -> dict[str, object]:
    data = _minimal_resume_data()
    del data["person"]
    return data


def test_a_resume_body_is_a_resume_without_the_person() -> None:
    body = ResumeBody.model_validate(_body_data())

    assert body.summary.text == "Software Engineer"
    # The job title names the resume and is not printed, so a resume has no place for it.
    assert set(Resume.model_fields) == {"person", "language", *ResumeBody.model_fields} - {
        "job_title"
    }


def test_the_body_schema_gives_a_model_no_place_to_choose_a_language() -> None:
    # Code finds the language in the profile; the writer is told it and cannot set it.
    assert "language" not in ResumeBody.model_json_schema()["properties"]


def test_a_resume_saved_before_languages_existed_is_read_as_english() -> None:
    assert Resume.model_validate(_minimal_resume_data()).language == "en"


def test_a_resume_keeps_the_language_it_was_saved_with() -> None:
    data = {**_minimal_resume_data(), "language": "de"}
    assert Resume.model_validate(data).language == "de"
    assert (
        Resume.model_validate_json(Resume.model_validate(data).model_dump_json()).language == "de"
    )


def test_a_resume_refuses_a_language_it_cannot_print() -> None:
    with pytest.raises(ValidationError):
        Resume.model_validate({**_minimal_resume_data(), "language": "ru"})


def test_a_resume_body_refuses_a_person() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted") as error:
        ResumeBody.model_validate(_minimal_resume_data())

    assert error.value.errors()[0]["loc"] == ("person",)


def test_the_body_schema_gives_a_model_no_place_to_write_contact_details() -> None:
    schema = json.dumps(ResumeBody.model_json_schema())

    for word in ("person", "email", "phone", "links"):
        assert word not in schema


def test_assembling_puts_the_given_person_on_the_body_unchanged() -> None:
    person = Person.model_validate(
        {
            "name": "Alex Rivera",
            "email": "alex.rivera@example.com",
            "phone": "555-0100",
            "location": "Austin, TX",
            "links": [{"label": "GitHub", "url": "https://github.com/alexrivera"}],
        }
    )
    body = ResumeBody.model_validate(_body_data())

    resume = assemble_resume(person, body, language="fr")

    assert resume.person == person
    assert resume.language == "fr"
    assert resume.model_dump(exclude={"person", "language"}) == body.model_dump(
        exclude={"job_title"}
    )


def test_a_saved_resume_still_starts_with_the_person() -> None:
    # The saved JSON is read by people; the person stays the first thing in it.
    assert next(iter(Resume.model_validate(_minimal_resume_data()).model_dump())) == "person"


def test_a_resume_is_no_longer_a_resume_body() -> None:
    # A person may clear the summary of their resume; a model may not leave it out. One class
    # cannot hold both rules, so the two only share their other sections.
    assert not isinstance(Resume.model_validate(_minimal_resume_data()), ResumeBody)


def _with_summary(summary: dict[str, object]) -> dict[str, object]:
    data = _minimal_resume_data()
    data["summary"] = summary
    return data


@pytest.mark.parametrize(
    ("summary", "cleared"),
    [
        ({"summary_description": "Builds reliable web applications."}, "text"),
        ({"text": "Software Engineer"}, "summary_description"),
    ],
)
def test_a_resume_may_have_either_part_of_the_summary_cleared(
    summary: dict[str, object], cleared: str
) -> None:
    resume = Resume.model_validate(_with_summary(summary))

    assert getattr(resume.summary, cleared) is None


def test_a_resume_may_have_no_summary_at_all() -> None:
    resume = Resume.model_validate(_with_summary({}))

    assert resume.summary.text is None
    assert resume.summary.summary_description is None


def test_a_cleared_summary_is_not_a_blank_one() -> None:
    blank: dict[str, object] = {
        "text": "   ",
        "summary_description": "Builds reliable web applications.",
    }

    with pytest.raises(ValidationError):
        Resume.model_validate(_with_summary(blank))


@pytest.mark.parametrize(
    "summary",
    [{"summary_description": "Builds reliable web applications."}, {"text": "Software Engineer"}],
)
def test_what_a_model_writes_still_needs_both_parts_of_the_summary(
    summary: dict[str, object],
) -> None:
    data = _with_summary(summary)
    del data["person"]

    with pytest.raises(ValidationError):
        ResumeBody.model_validate(data)


def test_the_summary_stays_the_first_thing_a_model_is_asked_to_write() -> None:
    assert next(iter(ResumeBody.model_fields)) == "summary"
    assert list(Resume.model_fields)[:2] == ["person", "summary"]


def test_the_job_title_is_asked_for_last_in_its_agreed_words() -> None:
    # The resume is written before it is named.
    assert list(ResumeBody.model_fields)[-1] == "job_title"
    assert ResumeBody.model_json_schema()["properties"]["job_title"] == {
        "description": "The job title this posting hires for, as the posting writes it. "
        "Empty when the posting names no title.",
        "title": "Job Title",
        "type": "string",
    }


def test_the_job_title_is_read_as_written_and_is_empty_when_none_is_given() -> None:
    data = _body_data()
    assert ResumeBody.model_validate(data).job_title == ""

    # It is never printed, so a sign the PDF cannot print is no reason to refuse the answer.
    data["job_title"] = "Applied AI Engineer → Platform"
    assert ResumeBody.model_validate(data).job_title == "Applied AI Engineer → Platform"


# `summary.text` is printed as the headline under the name; `summary_description` is the
# paragraph. A real model once filled them the other way round, and the PDF came out with the
# whole paragraph as its headline. A headline has a size, so the swap is now a schema failure.
# The size is a rule for what a model writes (`ResumeBody`). A hand edit is held to it where it
# is saved (`runs/edited_resume.py`), not in `Resume`, so a resume stored with a longer headline
# before that rule can still be read.


def test_a_headline_of_exactly_fifteen_words_is_accepted() -> None:
    data = _minimal_resume_data()
    data["summary"] = {
        "text": " ".join(["Engineer"] * MAX_HEADLINE_WORDS),
        "summary_description": "Builds reliable web applications.",
    }

    assert MAX_HEADLINE_WORDS == 15
    Resume.model_validate(data)


@pytest.mark.parametrize(
    "headline",
    [
        "Senior Backend Engineer | Python | AWS | Kubernetes | PostgreSQL | Kafka | Go | Rust",
        "Senior Backend Engineer · Python · AWS · Kubernetes · PostgreSQL · Kafka · Go · Rust",
        "Senior Backend Engineer - Python, AWS, Kubernetes, PostgreSQL, Kafka, Go & Rust",
    ],
)
def test_separators_in_a_headline_are_not_counted_as_words(headline: str) -> None:
    # Ten words. The prompt says "15 words or fewer", so "|" and "·" must not use up the limit.
    data = _minimal_resume_data()
    data["summary"] = {"text": headline, "summary_description": "Builds reliable services."}

    Resume.model_validate(data)


def test_sixteen_real_words_with_separators_are_still_too_many() -> None:
    data = _minimal_resume_data()
    data["summary"] = {
        "text": " | ".join(["Engineer"] * (MAX_HEADLINE_WORDS + 1)),
        "summary_description": "Builds reliable services.",
    }

    del data["person"]

    with pytest.raises(ValidationError, match="no more than 15 words"):
        ResumeBody.model_validate(data)


def test_a_stored_resume_with_a_longer_headline_can_still_be_read() -> None:
    data = _minimal_resume_data()
    long_headline = " ".join(["Engineer"] * (MAX_HEADLINE_WORDS + 1))
    data["summary"] = {"text": long_headline}

    assert Resume.model_validate(data).summary.text == long_headline


@pytest.mark.parametrize(
    ("headline", "too_long"),
    [
        (" ".join(["Engineer"] * MAX_HEADLINE_WORDS), False),
        (" ".join(["Engineer"] * (MAX_HEADLINE_WORDS + 1)), True),
        (" | ".join(["Engineer"] * MAX_HEADLINE_WORDS), False),
    ],
)
def test_a_headline_is_too_long_past_fifteen_words_not_counting_separators(
    headline: str, too_long: bool
) -> None:
    assert headline_is_too_long(headline) is too_long


def test_a_resume_edited_by_hand_may_still_have_no_headline() -> None:
    data = _minimal_resume_data()
    data["summary"] = {"summary_description": "Builds reliable services."}

    assert Resume.model_validate(data).summary.text is None


def test_a_paragraph_where_the_headline_belongs_is_rejected_and_told_where_it_goes() -> None:
    data = _minimal_resume_data()
    data["summary"] = {
        "text": "Software engineer with 8+ years building customer-facing web applications, typed "
        "Python services, React interfaces, and reliable APIs for teams that ship every week.",
        "summary_description": "Full-Stack Software Engineer | Python, React, TypeScript",
    }

    with pytest.raises(ValidationError) as caught:
        ResumeBody.model_validate({key: data[key] for key in data if key != "person"})

    message = str(caught.value)
    assert "no more than 15 words" in message
    assert "summary_description" in message  # the repair request tells the model where it goes


def _unprintable_errors(data: dict[str, object]) -> list[tuple[tuple[int | str, ...], str]]:
    with pytest.raises(ValidationError) as caught:
        ResumeBody.model_validate(data)
    return [(error["loc"], error["msg"]) for error in caught.value.errors()]


def test_what_a_model_writes_may_hold_only_characters_the_pdf_can_print() -> None:
    data = _body_data()
    data["professional_experience"] = [
        _professional_experience(bullets=["Built it.", "Cut build time 40 min → 6 min ✓, → done."])
    ]

    assert _unprintable_errors(data) == [
        (
            ("professional_experience", 0, "bullets", 1),
            "Value error, The resume PDF cannot print these characters: → ✓",
        )
    ]


def test_one_unprintable_character_is_named_as_one() -> None:
    data = _body_data()
    data["summary"] = {"text": "Engineer → Lead", "summary_description": "Builds services."}

    assert _unprintable_errors(data) == [
        (("summary", "text"), "Value error, The resume PDF cannot print this character: →")
    ]


def test_every_field_with_an_unprintable_character_is_named_with_its_own_characters() -> None:
    data = _body_data()
    data["summary"] = {"text": "Software Engineer", "summary_description": "Uptime ≥ 99.9%."}
    data["technical_skills"] = {"skills": [{"title": "Languages ★", "bullets": ["Python", "Go ✓"]}]}
    data["professional_experience"] = [
        _professional_experience(work_title="Engineer → Lead", company_name="李明 Inc.")
    ]
    data["education"] = [_education(field_of_study="Math ∞")]
    data["certifications"] = {"certifications": ["AWS ✓"]}

    assert _unprintable_errors(data) == [
        (
            ("summary", "summary_description"),
            "Value error, The resume PDF cannot print this character: ≥",
        ),
        (
            ("technical_skills", "skills", 0, "title"),
            "Value error, The resume PDF cannot print this character: ★",
        ),
        (
            ("technical_skills", "skills", 0, "bullets", 1),
            "Value error, The resume PDF cannot print this character: ✓",
        ),
        (
            ("professional_experience", 0, "work_title"),
            "Value error, The resume PDF cannot print this character: →",
        ),
        (
            ("professional_experience", 0, "company_name"),
            "Value error, The resume PDF cannot print these characters: 李 明",
        ),
        (
            ("education", 0, "field_of_study"),
            "Value error, The resume PDF cannot print this character: ∞",
        ),
        (
            ("certifications", "certifications", 0),
            "Value error, The resume PDF cannot print this character: ✓",
        ),
    ]


def test_a_stored_resume_with_an_unprintable_character_can_still_be_read() -> None:
    """The rule is on what a model returns. A resume saved before it must still open."""
    data = _minimal_resume_data()
    data["summary"] = {"text": "Engineer → Lead", "summary_description": "Uptime ≥ 99.9%."}

    assert Resume.model_validate(data).summary.text == "Engineer → Lead"


def test_the_rule_adds_nothing_to_the_schema_a_model_is_sent() -> None:
    schema = json.dumps(ResumeBody.model_json_schema(), ensure_ascii=False)

    assert "print" not in schema
    assert "character" not in schema


def test_an_answer_that_also_breaks_another_rule_is_told_about_that_rule_first() -> None:
    """The characters are checked once the answer is otherwise valid, within the three calls."""
    data = _body_data()
    too_long = " ".join(["word"] * (MAX_BULLET_WORDS + 1)) + " →"
    data["professional_experience"] = [_professional_experience(bullets=[too_long])]

    assert _unprintable_errors(data) == [
        (
            ("professional_experience", 0, "bullets", 0),
            "Value error, Experience bullets must contain no more than 20 words "
            "(approximately two lines).",
        )
    ]


def test_the_same_character_in_two_jobs_is_reported_for_each_bullet() -> None:
    data = _body_data()
    data["professional_experience"] = [
        _professional_experience(bullets=["Fine.", "Up 10 → 20."]),
        _professional_experience(company_name="Other Inc.", bullets=["Down 20 → 10.", "Fine."]),
    ]
    one = "Value error, The resume PDF cannot print this character: →"

    assert _unprintable_errors(data) == [
        (("professional_experience", 0, "bullets", 1), one),
        (("professional_experience", 1, "bullets", 0), one),
    ]


def test_the_invisible_mark_a_phone_adds_after_a_sign_is_not_a_reason_to_refuse() -> None:
    data = _body_data()
    data["summary"] = {"text": "Engineer", "summary_description": "Built Acme©️ tools."}

    assert ResumeBody.model_validate(data).summary.summary_description == "Built Acme©️ tools."


def test_a_sign_the_font_lacks_is_named_without_the_invisible_mark_after_it() -> None:
    data = _body_data()
    data["summary"] = {"text": "Engineer", "summary_description": "Shipped ✔️ on time."}

    assert _unprintable_errors(data) == [
        (
            ("summary", "summary_description"),
            "Value error, The resume PDF cannot print this character: ✔",
        )
    ]


def test_the_rule_reads_an_answer_given_as_json_text_the_same_way() -> None:
    data = _body_data()
    data["summary"] = {"text": "Engineer → Lead", "summary_description": "Builds services."}

    with pytest.raises(ValidationError) as caught:
        ResumeBody.model_validate_json(json.dumps(data, ensure_ascii=False))

    assert [(error["loc"], error["msg"]) for error in caught.value.errors()] == [
        (("summary", "text"), "Value error, The resume PDF cannot print this character: →")
    ]


def test_the_refusal_names_the_characters_and_never_repeats_the_text_around_them() -> None:
    data = _body_data()
    data["summary"] = {"text": "Engineer", "summary_description": "Private words → more."}

    with pytest.raises(ValidationError) as caught:
        ResumeBody.model_validate(data)

    assert caught.value.errors()[0]["input"] is None
    assert "Private words" not in str(caught.value)
    assert "Private words" not in caught.value.json()


def test_a_body_built_from_its_parts_is_held_to_the_rule_too() -> None:
    """The stand-in writer builds its answer this way, from the profile's own bullets."""
    valid = ResumeBody.model_validate(_body_data())

    with pytest.raises(ValidationError, match="cannot print this character: →"):
        ResumeBody(
            summary=valid.summary.model_copy(update={"text": "Engineer → Lead"}),
            technical_skills=valid.technical_skills,
            education=valid.education,
            certifications=valid.certifications,
        )


def test_checking_an_answer_leaves_it_as_it_was() -> None:
    data = _body_data()
    data["professional_experience"] = [_professional_experience()]

    body = ResumeBody.model_validate(data)

    assert body.model_dump(mode="json", exclude_defaults=True) == ResumeBody.model_validate(
        body.model_dump(mode="json")
    ).model_dump(mode="json", exclude_defaults=True)
    assert body.professional_experience[0].start_date == "2020-01"


def test_an_invisible_mark_left_alone_is_named_to_a_model_by_its_number() -> None:
    data = _body_data()
    data["summary"] = {"text": "Engineer", "summary_description": "Built \ufe0f tools."}

    assert _unprintable_errors(data) == [
        (
            ("summary", "summary_description"),
            "Value error, The resume PDF cannot print this character: U+FE0F (invisible)",
        )
    ]
