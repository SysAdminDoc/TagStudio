# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


from tagstudio.core.query_lang.completions import query_completions
from tagstudio.core.query_lang.parser import Parser

COMPLETION_DATA = {
    "tag_names": ["cat", "outdoor", "porch", "red fox"],
    "tag_ids": [11, 12, 13, 14],
    "paths": ["photos/outdoor", "photos/porch shots"],
    "media_types": ["image", "plain text"],
    "file_types": ["jpg", "png"],
}


def test_nested_boolean_grammar_parses():
    Parser("cat AND (outdoor OR porch) NOT blurry").parse()


def test_completions_offer_boolean_operators_after_terms():
    completions = query_completions("cat ", **COMPLETION_DATA)

    assert "cat AND" in completions
    assert "cat OR" in completions
    assert "cat NOT" in completions


def test_completions_keep_nested_query_prefix():
    completions = query_completions("cat AND (tag:out", **COMPLETION_DATA)

    assert "cat AND (tag:outdoor" in completions


def test_completions_offer_terms_after_boolean_operator_and_parenthesis():
    completions = query_completions("cat AND (outdoor OR ", **COMPLETION_DATA)

    assert "cat AND (outdoor OR tag:" in completions
    assert "cat AND (outdoor OR porch" in completions


def test_completions_quote_values_with_spaces():
    completions = query_completions('tag:"red ', **COMPLETION_DATA)

    assert 'tag:"red fox"' in completions


def test_completions_include_constraint_prefixes_at_query_start():
    completions = query_completions("", **COMPLETION_DATA)

    assert "tag:" in completions
    assert "special:untagged" in completions
