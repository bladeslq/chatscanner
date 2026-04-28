"""Shared helpers for handlers."""
import re

_PHONE_RE = re.compile(
    r"(?:\+?7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}"
)
_USERNAME_RE = re.compile(r"@([a-zA-Z][a-zA-Z0-9_]{3,31})")


def extract_phones(text: str) -> list[str]:
    """Return unique phones in E.164 (+7XXXXXXXXXX) order of appearance."""
    seen = []
    for raw in _PHONE_RE.findall(text or ""):
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits[0] in ("7", "8"):
            normalized = "+7" + digits[1:]
        elif len(digits) == 10:
            normalized = "+7" + digits
        else:
            continue
        if normalized not in seen:
            seen.append(normalized)
    return seen


def extract_usernames(text: str) -> list[str]:
    seen = []
    for u in _USERNAME_RE.findall(text or ""):
        if u not in seen:
            seen.append(u)
    return seen


def format_phone_pretty(e164: str) -> str:
    """+79991234567 → +7 999 123-45-67"""
    if not e164.startswith("+7") or len(e164) != 12:
        return e164
    d = e164[2:]
    return f"+7 {d[0:3]} {d[3:6]}-{d[6:8]}-{d[8:10]}"


def build_contacts_line(text: str) -> str | None:
    """Plain text line with phones and @usernames extracted from `text`.
    Telegram clients auto-link phones to dialer; we wrap usernames as t.me links.
    Returns None if nothing found.
    """
    phones = extract_phones(text)
    usernames = extract_usernames(text)
    if not phones and not usernames:
        return None
    parts = [format_phone_pretty(p) for p in phones]
    parts += [f'<a href="https://t.me/{u}">@{u}</a>' for u in usernames]
    return "📞 " + " · ".join(parts)
