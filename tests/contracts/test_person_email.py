import pytest
from pydantic import ValidationError

from resume_kit.contracts.resume import Person


@pytest.mark.parametrize(
    "email",
    [
        "marisol@example.com",
        "Marisol Okonkwo <marisol@example.com>",
        "mailto:marisol@example.com",
        "MAILTO:marisol@example.com?subject=Hello",
        "  MaIlTo:marisol@example.com?subject=Hello&body=Hi  ",
    ],
)
def test_person_keeps_the_bare_email_address(email: str) -> None:
    assert Person(name="Marisol", email=email).email == "marisol@example.com"


@pytest.mark.parametrize(
    "email", ["mailto:", "mailto:?subject=Hello", "mailto:broken?subject=Hello", "broken"]
)
def test_mailto_still_requires_a_valid_address(email: str) -> None:
    with pytest.raises(ValidationError):
        Person(name="Marisol", email=email)


def test_email_can_still_be_absent() -> None:
    assert Person(name="Marisol").email is None


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("mailto:marisol%2Bjobs@example.com?subject=Hello", "marisol+jobs@example.com"),
        ("mailto:marisol+jobs@example.com", "marisol+jobs@example.com"),
        ("mailto:marisol%40example.com", "marisol@example.com"),
        ("mailto:marisol%3Fjobs@example.com?subject=Hello", "marisol?jobs@example.com"),
        ("mailto:marisol%252Bjobs@example.com", "marisol%2Bjobs@example.com"),
        ("marisol%2Bjobs@example.com", "marisol%2Bjobs@example.com"),
    ],
)
def test_mailto_decodes_the_recipient_once_and_preserves_plus(email: str, expected: str) -> None:
    assert Person(name="Marisol", email=email).email == expected
