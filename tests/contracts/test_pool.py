import pytest
from pydantic import ValidationError

from resume_kit.contracts.pool import (
    PERSONAL_PROJECT,
    BulletInput,
    CertificationEntry,
    CertificationInput,
    HeadlineTitleInput,
    LooseRole,
    Pool,
    Profile,
    Role,
    RoleInput,
    SkillInput,
    SummaryInput,
)


def _role(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": "job",
        "organization": "Acme",
        "title": "Backend Engineer",
        "start_date": "2021-03",
        "end_date": "2023-06",
    }
    data.update(overrides)
    return data


def test_profile_requires_a_name_and_has_no_summary() -> None:
    profile = Profile.model_validate({"person": {"name": "Alex Morgan"}})

    assert profile.person.name == "Alex Morgan"
    with pytest.raises(ValidationError):
        Profile.model_validate({"person": {"name": "  "}})
    # The summary is written per job, so it is output, not stored identity.
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Profile.model_validate({"person": {"name": "Alex Morgan"}, "summary": "Builder."})


# A role that may be saved: a job or project says when it started.


@pytest.mark.parametrize("date", ["2021-03", "1999-12", "2021"])
def test_a_saved_role_accepts_a_year_with_or_without_its_month(date: str) -> None:
    assert RoleInput.model_validate(_role(start_date=date, end_date=None)).start_date == date


def test_a_saved_role_keeps_a_year_alone_as_it_was_written() -> None:
    role = RoleInput.model_validate(_role(start_date="2021", end_date="2024"))

    assert (role.start_date, role.end_date) == ("2021", "2024")


@pytest.mark.parametrize("date", ["21", "2021-13", "2021-3", "March 2021", "2021-00", ""])
def test_role_rejects_other_date_formats(date: str) -> None:
    with pytest.raises(ValidationError, match="Expected YYYY or YYYY-MM"):
        RoleInput.model_validate(_role(start_date=date))


def test_role_rejects_end_before_start() -> None:
    with pytest.raises(ValidationError, match="End date cannot be before start date"):
        RoleInput.model_validate(_role(start_date="2022-05", end_date="2022-04"))


def test_an_empty_end_date_means_the_job_is_current() -> None:
    assert RoleInput.model_validate(_role(end_date=None)).end_date is None


@pytest.mark.parametrize("kind", ["job", "project"])
def test_a_job_or_project_needs_a_start_date(kind: str) -> None:
    with pytest.raises(ValidationError, match="A start date is required for a job or project"):
        RoleInput.model_validate(_role(kind=kind, start_date=None, end_date=None))


def test_education_may_have_no_start_date() -> None:
    RoleInput.model_validate(_role(kind="education", start_date=None, end_date=None))
    # "Graduated June 2019" with no start is how many people write it.
    graduated = RoleInput.model_validate(
        _role(kind="education", start_date=None, end_date="2019-06")
    )
    assert graduated.end_date == "2019-06"


# A role as a file gave it, or as a row saved before a start date was required holds it.


@pytest.mark.parametrize("date", ["2021", "2021-03", "1999-12"])
def test_a_loose_role_accepts_a_year_with_or_without_a_month(date: str) -> None:
    assert LooseRole.model_validate(_role(start_date=date, end_date=None)).start_date == date


def test_year_only_dates_do_not_create_false_ordering_errors() -> None:
    # A job that started in June 2021 and ended "2021" may have ended in December 2021.
    LooseRole.model_validate(_role(start_date="2021-06", end_date="2021"))
    # A job that started "2021" and ended in January 2021 may have started in January.
    LooseRole.model_validate(_role(start_date="2021", end_date="2021-01"))
    with pytest.raises(ValidationError, match="End date cannot be before start date"):
        LooseRole.model_validate(_role(start_date="2022", end_date="2021-12"))


def test_a_loose_role_may_have_only_one_of_its_dates() -> None:
    # A file may give one readable date and one that could not be read.
    assert LooseRole.model_validate(_role(start_date=None, end_date="2023")).end_date == "2023"
    assert LooseRole.model_validate(_role(start_date=None, end_date=None)).start_date is None


def test_a_role_read_back_from_storage_may_still_hold_a_bare_year() -> None:
    stored = Role.model_validate({**_role(start_date="2019", end_date=None), "id": "r1"})

    assert stored.start_date == "2019"


def test_a_saved_role_is_also_a_loose_role() -> None:
    assert isinstance(RoleInput.model_validate(_role()), LooseRole)


def test_role_rejects_unknown_kind_and_blank_text() -> None:
    with pytest.raises(ValidationError):
        RoleInput.model_validate(_role(kind="hobby"))
    with pytest.raises(ValidationError):
        RoleInput.model_validate(_role(organization=" "))


