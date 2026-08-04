# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


from collections.abc import Iterable

CONSTRAINT_COMPLETIONS = (
    "mediatype:",
    "filetype:",
    "path:",
    "tag:",
    "tag_id:",
    "special:untagged",
)
BOOLEAN_OPERATORS = ("AND", "OR", "NOT")
CONSTRAINT_NAMES = {completion[:-1] for completion in CONSTRAINT_COMPLETIONS[:-1]} | {"special"}


def query_completions(
    text: str,
    *,
    tag_names: Iterable[str] = (),
    tag_ids: Iterable[int] = (),
    paths: Iterable[str] = (),
    media_types: Iterable[str] = (),
    file_types: Iterable[str] = (),
) -> list[str]:
    """Return full-query completions for the active query fragment.

    Completions retain the query prefix, so they work for terms after boolean operators and
    opening parentheses. Values that need quoting are returned in the syntax accepted by the
    tokenizer.
    """
    prefix, fragment = _split_active_fragment(text)
    expects_term = _expects_term(prefix)
    completions: list[str] = []

    if ":" in fragment:
        constraint, value_fragment = fragment.split(":", 1)
        constraint_name = constraint.lower()
        if constraint_name in CONSTRAINT_NAMES:
            values = _values_for_constraint(
                constraint_name,
                tag_names=tag_names,
                tag_ids=tag_ids,
                paths=paths,
                media_types=media_types,
                file_types=file_types,
            )
            value_prefix, quote = _value_prefix(value_fragment)
            for value in values:
                if value.casefold().startswith(value_prefix.casefold()):
                    literal = _quote_literal(value, quote)
                    completions.append(f"{prefix}{constraint}:{literal}")
            return _dedupe(completions)

    if expects_term:
        _append_matching(completions, prefix, CONSTRAINT_COMPLETIONS, fragment)
        _append_matching(completions, prefix, ("special:untagged",), fragment)

    _append_matching(completions, prefix, BOOLEAN_OPERATORS, fragment)
    bare_tags = [_quote_literal(tag_name) for tag_name in tag_names]
    _append_matching(completions, prefix, bare_tags, fragment)

    return _dedupe(completions)


def _split_active_fragment(text: str) -> tuple[str, str]:
    """Split query text at the last whitespace or grouping delimiter outside quotes."""
    start = 0
    quote: str | None = None
    for index, character in enumerate(text):
        if character in ('"', "'") and (index == 0 or text[index - 1] != "\\"):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        elif quote is None and (character.isspace() or character in "()"):
            start = index + 1
    return text[:start], text[start:]


def _expects_term(prefix: str) -> bool:
    stripped = prefix.rstrip()
    if not stripped or stripped.endswith("("):
        return True
    return stripped.rsplit(None, 1)[-1].upper() in BOOLEAN_OPERATORS


def _values_for_constraint(
    constraint: str,
    *,
    tag_names: Iterable[str],
    tag_ids: Iterable[int],
    paths: Iterable[str],
    media_types: Iterable[str],
    file_types: Iterable[str],
) -> Iterable[str]:
    match constraint:
        case "tag":
            return tag_names
        case "tag_id":
            return (str(tag_id) for tag_id in tag_ids)
        case "path":
            return paths
        case "mediatype":
            return media_types
        case "filetype":
            return file_types
        case "special":
            return ("untagged",)
        case _:
            return ()


def _value_prefix(value_fragment: str) -> tuple[str, str | None]:
    if value_fragment.startswith(('"', "'")):
        quote = value_fragment[0]
        value = value_fragment[1:]
        if value.endswith(quote):
            value = value[:-1]
        return value, quote
    return value_fragment, None


def _quote_literal(value: str, quote: str | None = None) -> str:
    requires_quotes = not value or any(
        character.isspace() or character in "():[],=\"'" for character in value
    )
    if quote is None and not requires_quotes:
        return value
    selected_quote = quote or '"'
    escaped = value.replace("\\", "\\\\").replace(selected_quote, "\\" + selected_quote)
    return f"{selected_quote}{escaped}{selected_quote}"


def _append_matching(
    completions: list[str], prefix: str, candidates: Iterable[str], fragment: str
) -> None:
    fragment_lower = fragment.casefold()
    for candidate in candidates:
        if candidate.casefold().startswith(fragment_lower):
            completions.append(prefix + candidate)


def _dedupe(completions: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(completions))
