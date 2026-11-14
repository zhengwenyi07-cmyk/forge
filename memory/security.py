"""Sanitization and trust-boundary helpers for persisted memory."""

from __future__ import annotations

import re
from dataclasses import dataclass


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd)\b\s*[:=]\s*['\"]?[^\s,'\"]{8,}"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s]+"),
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore\s+(?:all\s+)?previous\s+instructions?"),
    re.compile(r"(?i)(?:system|developer)\s+(?:message|prompt)"),
    re.compile(r"(?i)do\s+not\s+follow\s+(?:the\s+)?(?:system|developer)"),
    re.compile(r"(?i)you\s+are\s+now\s+(?:an?|the)\b"),
    re.compile(r"忽略(?:以上|之前|先前|所有).*?(?:指令|提示)"),
    re.compile(r"(?:系统|开发者)(?:消息|提示词|指令)"),
)


@dataclass(frozen=True)
class SanitizedText:
    text: str
    sensitivity: str
    injection_detected: bool = False
    rejected: bool = False


def sanitize_text(value: object, max_chars: int = 4000) -> SanitizedText:
    """Redact common credentials and flag instruction-shaped untrusted text."""
    text = str(value or "").replace("\x00", "")
    rejected = bool(_PRIVATE_KEY.search(text))
    if rejected:
        text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    redacted = rejected
    for pattern in _SECRET_PATTERNS:
        new_text, count = pattern.subn("[REDACTED SECRET]", text)
        text, redacted = new_text, redacted or count > 0
    injection = any(pattern.search(text) for pattern in _INJECTION_PATTERNS)
    if injection:
        # Preserve evidence that hostile text existed, but not executable phrasing.
        for pattern in _INJECTION_PATTERNS:
            text = pattern.sub("[UNTRUSTED INSTRUCTION REDACTED]", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "…[truncated]"
    return SanitizedText(
        text=text,
        sensitivity="rejected" if rejected else ("redacted" if redacted else "normal"),
        injection_detected=injection,
        rejected=rejected,
    )


def safe_label(value: object, max_chars: int = 240) -> str:
    """Return a display-safe label without URL userinfo or credentials."""
    text = str(value or "")
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1", text)
    return sanitize_text(text, max_chars=max_chars).text


def sanitize_structure(value: object, *, max_string_chars: int = 4000) -> object:
    """Recursively sanitize JSON-compatible evidence without breaking its shape."""
    if isinstance(value, dict):
        return {str(key)[:128]: sanitize_structure(item, max_string_chars=max_string_chars)
                for key, item in list(value.items())[:128]}
    if isinstance(value, list):
        return [sanitize_structure(item, max_string_chars=max_string_chars) for item in value[:256]]
    if isinstance(value, tuple):
        return [sanitize_structure(item, max_string_chars=max_string_chars) for item in value[:256]]
    if isinstance(value, str):
        return sanitize_text(value, max_chars=max_string_chars).text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_text(value, max_chars=max_string_chars).text