# A project of one's own has no organization: an empty one is saved as "Personal project", so
# what a resume is written from always names one, as it does for a job.


# "​" and "­" show nothing, and the server drops them from the ends of every text: a box
# that holds only them, pasted or read from a file, is as empty as one that holds nothing.
@pytest.mark.parametrize("organization", ["", "   ", None, "​", " ­​ "])
def test_a_project_without_an_organization_is_a_personal_project(
    organization: str | None,
) -> None:
    saved = RoleInput.model_validate(_role(kind="project", organization=organization))

    assert saved.organization == PERSONAL_PROJECT == "Personal project"


def test_a_project_may_leave_out_the_organization_altogether() -> None:
    data = _role(kind="project")
    del data["organization"]

    assert RoleInput.model_validate(data).organization == PERSONAL_PROJECT


@pytest.mark.parametrize("model", [LooseRole, Role])
def test_a_project_read_or_edited_without_an_organization_is_a_personal_project(
    model: type[LooseRole],
) -> None:
    data = _role(kind="project", organization="")
    role = model.model_validate({**data, "id": "r1"} if model is Role else data)

    assert role.organization == PERSONAL_PROJECT


def test_a_project_keeps_the_organization_it_was_given() -> None:
    role = RoleInput.model_validate(_role(kind="project", organization="  City Library "))

    assert role.organization == "City Library"


@pytest.mark.parametrize("kind", ["job", "education"])
@pytest.mark.parametrize("organization", ["", "   ", None])
def test_a_job_or_education_still_needs_its_organization(
    kind: str, organization: str | None
) -> None:
    with pytest.raises(ValidationError) as refused:
        RoleInput.model_validate(_role(kind=kind, organization=organization))

    assert [error["loc"] for error in refused.value.errors()] == [("organization",)]


def test_bullet_defaults_to_manual_and_limits_length() -> None:
    bullet = BulletInput.model_validate({"text": " Cut p95 latency by 40%. "})

    assert bullet.text == "Cut p95 latency by 40%."
    assert bullet.source == "manual"
    assert bullet.skills == []
    with pytest.raises(ValidationError):
        BulletInput.model_validate({"text": "x" * 501})
    with pytest.raises(ValidationError):
        BulletInput.model_validate({"text": "ok", "skills": [" "]})


def test_bullet_allows_more_than_twenty_words() -> None:
    # The 20-word limit is a generation-time rule, not a storage rule.
    BulletInput.model_validate({"text": " ".join(["word"] * 40)})


def test_skill_and_title_trim_and_reject_blank() -> None:
    assert SkillInput.model_validate({"name": " Python "}).name == "Python"
    assert (
        HeadlineTitleInput.model_validate({"text": " Senior Engineer "}).text == "Senior Engineer"
    )
    with pytest.raises(ValidationError):
        SkillInput.model_validate({"name": ""})
    with pytest.raises(ValidationError):
        HeadlineTitleInput.model_validate({"text": " "})


def test_a_pool_may_carry_one_optional_summary() -> None:
    empty = Pool(roles=[], skills=[], titles=[], certifications=[])
    assert empty.summary is None

    with_summary = Pool.model_validate(
        {
            "roles": [],
            "skills": [],
            "titles": [],
            "certifications": [],
            "summary": "  Backend engineer.  ",
        }
    )
    assert with_summary.summary == "Backend engineer."  # trimmed like every other text


@pytest.mark.parametrize("text", ["", "   ", "\u200b", "\n\t"])
def test_a_blank_summary_is_refused(text: str) -> None:
    with pytest.raises(ValidationError):
        SummaryInput.model_validate({"text": text})


def test_the_summary_has_a_length_limit_at_the_boundary() -> None:
    assert SummaryInput.model_validate({"text": "s" * 1000}).text == "s" * 1000
    with pytest.raises(ValidationError):
        SummaryInput.model_validate({"text": "s" * 1001})


def test_a_certification_needs_only_its_name() -> None:
    certification = CertificationInput.model_validate({"name": " AWS Solutions Architect "})

    assert certification.name == "AWS Solutions Architect"
    assert certification.issuer is None
    assert certification.date is None


def test_a_certification_may_say_who_issued_it_and_when() -> None:
    certification = CertificationInput.model_validate(
        {"name": "AWS Solutions Architect", "issuer": " Amazon Web Services ", "date": "2023"}
    )

    assert certification.issuer == "Amazon Web Services"
    assert certification.date == "2023"


