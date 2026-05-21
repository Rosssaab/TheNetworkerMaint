"""Sign-in for the maint app using username/password pairs from the environment."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from flask import current_app, session

SESSION_MAINT_ENV_USER = "maint_env_username"
SESSION_MAINT_ENV_IMPERSONATOR = "maint_env_impersonator_username"


@dataclass(frozen=True)
class MaintEnvUser:
    """Synthetic user for templates and admin checks (no ``users`` table row)."""

    username: str

    @property
    def user_id(self):
        return None

    @property
    def email(self):
        return self.username

    @property
    def admin_user(self):
        return True

    @property
    def is_verified(self):
        return True

    @property
    def is_profile_complete(self):
        return True

    @property
    def twofa_enabled(self):
        return False

    @property
    def first_name(self):
        return self.username

    @property
    def second_name(self):
        return ""


def load_maint_env_login_pairs() -> list[tuple[str, str]]:
    """Return configured (username, password) pairs (passwords are plain from env)."""
    pairs: list[tuple[str, str]] = []
    for i in (1, 2):
        user = (current_app.config.get(f"TNW_MAINT_LOGIN_USER_{i}") or "").strip()
        password = current_app.config.get(f"TNW_MAINT_LOGIN_PASSWORD_{i}") or ""
        if user and password:
            pairs.append((user, password))
    return pairs


def maint_env_login_configured() -> bool:
    return bool(load_maint_env_login_pairs())


def verify_maint_env_login(username: str, password: str) -> str | None:
    """Return the matched username, or None."""
    name = (username or "").strip()
    if not name or not password:
        return None
    for expected_user, expected_password in load_maint_env_login_pairs():
        if secrets.compare_digest(name, expected_user) and secrets.compare_digest(
            password, expected_password
        ):
            return expected_user
    return None


def session_has_maint_env_auth() -> bool:
    return bool(session.get(SESSION_MAINT_ENV_USER))


def get_maint_env_session_user() -> MaintEnvUser | None:
    name = session.get(SESSION_MAINT_ENV_USER)
    if not name:
        return None
    return MaintEnvUser(str(name))


def get_maint_env_impersonator_user() -> MaintEnvUser | None:
    name = session.get(SESSION_MAINT_ENV_IMPERSONATOR)
    if not name:
        return None
    return MaintEnvUser(str(name))


def clear_maint_env_session_keys() -> None:
    session.pop(SESSION_MAINT_ENV_USER, None)
    session.pop(SESSION_MAINT_ENV_IMPERSONATOR, None)
