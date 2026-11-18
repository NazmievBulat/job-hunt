import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from resume_kit.contracts.resume import Resume
from resume_kit.contracts.runs import (
    FailedResumeRun,
    PublicError,
    QueuedResumeRun,
    ResumeEdit,
    ResumeFiles,
    ResumeRunAdapter,
    ResumeRunResult,
    RunningResumeRun,
    SucceededResumeRun,
    back_from,
)


def _minimal_run_data() -> dict[str, object]:
    timestamp = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    return {
        "run_id": UUID("123e4567-e89b-12d3-a456-426614174000"),
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def test_queued_resume_run_accepts_minimal_data() -> None:
    data = _minimal_run_data()
    data["status"] = "queued"
    run = QueuedResumeRun.model_validate(data)
    assert run.run_id == data["run_id"]
    assert run.created_at == data["created_at"]
    assert run.updated_at == data["updated_at"]
    assert run.status == "queued"


def test_run_rejects_update_before_creation() -> None:
    data = _minimal_run_data()
    data["status"] = "queued"
    data["updated_at"] = datetime(2025, 12, 31, 23, 59, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="updated_at cannot be before created_at"):
        ResumeRunAdapter.validate_python(data)


def test_run_compares_timestamps_as_instants_across_offsets() -> None:
    data = _minimal_run_data()
    data["status"] = "queued"
    # 2025-12-31T20:00-05:00 is 2026-01-01T01:00Z: later instant, earlier wall clock.
    data["updated_at"] = datetime(2025, 12, 31, 20, 0, 0, tzinfo=timezone(timedelta(hours=-5)))

    run = ResumeRunAdapter.validate_python(data)

    assert run.updated_at == datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)


def test_running_resume_run_accepts_minimal_data() -> None:
    data = _minimal_run_data()
    data["status"] = "running"
    run = RunningResumeRun.model_validate(data)
    assert run.run_id == data["run_id"]
    assert run.created_at == data["created_at"]
    assert run.updated_at == data["updated_at"]
    assert run.status == "running"
    assert run.stage is None


def test_running_resume_run_accepts_stage() -> None:
    data = _minimal_run_data()
    data["status"] = "running"
    data["stage"] = "generating"
    run = RunningResumeRun.model_validate(data)
    assert run.run_id == data["run_id"]
    assert run.created_at == data["created_at"]
    assert run.updated_at == data["updated_at"]
    assert run.status == "running"
    assert run.stage == "generating"


def test_succeeded_resume_run_accepts_result() -> None:
    data = _minimal_run_data()
    data["status"] = "succeeded"
    resume_path = Path(__file__).parents[2] / "examples" / "resume.example.json"
    resume = Resume.model_validate_json(resume_path.read_text(encoding="utf-8"))
    data["result"] = {
        "resume": resume,
        "files": {"pdf_url": "https://example.com/resume.pdf"},
    }
    run = SucceededResumeRun.model_validate(data)
    assert run.run_id == data["run_id"]
    assert run.created_at == data["created_at"]
    assert run.updated_at == data["updated_at"]
    assert run.status == "succeeded"
    assert run.result.resume == resume
    assert run.result.files.pdf_url == "https://example.com/resume.pdf"


def _state_data(status: str) -> dict[str, object]:
    data = _minimal_run_data()
    data["status"] = status
    if status == "failed":
        data["error"] = {"code": "GENERATION_FAILED", "message": "Generation failed."}
    elif status == "succeeded":
        path = Path(__file__).parents[2] / "examples" / "resume.example.json"
        data["result"] = {
            "resume": Resume.model_validate_json(path.read_text(encoding="utf-8")),
            "files": {"pdf_url": "/downloads/resume.pdf"},
        }
    return data


@pytest.mark.parametrize(
    "code", ["GENERATION_FAILED", "RENDER_FAILED", "VALIDATION_FAILED", "INTERNAL_ERROR"]
)
def test_failed_run_accepts_public_error(code: str) -> None:
    data = _state_data("failed")
    data["error"] = {"code": code, "message": "  Could not complete the request.  "}
    run = FailedResumeRun.model_validate(data)
    assert run.error.code == code
    assert run.error.message == "Could not complete the request."


@pytest.mark.parametrize("stage", ["normalizing", "generating", "rendering", None])
def test_running_run_accepts_supported_stages(stage: str | None) -> None:
    data = _state_data("running")
    data["stage"] = stage
    assert RunningResumeRun.model_validate(data).stage == stage


@pytest.mark.parametrize("stage", ["failed", "queued", "", "GENERATING", 1])
def test_running_run_rejects_invalid_stage(stage: object) -> None:
    data = _state_data("running")
    data["stage"] = stage
    with pytest.raises(ValidationError) as exc:
        RunningResumeRun.model_validate(data)
    assert exc.value.errors()[0]["loc"] == ("stage",)


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed"])
@pytest.mark.parametrize("field", ["run_id", "created_at", "updated_at", "status"])
def test_run_requires_common_fields(status: str, field: str) -> None:
    data = _state_data(status)
    del data[field]
    with pytest.raises(ValidationError) as exc:
        ResumeRunAdapter.validate_python(data)
    expected = "union_tag_not_found" if field == "status" else "missing"
    assert exc.value.errors()[0]["type"] == expected


