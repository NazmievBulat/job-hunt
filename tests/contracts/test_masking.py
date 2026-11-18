"""Contact details and a known name, swapped for placeholders before a model reads a text."""

import time

import pytest

from resume_kit.contracts.masking import Masks, UnresolvedPlaceholder, without_own_details
from resume_kit.contracts.resume import Person


def test_emails_phones_and_links_get_numbered_placeholders_and_come_back() -> None:
    masks = Masks()
    text = (
        "maria@example.com | +1 (555) 123-4567 | linkedin.com/in/maria\n"
        "https://github.com/maria, and again maria@example.com"
    )

    masked = masks.mask(text)

    assert masked == (
        "[EMAIL_1] | [PHONE_1] | linkedin.com/[LINK_1]\n"
        "https://github.com/[LINK_2], and again [EMAIL_1]"
    )
    assert masks.unmask(masked) == text


def test_a_link_to_a_profile_site_keeps_the_start_of_its_address() -> None:
    masks = Masks()

    assert masks.mask("www.LinkedIn.com/in/maria maria.dev/cv") == (
        "www.LinkedIn.com/[LINK_1] [LINK_2]"
    )
    # However the model writes the start, the whole address comes back once.
    assert masks.unmask(
        "[LINK_1] https://linkedin.com/[LINK_1] www.LinkedIn.com/[LINK_1] [LINK_2]"
    ) == " ".join(["www.LinkedIn.com/in/maria"] * 3 + ["maria.dev/cv"])


def test_a_name_is_known_once_the_text_held_it() -> None:
    masks = Masks(["Maria Lopez"])
    assert masks.name is None

    masks.mask("LOPEZ, MARIA")

    assert masks.name == "LOPEZ, MARIA"


def test_the_same_address_in_the_file_link_list_gets_the_same_placeholder() -> None:
    masks = Masks()

    assert masks.mask("See https://maria.dev/work.") == "See [LINK_1]."
    assert masks.mask("https://maria.dev/work") == "[LINK_1]"


@pytest.mark.parametrize(
    "written",
    [
        "Jan 2019 - Dec 2021",
        "2019 - 2023",
        "2019-2023",
        "01/2020 - 03/2021",
        "2015 \u2013 2019",
        "Grew revenue 12.5% to $1,200,000",
        "Served 3,000,000 users",
        "Booking.com",
        "Python 3.12",
        "201801 02019412",
        "Bachelor of Science, 2010-01 0201401",
        "Order 123456789",
        "Telecom order 612345678",
        "Acme Inc., 2015-2019 (4 yrs)",
        "Acme, 3 (2019-2023)",
        "01.2015-12.2019",
        "2015.01-2019.12",
        "2020-01-2021-03",
        "Awards 2019 2020 2021",
        "12.000.000.000",
        "192.168.100.200",
        "Skills: Node.js/React, ASP.NET/C#, Vue.js/Nuxt, Next.js/TypeScript",
        "M.Sc/MBA",
    ],
)
def test_dates_amounts_and_names_of_companies_are_left_as_they_are(written: str) -> None:
    assert Masks().mask(written) == written


@pytest.mark.parametrize(
    "phone",
    ["+44 20 7946 0958", "(555) 123-4567", "555.123.4567", "+7 999 123-45-67", "5551234567"],
)
def test_phones_in_common_spellings_are_masked(phone: str) -> None:
    assert Masks().mask(f"Phone: {phone}.") == "Phone: [PHONE_1]."


def test_a_known_name_is_masked_in_the_common_orders_and_cases() -> None:
    masks = Masks(["Maria Lopez"])
    text = "MARIA LOPEZ\nLopez, Maria\nMaria Lopez\nMaria J. Lopez"

    # One placeholder for each spelling, so each comes back as it was written.
    masked = masks.mask(text)
    assert masked == "[NAME]\n[NAME_2]\n[NAME_3]\n[NAME_4]"
    assert masks.unmask(masked) == text