@pytest.mark.parametrize(
    "fields",
    [
        {"name": ""},
        {"name": "   "},
        {},
        {"name": "CKA", "issuer": " "},
        {"name": "n" * 201},
        {"name": "CKA", "issuer": "i" * 201},
    ],
)
def test_a_certification_refuses_blank_or_overlong_text(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CertificationInput.model_validate(fields)


@pytest.mark.parametrize("date", ["2023", "2023-03", "1900", "2100-12"])
def test_a_certification_date_is_a_year_with_or_without_its_month(date: str) -> None:
    assert CertificationInput.model_validate({"name": "CKA", "date": date}).date == date


@pytest.mark.parametrize(
    "date",
    ["1899", "2101", "23", "2023-13", "2023-00", "2023-3", "03-2023", "March 2023", "", 2023, True],
)
def test_a_certification_date_in_any_other_form_is_refused(date: object) -> None:
    with pytest.raises(ValidationError):
        CertificationInput.model_validate({"name": "CKA", "date": date})


def test_a_certification_may_keep_its_credential_id_and_verification_link() -> None:
    certification = CertificationInput.model_validate(
        {
            "name": "Workshop Safety",
            "credential_id": " HL-7312 ",
            "verification_url": "https://example.com/verify/HL-7312",
        }
    )

    assert certification.credential_id == "HL-7312"
    assert str(certification.verification_url) == "https://example.com/verify/HL-7312"
    assert CertificationInput.model_validate({"name": "CKA"}).credential_id is None
    assert CertificationInput.model_validate({"name": "CKA"}).verification_url is None


def test_a_verification_link_written_without_its_scheme_gets_https() -> None:
    certification = CertificationInput.model_validate(
        {"name": "CKA", "verification_url": "verify.example.org/HL-7312"}
    )

    assert str(certification.verification_url) == "https://verify.example.org/HL-7312"


@pytest.mark.parametrize(
    "fields",
    [
        {"credential_id": " "},
        {"credential_id": "c" * 201},
        {"verification_url": "HL-7312"},
        {"verification_url": "mailto:someone@example.com"},
        {"verification_url": " "},
    ],
)
def test_a_credential_id_or_link_that_cannot_be_kept_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CertificationInput.model_validate({"name": "CKA", **fields})


def test_a_certification_has_no_other_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CertificationInput.model_validate({"name": "CKA", "year": 2023})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CertificationInput.model_validate({"name": "CKA", "expires": "2026"})


@pytest.mark.parametrize(
    ("date", "expiry_date"),
    [("2025", "2024"), ("2024-03", "2024-02"), ("2024-03", "2023"), ("2025", "2024-12")],
)
def test_a_certification_typed_by_a_person_cannot_expire_before_its_date(
    date: str, expiry_date: str
) -> None:
    with pytest.raises(ValidationError, match="expire before its date"):
        CertificationEntry.model_validate({"name": "CKA", "date": date, "expiry_date": expiry_date})


@pytest.mark.parametrize(
    ("date", "expiry_date"),
    [
        ("2024", "2024"),  # a year alone may be any month of it
        ("2024-03", "2024"),
        ("2024", "2024-01"),
        ("2024-03", "2024-03"),
        ("2024-02", "2024-03"),
        (None, "2020"),
        ("2030", None),
    ],
)
def test_a_certification_may_expire_on_or_after_its_date(
    date: str | None, expiry_date: str | None
) -> None:
    fields = {"name": "CKA", "date": date, "expiry_date": expiry_date}

    assert CertificationEntry.model_validate(fields).expiry_date == expiry_date


def test_a_certification_saved_with_its_expiry_first_still_loads() -> None:
    # Stored before the rule, or read from a resume: it is shown, so it can be put right.
    fields = {"name": "CKA", "date": "2025", "expiry_date": "2024"}

    assert CertificationInput.model_validate(fields).expiry_date == "2024"
    assert (
        Pool.model_validate(
            {"roles": [], "skills": [], "titles": [], "certifications": [{"id": "c1", **fields}]}
        )
        .certifications[0]
        .date
        == "2025"
    )


def test_a_pool_lists_its_certifications() -> None:
    pool = Pool.model_validate(
        {
            "roles": [],
            "skills": [],
            "titles": [],
            "certifications": [{"id": "c1", "name": "CKA", "date": "2022"}],
        }
    )

    assert [(item.id, item.name, item.issuer, item.date) for item in pool.certifications] == [
        ("c1", "CKA", None, "2022")
    ]
    with pytest.raises(ValidationError, match="certifications"):
        Pool.model_validate({"roles": [], "skills": [], "titles": []})


def test_a_pool_reads_a_summary_saved_before_the_limit_was_lowered() -> None:
    empty: dict[str, object] = {"roles": [], "skills": [], "titles": [], "certifications": []}
    older = "s" * 5000

    assert Pool.model_validate({**empty, "summary": older}).summary == older
    with pytest.raises(ValidationError, match="summary"):
        Pool.model_validate({**empty, "summary": older + "s"})