@pytest.mark.parametrize(("status", "field"), [("succeeded", "result"), ("failed", "error")])
def test_terminal_run_requires_payload(status: str, field: str) -> None:
    data = _state_data(status)
    del data[field]
    with pytest.raises(ValidationError) as exc:
        ResumeRunAdapter.validate_python(data)
    assert exc.value.errors()[0]["loc"] == (status, field)
    assert exc.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize(
    ("status", "field"),
    [
        ("queued", "stage"),
        ("queued", "result"),
        ("queued", "error"),
        ("running", "result"),
        ("running", "error"),
        ("succeeded", "error"),
        ("succeeded", "stage"),
        ("failed", "result"),
        ("failed", "stage"),
    ],
)
def test_run_rejects_fields_from_other_states(status: str, field: str) -> None:
    data = _state_data(status)
    data[field] = None
    with pytest.raises(ValidationError) as exc:
        ResumeRunAdapter.validate_python(data)
    assert exc.value.errors()[0]["type"] == "extra_forbidden"
    assert exc.value.errors()[0]["loc"] == (status, field)


@pytest.mark.parametrize("status", ["completed", "unknown", "", None, 1])
def test_adapter_rejects_invalid_status(status: object) -> None:
    data = _minimal_run_data()
    data["status"] = status
    with pytest.raises(ValidationError) as exc:
        ResumeRunAdapter.validate_python(data)
    assert exc.value.errors()[0]["type"] == "union_tag_invalid"


@pytest.mark.parametrize(
    ("status", "model"),
    [
        ("queued", QueuedResumeRun),
        ("running", RunningResumeRun),
        ("succeeded", SucceededResumeRun),
        ("failed", FailedResumeRun),
    ],
)
def test_adapter_selects_state_and_round_trips_json(
    status: str,
    model: type[QueuedResumeRun]
    | type[RunningResumeRun]
    | type[SucceededResumeRun]
    | type[FailedResumeRun],
) -> None:
    run = ResumeRunAdapter.validate_python(_state_data(status))
    assert type(run) is model
    restored = ResumeRunAdapter.validate_json(ResumeRunAdapter.dump_json(run))
    assert type(restored) is model
    assert restored == run


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_run_rejects_naive_timestamp(field: str) -> None:
    data = _state_data("queued")
    data[field] = datetime(2026, 1, 1)  # noqa: DTZ001 - verifies rejection of a naive timestamp
    with pytest.raises(ValidationError) as exc:
        QueuedResumeRun.model_validate(data)
    assert exc.value.errors()[0]["type"] == "timezone_aware"


@pytest.mark.parametrize("offset", [-5, 0, 5.5])
def test_run_converts_both_timestamps_to_utc(offset: float) -> None:
    data = _state_data("queued")
    local = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=offset)))
    data["created_at"] = local
    data["updated_at"] = local
    run = QueuedResumeRun.model_validate(data)
    expected = datetime(2026, 1, 1, tzinfo=UTC) - timedelta(hours=offset)
    assert run.created_at == expected
    assert run.updated_at == expected
    assert run.created_at.tzinfo is UTC
    assert run.updated_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "invalid"),
        ("created_at", "invalid"),
        ("updated_at", "2026-01-01T00:00:00"),
    ],
)
def test_run_rejects_invalid_json_identifiers_and_timestamps(field: str, value: str) -> None:
    data = QueuedResumeRun.model_validate(_state_data("queued")).model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError) as exc:
        ResumeRunAdapter.validate_json(json.dumps(data))
    assert exc.value.errors()[0]["loc"] == ("queued", field)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"pdf_url": None},
        {"docx_url": None},
        {"pdf_url": None, "docx_url": None},
    ],
)
def test_resume_files_requires_a_download(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="At least one"):
        ResumeFiles.model_validate(payload)


@pytest.mark.parametrize("fields", [("pdf_url",), ("docx_url",), ("pdf_url", "docx_url")])
def test_resume_files_accepts_one_or_both_downloads(fields: tuple[str, ...]) -> None:
    data = {field: "  /downloads/resume  " for field in fields}
    files = ResumeFiles.model_validate(data)
    assert files.model_dump(exclude_none=True) == {field: "/downloads/resume" for field in fields}


