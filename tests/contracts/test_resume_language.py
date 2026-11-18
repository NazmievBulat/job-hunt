"""Which language a resume is written in: the one the saved career is written in."""

import json
from pathlib import Path
from typing import get_args

import pytest

from resume_kit.contracts.generation import CandidateSnapshot, CareerSnapshot
from resume_kit.contracts.printable import unprintable_characters
from resume_kit.contracts.resume_language import (
    LANGUAGE_NAMES,
    PRINTED_WORDS,
    ResumeLanguage,
    printed_date,
    printed_language,
    profile_language,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def career(*bullets: str, title: str = "Nurse", summary: str | None = None) -> CareerSnapshot:
    return CareerSnapshot.model_validate(
        {
            "roles": [
                {
                    "id": "role-1",
                    "kind": "job",
                    "organization": "Harbor Clinic",
                    "title": title,
                    "start_date": "2019-03",
                    "end_date": "2023-06",
                    "bullets": [
                        {"id": f"b{index}", "role_id": "role-1", "text": text}
                        for index, text in enumerate(bullets)
                    ],
                }
            ],
            "skills": [],
            "titles": [],
            "summary": summary,
        }
    )


def test_the_profile_of_the_bug_hunt_is_english() -> None:
    # explore-2: one job and two short bullets, with a posting in German.
    assert (
        profile_language(
            career(
                "Cared for 30 patients a shift on the night ward.",
                "Trained 12 new nurses in triage.",
            )
        )
        == "en"
    )


def test_the_example_profile_is_english() -> None:
    candidate = CandidateSnapshot.model_validate_json(
        (EXAMPLES / "candidate.example.json").read_text(encoding="utf-8")
    )
    assert profile_language(candidate.career) == "en"


@pytest.mark.parametrize(
    ("language", "title", "bullets"),
    [
        (
            "de",
            "Pflegefachkraft",
            (
                "Betreuung von 30 Patienten pro Schicht auf der Nachtstation.",
                "Einarbeitung von 12 neuen Pflegekräften in die Ersteinschätzung.",
            ),
        ),
        (
            "fr",
            "Infirmière",
            (
                "Prise en charge de 30 patients par nuit dans le service des urgences.",
                "Formation de 12 nouvelles infirmières au triage.",
            ),
        ),
        (
            "es",
            "Enfermera",
            (
                "Atención a 30 pacientes por turno en la planta de noche.",
                "Formación de 12 enfermeras nuevas en el triaje y los protocolos del hospital.",
            ),
        ),
        (
            "it",
            "Infermiera",
            (
                "Assistenza a 30 pazienti per turno nel reparto di notte.",
                "Formazione di 12 nuove infermiere nella gestione del triage.",
            ),
        ),
        (
            "pt",
            "Enfermeira",
            (
                "Atendimento a 30 pacientes por turno na ala noturna.",
                "Formação de 12 novas enfermeiras em triagem e nos protocolos do hospital.",
            ),
        ),
        (
            "nl",
            "Verpleegkundige",
            (
                "Zorg voor 30 patiënten per dienst op de nachtafdeling.",
                "Inwerken van 12 nieuwe verpleegkundigen in de triage.",
            ),
        ),
    ],
)
def test_each_language_is_found_from_short_bullets(
    language: ResumeLanguage, title: str, bullets: tuple[str, ...]
) -> None:
    assert profile_language(career(*bullets, title=title)) == language


def test_the_summary_counts_with_the_bullets() -> None:
    found = profile_language(
        career(
            "Python, Kubernetes, Terraform.",
            title="Software Engineer",
            summary="Ingenieur mit zehn Jahren Erfahrung in der Entwicklung von Plattformen für "
            "den Zahlungsverkehr und mit Teams aus drei Ländern.",
        )
    )
    assert found == "de"


def test_english_job_titles_and_skills_do_not_outweigh_german_bullets() -> None:
    profile = career(
        "Entwicklung einer Plattform für den Zahlungsverkehr mit Python und Kubernetes.",
        "Aufbau der Pipeline für Tests und Auslieferung im Team.",
        title="Senior Software Engineer",
    )
    assert profile_language(profile) == "de"


@pytest.mark.parametrize(
    "bullets",
    [
        # None of the seven: the writer is told nothing, and the headings stay in English.
        ("Уход за 30 пациентами за смену в ночном отделении.", "Обучил 12 новых медсестёр."),
        (
            "Opieka nad 30 pacjentami na zmianie w oddziale nocnym.",
            "Szkolenie 12 nowych pielęgniarek.",
        ),
        # Polish shares "na", "a" and "o" with Portuguese, but writes letters Portuguese does not.
        ("Praca na oddziale ratunkowym.", "Szkolenie personelu na stanowisku pielęgniarki."),
        (
            "Praca na oddziale ratunkowym a także szkolenia o bezpieczeństwie pacjentów.",
            "Opieka nad pacjentami na zmianie nocnej.",
        ),
        # Romanian shares "de", "la", "a" and "un" with French, Spanish and Italian.
        (
            "Îngrijirea a 30 de pacienți pe tura de noapte.",
            "Instruirea unui personal nou la triaj.",
        ),
        # Nothing but names and numbers says no language at all.
        ("Python · Kubernetes · Terraform", "40% faster deploys"),
    ],
)
def test_a_profile_in_none_of_the_seven_languages_has_none(bullets: tuple[str, ...]) -> None:
    assert profile_language(career(*bullets, title="Python")) is None
    assert printed_language(career(*bullets, title="Python")) == "en"


def test_a_place_with_letters_from_elsewhere_does_not_stop_english() -> None:
    profile = career(
        "Cared for 30 patients a shift on the night ward of the hospital in Zürich.",
        "Trained 12 new nurses in triage and in the use of the ward's records.",
    )
    assert profile_language(profile) == "en"


def test_one_common_word_written_twice_is_not_enough() -> None:
    assert profile_language(career("Praca na oddziale.", "Szkolenie na stanowisku.")) is None


def test_capital_letters_at_the_start_of_a_sentence_count_as_well() -> None:
    profile = career("Die Station Mit Dem Team Und Der Klinik.", "Für Den Dienst Auf Der Station.")
    assert profile_language(profile) == "de"


def test_french_words_joined_by_an_apostrophe_count_straight_or_curly() -> None:
    # A word is split at the apostrophe, so "l'équipe" gives "l" and "d\u2019un" gives "d".
    profile = career("Gestion l'équipe d\u2019un service.", "Suivi qu'on fait l\u2019accueil.")
    assert profile_language(profile) == "fr"


def test_e_commerce_is_not_the_italian_word_e() -> None:
    profile = career(
        "Built an e-commerce site with Python.", "Ran e-mail campaigns.", title="Developer"
    )
    assert profile_language(profile) == "en"


def test_skills_are_not_counted() -> None:
    profile = CareerSnapshot.model_validate(
        {
            **career("Led the team of the night ward.", "Trained nurses in triage.").model_dump(),
            "skills": [
                {"id": str(index), "name": name}
                for index, name in enumerate(["für die Station", "mit der Klinik", "und den"])
            ],
        }
    )
    assert profile_language(profile) == "en"


def test_headline_titles_are_counted() -> None:
    profile = CareerSnapshot.model_validate(
        {
            **career().model_dump(),
            "titles": [{"id": "t1", "text": "Pflegefachkraft für die Notaufnahme und den Dienst"}],
        }
    )
    assert profile_language(profile) == "de"


def test_a_single_common_word_is_not_enough() -> None:
    assert profile_language(career("Cared for patients.")) is None


def test_a_profile_with_no_bullets_has_no_language() -> None:
    assert profile_language(career()) is None


def test_short_english_bullets_whose_only_common_word_is_shared_are_english() -> None:
    # "in" is German, Dutch and Italian too; letters A to Z alone tip it to English.
    profile = career(
        "Built REST APIs in TypeScript serving 2M requests per day",
        "Fixed bugs in Node.js services",
        title="Backend Engineer",
    )
    assert profile_language(profile) == "en"


def test_a_tie_with_letters_english_does_not_use_is_not_english() -> None:
    profile = career("Prüfung in Köln", "Planung in Düsseldorf", title="Ingenieur")
    assert profile_language(profile) is None


def test_two_languages_written_as_much_as_each_other_give_none() -> None:
    assert profile_language(career("Led the team.", "Leitung der Station.")) is None


def test_every_language_has_a_name_and_all_its_printed_words() -> None:
    languages = get_args(ResumeLanguage)
    assert set(LANGUAGE_NAMES) == set(languages)
    assert set(PRINTED_WORDS) == set(languages)
    for words in PRINTED_WORDS.values():
        assert len(words.months) == 12
        texts = [
            words.summary,
            words.skills,
            words.experience,
            words.education,
            words.certifications,
            words.present,
            words.page,
            *words.months,
        ]
        assert all(text.strip() == text and text for text in texts)
        # Every word must be one the PDF can print.
        assert not unprintable_characters("".join(texts))


def test_english_words_are_the_ones_printed_before_languages_existed() -> None:
    english = PRINTED_WORDS["en"]
    assert (english.summary, english.skills, english.experience, english.education) == (
        "SUMMARY",
        "SKILLS",
        "PROFESSIONAL EXPERIENCE",
        "EDUCATION & CREDENTIALS",
    )
    assert (english.certifications, english.present, english.page) == (
        "Certifications",
        "Present",
        "Page",
    )


@pytest.mark.parametrize(
    ("language", "value", "printed"),
    [
        ("en", "2019-03", "Mar 2019"),
        ("de", "2019-03", "März 2019"),
        ("fr", "2023-06", "juin 2023"),
        ("de", "2019", "2019"),
    ],
)
def test_a_date_is_printed_with_the_month_of_its_language(
    language: ResumeLanguage, value: str, printed: str
) -> None:
    assert printed_date(value, language) == printed


def test_the_browser_holds_the_same_printed_words() -> None:
    """the web page's copy is the copy the page reads."""
    copy = json.loads(
        (
            Path(__file__).resolve().parents[2] / "examples/packages-ui/printed-words.json"
        ).read_text(encoding="utf-8")
    )
    assert copy == {
        language: {**words._asdict(), "months": list(words.months)}
        for language, words in PRINTED_WORDS.items()
    }
