import pytest

from resume_kit.contracts.base import same_certification_name_key, same_name_key


@pytest.mark.parametrize(
    "name",
    ["Certified Scrum Master (CSM)", "CSM", "C++", "C#", ".NET Certification"],
)
def test_one_sentence_ending_period_is_only_ignored_for_certification_names(name: str) -> None:
    assert same_certification_name_key(name + ".") == same_certification_name_key(name)
    assert same_name_key(name + ".") != same_name_key(name)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("P.M.P.", "P.M.P"),
        ("Course P.M.P.", "Course P.M.P"),
        (".NET.", ".NET"),
        ("CSM..", "CSM."),
        ("CSM...", "CSM.."),
        ("CSM!", "CSM"),
        ("CSM。", "CSM"),
        ("CSM\uff0e", "CSM"),
        ("C++", "C#"),
        ("C#", "C"),
    ],
)
def test_meaningful_punctuation_stays_distinct(first: str, second: str) -> None:
    assert same_certification_name_key(first) != same_certification_name_key(second)


def test_existing_name_normalization_still_applies() -> None:
    assert same_certification_name_key("  Certified\u00a0 Scrum MASTER (CSM).\u00a0") == (
        same_certification_name_key("Certified Scrum Master (CSM)")
    )
    assert same_certification_name_key("CSM.\u200c \u200d") == same_certification_name_key("CSM")
    assert same_certification_name_key(".") == "."


@pytest.mark.parametrize("first", ["P\uff0eM\uff0eP.", "P\uff0eM.P.", "P.M.P\uff0e"])
def test_width_variants_of_dotted_acronyms_keep_the_same_identity(first: str) -> None:
    assert same_certification_name_key(first) == same_certification_name_key("P.M.P.")
    assert same_certification_name_key(first) != same_certification_name_key("P.M.P")
