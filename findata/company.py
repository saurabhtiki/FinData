"""Asking the host app about companies and permissions (FinData owns neither)."""

import logging
import re
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from . import conf

logger = logging.getLogger(__name__)

COMPANY_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")


@dataclass(frozen=True)
class Company:
    key: str  # e.g. "acme"
    name: str  # e.g. "ACME Ltd"

    @property
    def schema(self) -> str:
        """The PostgreSQL schema holding this company's tables, e.g. co_acme."""
        return schema_for(self.key)


def schema_for(company_key: str) -> str:
    return f"co_{company_key}"


def _hook(setting: str):
    path = conf.get(setting)
    if not path:
        return None
    try:
        return import_string(path) if isinstance(path, str) else path
    except ImportError as error:
        logger.error("FINDATA %s = %r can't be imported: %s", setting, path, error)
        raise ImproperlyConfigured(
            f"FINDATA[{setting!r}] = {path!r} can't be imported. Check the path in settings.py."
        ) from error


def _to_company(value) -> Company:
    if not (isinstance(value, list | tuple) and len(value) == 2):
        logger.error("Host gave a company as %r", value)
        raise ImproperlyConfigured(
            f"The host app gave the company as {value!r}. FinData expects a pair "
            "(key, name), e.g. ('acme', 'ACME Ltd')."
        )
    key, name = value
    if not isinstance(key, str) or not COMPANY_KEY.fullmatch(key):
        logger.error("Host gave an invalid company key: %r", key)
        raise ImproperlyConfigured(
            f"The host app gave the company key {key!r}. A key must start with a small letter "
            "and use only small letters, digits and _ (at most 40), e.g. 'acme' or 'acme_india'."
        )
    return Company(key, str(name))


def current_company(request) -> Company | None:
    hook = _hook("GET_COMPANY")
    if hook is None:
        logger.error("FINDATA['GET_COMPANY'] is not set")
        raise ImproperlyConfigured(
            "FinData needs FINDATA['GET_COMPANY'] in settings.py: a function that takes the "
            "request and returns the current company as (key, name), e.g. ('acme', 'ACME Ltd')."
        )
    value = hook(request)
    return _to_company(value) if value else None


def other_companies(request, current: Company) -> list[Company]:
    """Companies the user may copy definitions from (not the current one)."""
    hook = _hook("LIST_COMPANIES")
    if hook is None:
        return []
    return [company for company in map(_to_company, hook(request)) if company.key != current.key]


def can_edit_definitions(request) -> bool:
    hook = _hook("CAN_EDIT_DEFINITIONS")
    if hook is None:
        return bool(getattr(request.user, "is_staff", False))
    return bool(hook(request))


def can_upload(request) -> bool:
    hook = _hook("CAN_UPLOAD")
    return True if hook is None else bool(hook(request))
