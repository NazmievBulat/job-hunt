"""The settings a person gave /setup, read from data/settings.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_hunt.settings import Settings, SettingsError, read_settings

GOOD = {
    "titles": ["QA Automation Engineer", "SDET"],
    "areas": ["Raleigh-Durham-Chapel Hill", "New York City"],
    "remote": "include",
    "lowest_salary": 120000,
    "morning_hour": 7,
}


def _write(tmp_path: Path, settings: object) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    return path


def test_settings_are_read() -> None:
    path = Path(__file__).parent / "finder" / "settings.json"
    assert read_settings(path) == Settings(
        titles=["QA Automation Engineer", "SDET"],
        areas=["Raleigh-Durham-Chapel Hill", "San Francisco Bay Area"],
        remote="include",
        lowest_salary=120000,
        morning_hour=7,
    )


def test_a_missing_file_names_setup(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="/setup"):
        read_settings(tmp_path / "settings.json")


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"titles": []}, "titles"),
        ({"titles": ["  "]}, "titles"),
        ({"areas": ["Durham", ""]}, "areas"),
        ({"areas": "Durham"}, "areas"),
        ({"remote": "yes"}, "remote"),
        ({"remote": True}, "remote"),
        ({"lowest_salary": -1}, "lowest_salary"),
        ({"lowest_salary": "100k"}, "lowest_salary"),
        ({"morning_hour": 24}, "morning_hour"),
        ({"morning_hour": 7.5}, "morning_hour"),
        ({"miles": 30}, "miles"),
        # With no area and remote jobs left out, no job could ever be kept.
        ({"areas": [], "remote": "exclude"}, "areas"),
    ],
)
def test_settings_that_cannot_be_used_are_refused_naming_the_field(
    tmp_path: Path, change: dict[str, object], named: str
) -> None:
    with pytest.raises(SettingsError, match=named):
        read_settings(_write(tmp_path, {**GOOD, **change}))


def test_a_field_left_out_is_refused(tmp_path: Path) -> None:
    settings = dict(GOOD)
    del settings["remote"]
    with pytest.raises(SettingsError, match="remote"):
        read_settings(_write(tmp_path, settings))


def test_a_file_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{titles: ")
    with pytest.raises(SettingsError, match="not JSON"):
        read_settings(path)


def test_only_remote_jobs_needs_no_area_and_the_salary_floor_may_be_left_empty(tmp_path: Path) -> None:
    settings = read_settings(_write(tmp_path, {**GOOD, "areas": [], "remote": "only", "lowest_salary": None}))
    assert (settings.areas, settings.remote, settings.lowest_salary) == ([], "only", None)


def test_pushover_keys_are_optional(tmp_path: Path) -> None:
    assert read_settings(_write(tmp_path, GOOD)).pushover is None
    keys = {"token": "aExampleAppToken", "user": "uExampleUserKey"}

    settings = read_settings(_write(tmp_path, {**GOOD, "pushover": keys}))

    assert settings.pushover is not None
    assert (settings.pushover.token, settings.pushover.user) == ("aExampleAppToken", "uExampleUserKey")


@pytest.mark.parametrize("pushover", [{"token": "aExampleAppToken"}, {"token": "", "user": "u"}, "keys",
                                      {"token": "a", "user": "u", "device": "phone"}])
def test_pushover_keys_need_both_a_token_and_a_user(tmp_path: Path, pushover: object) -> None:
    with pytest.raises(SettingsError, match="pushover") as refused:
        read_settings(_write(tmp_path, {**GOOD, "pushover": pushover}))
    # The keys are never repeated in an error.
    assert "aExampleAppToken" not in str(refused.value)


def test_hunt_settings_saves_settings_it_can_read_and_keeps_the_pushover_keys(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from job_hunt import cli

    monkeypatch.setattr(cli, "DATA", tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({
        "titles": ["Old"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 6,
        "pushover": {"token": "t0ken", "user": "us3r"}}))
    given = {"titles": ["QA Engineer"], "areas": ["New York City"], "remote": "include", "lowest_salary": 120000,
             "morning_hour": 7}

    assert cli.main(["settings", json.dumps(given)]) == 0

    saved = read_settings(tmp_path / "settings.json")
    assert (saved.titles, saved.areas, saved.lowest_salary, saved.morning_hour) == (
        ["QA Engineer"], ["New York City"], 120000, 7)
    assert saved.pushover is not None and saved.pushover.token == "t0ken"
    printed = capsys.readouterr().out
    assert "QA Engineer" in printed and "t0ken" not in printed


def test_hunt_settings_refuses_settings_it_cannot_read_and_keeps_the_old_ones(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from job_hunt import cli

    monkeypatch.setattr(cli, "DATA", tmp_path)
    before = json.dumps({"titles": ["Old"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 6})
    (tmp_path / "settings.json").write_text(before)

    assert cli.main(["settings", json.dumps({"titles": [], "areas": [], "remote": "only", "lowest_salary": None,
                                             "morning_hour": 7})]) == 1

    assert "titles must name at least one job title" in capsys.readouterr().err
    assert (tmp_path / "settings.json").read_text() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["settings.json"]


def test_hunt_settings_replaces_a_settings_file_that_cannot_be_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from job_hunt import cli

    monkeypatch.setattr(cli, "DATA", tmp_path)
    for broken in ("{", "[1, 2]"):
        (tmp_path / "settings.json").write_text(broken)

        assert cli.main(["settings", json.dumps(GOOD)]) == 0

        assert read_settings(tmp_path / "settings.json").titles == GOOD["titles"]


def test_the_summary_hour_is_18_unless_the_settings_give_another(tmp_path: Path) -> None:
    assert read_settings(_write(tmp_path, GOOD)).summary_hour == 18
    assert read_settings(_write(tmp_path, {**GOOD, "summary_hour": 20})).summary_hour == 20


@pytest.mark.parametrize("summary_hour", [7, 6, 24, "18", True])
def test_a_summary_hour_not_after_the_morning_hour_is_refused(tmp_path: Path, summary_hour: object) -> None:
    with pytest.raises(SettingsError, match="summary_hour"):
        read_settings(_write(tmp_path, {**GOOD, "summary_hour": summary_hour}))
