import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from avito_parser.analytics import _fuzzy_match

def test_exact_match():
    assert _fuzzy_match("iPhone 16", "iPhone 16 Pro Max 256GB")

def test_no_false_numeric():
    assert not _fuzzy_match("iPhone 16", "iPhone 6 16GB")
    assert not _fuzzy_match("iPhone 16", "iPhone 5S 16GB 1 SIM")

def test_no_older_model():
    assert not _fuzzy_match("iPhone 16", "iPhone 6")
    assert not _fuzzy_match("iPhone 16", "iPhone 5S")

def test_relevant_model():
    assert _fuzzy_match("iPhone 16", "iPhone 16 128GB")
    assert _fuzzy_match("iPhone 16", "iPhone 16 Pro Max")

def test_multi_word_model():
    assert _fuzzy_match("iPhone 15 Pro Max", "iPhone 15 Pro Max 256GB")
    assert not _fuzzy_match("iPhone 15 Pro Max", "iPhone 14 Pro Max")

def test_model_vs_storage():
    assert not _fuzzy_match("iPhone 15", "iPhone 14 15GB RAM")

def test_single_token():
    assert _fuzzy_match("iPhone", "iPhone 16 Pro Max")
    assert not _fuzzy_match("16", "iPhone 16GB")

if __name__ == "__main__":
    test_exact_match()
    test_no_false_numeric()
    test_no_older_model()
    test_relevant_model()
    test_multi_word_model()
    test_model_vs_storage()
    test_single_token()
    print("ALL TESTS PASSED")