@pytest.mark.parametrize("field", ["pdf_url", "docx_url"])
@pytest.mark.parametrize("value", ["", " \t\n", 1, b"/downloads/resume"])
def test_resume_files_rejects_invalid_link(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as exc:
        ResumeFiles.model_validate({field: value})
    assert exc.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "RENDERING_FAILED", "message": "Failed."},
        {"code": "INTERNAL_ERROR", "message": " "},
        {"code": "INTERNAL_ERROR", "message": None},
        {"code": "INTERNAL_ERROR"},
        {"message": "Failed."},
        {"code": "INTERNAL_ERROR", "message": "Failed.", "trace": "private"},
    ],
)
def test_public_error_rejects_invalid_data(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PublicError.model_validate(payload)


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"resume": {}},
        {"resume": {}, "files": {"pdf_url": "/resume.pdf"}},
    ],
)
def test_succeeded_run_rejects_invalid_result(result: object) -> None:
    data = _state_data("succeeded")
    data["result"] = result
    with pytest.raises(ValidationError) as exc:
        SucceededResumeRun.model_validate(data)
    assert exc.value.errors()[0]["loc"][0] == "result"


# ── A resume changed by hand, as a page sends it ─────────────────────────────


def _edit(**changes: object) -> dict[str, object]:
    path = Path(__file__).parents[2] / "examples" / "resume.example.json"
    return {
        "resume": Resume.model_validate_json(path.read_text(encoding="utf-8")),
        "started_from": "2026-10-09T08:15:30.123456Z",
        "tab": "k3Jx_9-a",
        "sequence": 3,
        **changes,
    }


def _result_data(**changes: object) -> dict[str, object]:
    path = Path(__file__).parents[2] / "examples" / "resume.example.json"
    return {
        "resume": Resume.model_validate_json(path.read_text(encoding="utf-8")),
        "files": {"pdf_url": "/downloads/resume.pdf"},
        **changes,
    }


def test_a_resume_edit_reads_the_version_it_started_from_as_the_time_a_run_sends() -> None:
    edit = ResumeEdit.model_validate(_edit())

    assert edit.started_from == datetime(2026, 10, 9, 8, 15, 30, 123456, tzinfo=UTC)
    assert edit.tab == "k3Jx_9-a"
    assert edit.sequence == 3


def test_a_resume_edit_may_carry_the_largest_number_a_browser_counts_exactly() -> None:
    assert ResumeEdit.model_validate(_edit(sequence=2**53 - 1)).sequence == 9007199254740991


def test_a_resume_edit_reads_a_time_in_another_zone_as_the_same_moment() -> None:
    edit = ResumeEdit.model_validate(_edit(started_from="2026-10-09T10:15:30.123456+02:00"))

    assert edit.started_from == datetime(2026, 10, 9, 8, 15, 30, 123456, tzinfo=UTC)
    assert edit.started_from.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "changes",
    [
        {"started_from": "2026-10-09T08:15:30"},
        {"started_from": "last night"},
        {"started_from": 1791533730},
        {"started_from": None},
        {"tab": ""},
        {"tab": "two words"},
        {"tab": "a" * 65},
        {"tab": 7},
        {"sequence": 0},
        {"sequence": "3"},
        {"sequence": 1.5},
        {"sequence": True},
        {"sequence": 2**53},
        {"sequence": None},
        {"saved_by": "someone"},
    ],
    ids=[
        "no time zone",
        "not a time",
        "a number",
        "no version",
        "an empty tab",
        "a tab with a space",
        "a tab that is too long",
        "a tab that is a number",
        "a number below one",
        "a number sent as text",
        "a number that is not whole",
        "a number that is yes or no",
        "a number a browser cannot count to",
        "no number",
        "a field that does not exist",
    ],
)
def test_a_resume_edit_that_does_not_say_where_it_started_or_who_sends_it_is_refused(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ResumeEdit.model_validate(_edit(**changes))


def test_a_result_says_how_many_pages_its_pdf_has() -> None:
    assert ResumeRunResult.model_validate(_result_data(pdf_page_count=2)).pdf_page_count == 2


def test_a_result_saved_before_pages_were_counted_has_no_page_count() -> None:
    assert ResumeRunResult.model_validate(_result_data()).pdf_page_count is None


@pytest.mark.parametrize("count", [0, -1, 1.5, "2", True])
def test_a_page_count_is_a_whole_number_from_one_up(count: object) -> None:
    with pytest.raises(ValidationError):
        ResumeRunResult.model_validate(_result_data(pdf_page_count=count))


def test_a_result_with_no_pdf_cannot_have_a_page_count() -> None:
    with pytest.raises(ValidationError, match="page count"):
        ResumeRunResult.model_validate(
            _result_data(files={"docx_url": "/downloads/resume.docx"}, pdf_page_count=1)
        )


def test_counting_back_gives_the_time_before_which_something_is_old() -> None:
    now = datetime(2026, 10, 13, 9, 30, tzinfo=UTC)

    assert back_from(now, timedelta(days=30)) == datetime(2026, 9, 13, 9, 30, tzinfo=UTC)


def test_counting_back_further_than_time_goes_gives_the_earliest_time_there_is() -> None:
    now = datetime(2026, 10, 13, 9, 30, tzinfo=UTC)

    earliest = back_from(now, timedelta.max)

    assert earliest == datetime.min.replace(tzinfo=UTC)
    # The stores compare stored text, so its text must sort below any real stamp.
    assert earliest.isoformat(timespec="microseconds") < now.isoformat(timespec="microseconds")