def test_a_name_comes_back_as_it_was_first_written() -> None:
    masks = Masks(["Maria Lopez"])
    masks.mask("MARIA LOPEZ\nMaria Lopez")

    assert masks.unmask("[NAME]") == "MARIA LOPEZ"


def test_a_name_is_matched_as_whole_words_only() -> None:
    masks = Masks(["Ann Lee"])

    assert masks.mask("Joann Leeds; Ann Leeway") == "Joann Leeds; Ann Leeway"


def test_a_one_word_name_is_not_masked() -> None:
    # A name of one word is too often an ordinary word ("Owner", "Grant").
    assert Masks(["Owner"]).mask("Product Owner at Owner Inc.") == "Product Owner at Owner Inc."


def test_names_from_the_account_and_the_profile_are_both_masked() -> None:
    masks = Masks(["Bob Stone", "Robert Stone", "  "])

    assert masks.mask("Robert Stone (Bob Stone)") == "[NAME] ([NAME_2])"


def test_a_name_in_another_script_is_masked() -> None:
    assert Masks(["Булат Назмиев"]).mask("БУЛАТ НАЗМИЕВ, инженер") == "[NAME], инженер"


def test_only_the_kinds_asked_for_are_masked() -> None:
    masks = Masks(kinds={"email", "phone"})

    assert masks.mask("a@b.co, +1 555 123 4567, https://x.io/a") == (
        "[EMAIL_1], [PHONE_1], https://x.io/a"
    )


@pytest.mark.parametrize("placeholder", ["[EMAIL_2]", "[NAME]", "[EMAIL_1", "[LINK", "[PHONE]"])
def test_a_placeholder_the_text_did_not_have_is_rejected(placeholder: str) -> None:
    masks = Masks()
    masks.mask("a@b.co")

    with pytest.raises(UnresolvedPlaceholder) as caught:
        masks.unmask(f"Private text [EMAIL_1] {placeholder}")
    assert "Private text" not in str(caught.value)


@pytest.mark.parametrize("placeholder", ["[EMAIL_1]", "[EMAIL_1", "[NAME]", "[LINK"])
def test_a_literal_placeholder_is_preserved_and_never_reused(placeholder: str) -> None:
    typed = f"Built {placeholder}; contact a@b.co"
    masks = Masks(given=[typed])

    masked = masks.mask(typed)

    assert masks.unmask(masked) == typed
    if placeholder.startswith("[EMAIL_1"):
        assert masked.endswith("[EMAIL_2]")


def test_unmask_reaches_every_string_in_a_structure() -> None:
    masks = Masks(["Maria Lopez"])
    masks.mask("Maria Lopez, maria@example.com")

    assert masks.unmask_all(
        {"name": "[NAME]", "links": [{"url": "[EMAIL_1]"}], "count": 2, "none": None}
    ) == {
        "name": "Maria Lopez",
        "links": [{"url": "maria@example.com"}],
        "count": 2,
        "none": None,
    }


PERSON = Person.model_validate(
    {
        "name": "Maria Lopez",
        "email": "maria@example.com",
        "phone": "+1 555 123 4567",
        "links": [{"label": "GitHub", "url": "https://github.com/maria"}],
    }
)


def test_the_persons_own_details_are_taken_out_of_a_text() -> None:
    text = (
        "Maria Lopez is a data engineer. Reach me at maria@example.com or +1 555 123 4567; "
        "code on github.com/maria."
    )

    assert without_own_details(text, PERSON) == (
        "the candidate is a data engineer. Reach me at an email address or a phone number; "
        "code on a personal link."
    )


def test_a_text_without_the_persons_details_is_unchanged() -> None:
    text = "Built a pipeline  for 3,000,000 users (contact: team@example.com)."

    assert without_own_details(text, PERSON) == text


