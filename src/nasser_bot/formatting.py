from __future__ import annotations

import html
import math
import re
from collections.abc import Iterable


TELEGRAM_TEXT_LIMIT = 3900


def h(value: object) -> str:
    return html.escape(str(value), quote=False)


def code(value: object) -> str:
    return f"<code>{h(value)}</code>"


def bold(value: object) -> str:
    return f"<b>{h(value)}</b>"


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(number):
        return "unknown"
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    idx = 0
    while abs(number) >= 1024 and idx < len(units) - 1:
        number /= 1024
        idx += 1
    if idx == 0:
        return f"{int(number)} {units[idx]}"
    return f"{number:.1f} {units[idx]}"


def percent(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "unknown"


def bullet_lines(lines: Iterable[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def truncate_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n\n[truncated]"
    # Reserve room for the suffix plus closing tags so a cut never produces
    # HTML that Telegram rejects.
    cut = text[: limit - len(suffix) - len("</code></pre></b>")]
    open_bracket = cut.rfind("<")
    if open_bracket != -1 and ">" not in cut[open_bracket:]:
        cut = cut[:open_bracket]
    ampersand = cut.rfind("&")
    if ampersand != -1 and ";" not in cut[ampersand:]:
        cut = cut[:ampersand]
    return cut + _close_open_tags(cut) + suffix


def _close_open_tags(text: str) -> str:
    stack: list[str] = []
    for match in re.finditer(r"<(/?)(b|code|pre)>", text):
        closing, tag = match.groups()
        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)
    return "".join(f"</{tag}>" for tag in reversed(stack))

