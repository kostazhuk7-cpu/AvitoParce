"""Tests for _fuzzy_match relevance function."""

import pytest

from avito_parser.analytics import _fuzzy_match


@pytest.mark.parametrize(
    "query,title,expected",
    [
        ("iPhone 16", "iPhone 16 Pro Max 256GB", True),
        ("iPhone 16", "iPhone 6 16GB", False),
        ("iPhone 16", "iPhone 5S 16GB 1 SIM", False),
        ("iPhone 16", "iPhone 6", False),
        ("iPhone 16", "iPhone 5S", False),
        ("iPhone 16", "iPhone 16 128GB", True),
        ("iPhone 16", "iPhone 16 Pro Max", True),
        ("iPhone 15 Pro Max", "iPhone 15 Pro Max 256GB", True),
        ("iPhone 15 Pro Max", "iPhone 14 Pro Max", False),
        ("iPhone 15", "iPhone 14 15GB RAM", False),
        ("iPhone", "iPhone 16 Pro Max", True),
        ("16", "iPhone 16GB", False),
        ("Samsung S24", "Samsung Galaxy S24 Ultra", True),
        ("Samsung S24", "Samsung Galaxy S23", True),
        ("", "Any title here", True),
        ("PlayStation 5", "PlayStation 5 Digital Edition", True),
        ("PlayStation 5", "PlayStation 4 Pro", True),
    ],
)
def test_fuzzy_match(query, title, expected):
    assert _fuzzy_match(query, title) is expected


@pytest.mark.parametrize(
    "query,title,min_ratio,expected",
    [
        ("iPhone 16 Pro", "iPhone 16", 0.8, False),
        ("iPhone 16 Pro", "iPhone 16 Pro Max", 0.5, True),
        ("abc def", "abc def ghi", 0.6, True),
        ("abc def", "abc xyz ghi", 0.6, False),
    ],
)
def test_fuzzy_match_custom_ratio(query, title, min_ratio, expected):
    assert _fuzzy_match(query, title, min_overlap_ratio=min_ratio) is expected