@pytest.mark.parametrize(
    "written,masked",
    [
        ("555-123-4567 555-987-6543", "[PHONE_1] [PHONE_2]"),
        ("555-123-4567 2019", "[PHONE_1] 2019"),
        ("Call 2019 555-123-4567", "Call 2019 [PHONE_1]"),
        ("(555) 123-4567 (555) 987-6543", "[PHONE_1] [PHONE_2]"),
        ("+1 (555) 123-4567", "[PHONE_1]"),
        ("(212) 555 2019", "[PHONE_1]"),
        ("212-555-2019", "[PHONE_1]"),
        ("Phone: 98765 43210", "Phone: [PHONE_1]"),
        ("030 12345678", "[PHONE_1]"),
        ("Phone: 612345678", "Phone: [PHONE_1]"),
        ("+1\u00a0212\u00a0555\u00a01234", "[PHONE_1]"),
        ("Tel. 612345678", "Tel. [PHONE_1]"),
    ],
)
def test_phones_next_to_other_numbers_are_still_masked(written: str, masked: str) -> None:
    assert Masks().mask(written) == masked


def test_a_placeholder_the_text_already_held_comes_back_as_written() -> None:
    typed = "The UI showed [EMAIL_1] and [NAME] when I entered sam@example.com as Sam Lee."
    masks = Masks(["Sam Lee"], given=[typed])

    masked = masks.mask(typed)

    assert masked == "The UI showed [EMAIL_1] and [NAME] when I entered [EMAIL_2] as [NAME_2]."
    assert masks.unmask(masked) == typed
    assert masks.name == "Sam Lee"


def test_a_link_placeholder_written_with_more_of_the_path_comes_back_once() -> None:
    masks = Masks()
    masks.mask("https://www.linkedin.com/in/sam")

    assert masks.unmask("https://www.linkedin.com/in/[LINK_1]") == "https://www.linkedin.com/in/sam"


def test_a_saved_link_is_found_with_or_without_its_scheme_and_www() -> None:
    person = Person.model_validate(
        {"name": "Maria Lopez", "links": [{"label": "Site", "url": "https://www.maria.dev/cv"}]}
    )

    assert without_own_details("See https://maria.dev/cv and www.maria.dev/cv.", person) == (
        "See a personal link and a personal link."
    )


def test_a_bare_profile_site_or_a_short_phone_takes_nothing_else_out() -> None:
    person = Person.model_validate(
        {
            "name": "Maria Lopez",
            "phone": "1234",
            "links": [{"label": "GitHub", "url": "https://github.com/"}],
        }
    )
    text = "Built CI on github.com for 1234 services."

    assert without_own_details(text, person) == text


def test_an_address_with_letters_of_another_script_is_masked() -> None:
    assert Masks().mask("müller@exämple.de") == "[EMAIL_1]"


@pytest.mark.parametrize("line", [" ".join(["2019"] * 2000), "1 " + "- " * 3000 + "2"])
def test_a_long_line_of_numbers_or_marks_is_read_in_one_pass(line: str) -> None:
    # Count this thread's CPU work, including native regex backtracking. Descheduling and
    # unrelated server threads consume none of this budget; keep the original CPU limit.
    started = time.thread_time()
    assert Masks().mask(line) == line
    assert time.thread_time() - started < 1


def test_a_saved_link_is_found_with_a_slash_at_its_end_too() -> None:
    person = Person.model_validate(
        {"name": "Sam Lee", "links": [{"label": "LinkedIn", "url": "https://linkedin.com/in/sam"}]}
    )

    assert without_own_details("Profile https://linkedin.com/in/sam/ here", person) == (
        "Profile a personal link here"
    )


def test_an_address_with_its_scheme_in_capitals_is_masked() -> None:
    assert Masks().mask("HTTPS://samlee.dev/resume WWW.samlee.dev") == "[LINK_1] [LINK_2]"


def test_a_profile_link_with_its_scheme_in_capitals_comes_back_once() -> None:
    masks = Masks()

    masked = masks.mask("HTTPS://github.com/sam")

    assert masked == "HTTPS://github.com/[LINK_1]"
    assert masks.unmask(masked) == "HTTPS://github.com/sam"


def test_the_saved_phone_is_found_however_it_is_spaced() -> None:
    person = Person.model_validate({"name": "Sam Lee", "phone": "+1 (212) 555-1234"})

    assert without_own_details("Call +1 212 555 1234 or 1 212.555.1234 today", person) == (
        "Call a phone number or a phone number today"
    )


