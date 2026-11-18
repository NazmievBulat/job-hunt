from resume_kit.contracts.pool import LooseRole, listed_newest_first


def _role(title: str, start: str | None, end: str | None = None) -> LooseRole:
    return LooseRole(kind="job", organization="Acme", title=title, start_date=start, end_date=end)


def _titles(roles: list[LooseRole]) -> list[str]:
    return [role.title for role in listed_newest_first(roles)]


def test_current_roles_come_first_newest_start_first() -> None:
    roles = [_role("Old current", "2015-01"), _role("Ended", "2019-01", "2024-01")]
    roles.append(_role("New current", "2022-05"))

    assert _titles(roles) == ["New current", "Old current", "Ended"]


def test_an_equal_end_is_ordered_by_start_and_then_as_added() -> None:
    roles = [
        _role("First added", "2018-01", "2020-01"),
        _role("Earlier start", "2017-01", "2020-01"),
        _role("Second added", "2018-01", "2020-01"),
        _role("Later start", "2019-01", "2020-01"),
    ]

    assert _titles(roles) == ["Later start", "First added", "Second added", "Earlier start"]


def test_an_end_without_a_start_is_listed_by_its_end_and_undated_ones_last() -> None:
    roles = [
        _role("No dates", None),
        _role("End only", None, "2021-06"),
        _role("Both", "2019-01", "2020-01"),
        _role("Also no dates", None),
    ]

    assert _titles(roles) == ["End only", "Both", "No dates", "Also no dates"]


def test_an_end_of_a_year_alone_counts_before_the_months_of_that_year() -> None:
    roles = [_role("Year end", "2018-01", "2020"), _role("Month end", "2018-01", "2020-06")]

    assert _titles(roles) == ["Month end", "Year end"]


def test_current_roles_with_the_same_start_keep_the_order_they_were_added() -> None:
    roles = [_role("First", "2021-01"), _role("Second", "2021-01"), _role("Third", "2021-01")]

    assert _titles(roles) == ["First", "Second", "Third"]
