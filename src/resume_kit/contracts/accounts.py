"""Who is signed in, and what a sign-in provider says about a person.

An account is a person who may use the application. How they prove who they are is kept apart
from it (`sign_in_methods`, one row per way), so another way can be added without changing what
an account is.
"""

from typing import Literal

from resume_kit.contracts.base import StrictModel


class Account(StrictModel):
    id: str
    email: str
    name: str


class Identity(StrictModel):
    """What the sign-in provider answered about the person who signed in there.

    `subject` is the provider's own name for the person and never changes; the address can.
    """

    provider: Literal["google"]
    subject: str
    email: str
    name: str


class SessionView(StrictModel):
    """What the web application is told about the session it holds."""

    account: Account
    # Said only to the admin, so the menu shows the admin page; left out for everyone else, who
    # is not told the page exists (`api/admin_routes.py`).
    admin: Literal[True] | None = None