@pytest.mark.parametrize(
    "text",
    ["a" * 60000, "( " * 30000, "linkedin.com/" * 5000 + "x", "%" * 8000 + "@" + "a" * 24000],
)
def test_a_long_text_is_masked_and_given_back_in_one_pass(text: str) -> None:
    # Count this thread's CPU work, including native regex backtracking. Descheduling and
    # unrelated server threads consume none of this budget; keep the original CPU limit.
    started = time.thread_time()
    masks = Masks(given=[text])
    assert masks.unmask(masks.mask(text)) == text
    assert time.thread_time() - started < 1


def test_a_long_line_of_numbers_keeps_its_phone() -> None:
    line = " ".join(["2019"] * 50) + " +1 (555) 123-4567"

    assert Masks().mask(line).endswith(" [PHONE_1]")


def test_a_placeholder_written_after_a_bare_scheme_comes_back_once() -> None:
    masks = Masks()
    masks.mask("https://samlee.dev/work")

    assert masks.unmask("https://[LINK_1]") == "https://samlee.dev/work"
    assert masks.unmask("HTTP://[LINK_1]") == "https://samlee.dev/work"


def test_many_addresses_are_numbered_in_one_pass() -> None:
    text = " ".join(f"p{number}@example.com" for number in range(6000))

    # Count this thread's CPU work, including native regex backtracking. Descheduling and
    # unrelated server threads consume none of this budget; keep the original CPU limit.
    started = time.thread_time()
    masked = Masks(given=[text]).mask(text)
    assert masked.endswith("[EMAIL_6000]")
    assert time.thread_time() - started < 1


def test_a_name_saved_with_a_comma_is_masked_as_saved() -> None:
    assert Masks(["Rivera, Alex"]).mask("Rivera, Alex\nAlex Rivera") == "[NAME]\n[NAME_2]"


def test_the_saved_phone_with_an_extension_is_taken_out_as_saved() -> None:
    person = Person.model_validate({"name": "Sam Lee", "phone": "+44 20 7946 0958 ext. 10"})

    assert without_own_details("Call +44 20 7946 0958 ext. 10 today", person) == (
        "Call a phone number today"
    )


def test_each_spelling_of_the_name_comes_back_as_written_and_a_fact_is_kept() -> None:
    masks = Masks(["Alex Rivera"])
    text = "ALEX RIVERA\nFounded Alex Rivera Studio"

    masked = masks.mask(text)

    assert masked == "[NAME]\nFounded [NAME_2] Studio"
    assert masks.unmask("Founded [NAME_2] Studio") == "Founded Alex Rivera Studio"


def test_a_word_in_small_letters_between_the_two_names_is_no_middle_name() -> None:
    assert Masks(["Alex Rivera"]).mask("Met Alex at Rivera Park") == "Met Alex at Rivera Park"
    assert Masks(["Alex Rivera"]).mask("Alex Maria Rivera, Alex J. Rivera") == "[NAME], [NAME_2]"


@pytest.mark.parametrize(
    "written,masked",
    [
        ("alexrivera.com", "[LINK_1]"),
        ("Portfolio: alex-rivera.dev", "Portfolio: [LINK_1]"),
        ("alex-rivera.example.com (Personal)", "[LINK_1] (Personal)"),
        ("Twitter @alexrivera", "Twitter [LINK_1]"),
        ("Clients: Booking.com, @acme", "Clients: Booking.com, @acme"),
    ],
)
def test_a_bare_host_or_a_handle_holding_the_known_name_is_masked(
    written: str, masked: str
) -> None:
    masks = Masks(["Alex Rivera"])

    assert masks.mask(written) == masked
    assert masks.unmask(masked) == written


def test_a_bare_host_from_the_files_links_is_masked() -> None:
    masks = Masks(links=["https://sammakes.dev"])

    assert masks.mask("Site: sammakes.dev") == "Site: [LINK_1]"


@pytest.mark.parametrize(
    "written",
    ["1 200 000 000 RUB", "Grew from 100 000 000 to 250 000 000", "7 000 000 000 rows"],
)
def test_an_amount_in_thousands_is_no_phone(written: str) -> None:
    assert Masks().mask(written) == written


def test_a_phone_in_threes_is_masked_after_its_label() -> None:
    assert Masks().mask("Tel. 612 345 678") == "Tel. [PHONE_1]"


@pytest.mark.parametrize("written", ["(212) 555-1234", "212-555-1234", "212.555.1234"])
def test_the_saved_phone_is_found_without_its_countrys_code(written: str) -> None:
    person = Person.model_validate({"name": "Sam Lee", "phone": "+1 (212) 555-1234"})

    assert without_own_details(f"Call {written} now", person) == "Call a phone number now"


def test_a_saved_link_is_found_with_more_of_its_path() -> None:
    person = Person.model_validate(
        {
            "name": "Alex Rivera",
            "links": [
                {"label": "Site", "url": "https://alex.dev"},
                {"label": "GitHub", "url": "https://github.com/alex-rivera"},
            ],
        }
    )
    text = "Wrote at alex.dev/blog. Built github.com/alex-rivera/ledger."

    assert without_own_details(text, person) == ("Wrote at a personal link. Built a personal link.")


def test_a_name_in_a_bullet_is_replaced_by_a_neutral_word() -> None:
    assert without_own_details("Led Alex Rivera's team", Person(name="Alex Rivera")) == (
        "Led the candidate's team"
    )


def test_no_break_spaces_hide_neither_a_known_name_nor_the_saved_phone() -> None:
    assert Masks(["Sam Lee"]).mask("Sam\u00a0Lee") == "[NAME]"
    person = Person.model_validate({"name": "Sam Lee", "phone": "+1 212 555 1234"})

    assert without_own_details("Call +1\u00a0212\u00a0555\u00a01234 now", person) == (
        "Call a phone number now"
    )


@pytest.mark.parametrize(
    "name,text",
    [
        ("Will Price", "We will price the plan monthly"),
        ("Grace Hill", "A hill grace period"),
        ("Grace Hill", "Hill Grace Lane"),
    ],
)
def test_a_name_of_common_words_leaves_the_words_in_a_sentence(name: str, text: str) -> None:
    person = Person.model_validate({"name": name})

    assert Masks([name]).mask(text) == text
    assert without_own_details(text, person) == text


@pytest.mark.parametrize(
    "written",
    ["marketplace.com", "shopmark.io", "@marketingteam", "alexa.amazon.com"],
)
def test_a_host_or_handle_with_only_part_of_the_name_is_left(written: str) -> None:
    assert Masks(["Mark Chen", "Alex Rivera"]).mask(written) == written


@pytest.mark.parametrize("written", ["markchen.dev", "mark-chen.example.com", "@mark_chen"])
def test_a_host_or_handle_with_both_words_of_the_name_is_masked(written: str) -> None:
    assert Masks(["Mark Chen"]).mask(written) == "[LINK_1]"


@pytest.mark.parametrize(
    "saved,text",
    [
        ("Alex Rivera-Lopez", "Alex Rivera"),
        ("Alex Rivera-Lopez", "Alex Lopez"),
        ("Alex Rivera-Lopez", "Alex Rivera-Lopez"),
        ("Alex Rivera", "Alex Rivera-Lopez"),
        ("Alex Rivera", "Rivera-Lopez, Alex"),
    ],
)
def test_a_double_last_name_is_masked_whole_or_by_its_parts(saved: str, text: str) -> None:
    assert Masks([saved]).mask(text) == "[NAME]"


def test_a_reserved_placeholder_is_not_made_and_not_given_back() -> None:
    masks = Masks(kinds={"email"}, given=["sam@example.com"], reserved=["Is [EMAIL_1] it?"])

    assert masks.mask("sam@example.com") == "[EMAIL_2]"
    assert masks.unmask("[EMAIL_2]") == "sam@example.com"
    with pytest.raises(UnresolvedPlaceholder):
        masks.unmask("[EMAIL_1]")
