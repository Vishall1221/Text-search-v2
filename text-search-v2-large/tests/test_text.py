from app.text import fold, normalize, osa_distance, scrub_pii, strip_accents, trigrams


def test_fold_unifies_quotes_case_and_latin_accents():
    assert fold("  Café  “Résumé”  ") == 'cafe "resume"'


def test_accent_folding_leaves_indic_scripts_alone():
    tamil = "இருள் நிழல்"
    assert strip_accents(tamil) == tamil
    assert normalize("मैं साथ चलूंगी") == "मैं साथ चलूंगी"


def test_normalize_possessives_and_apostrophes():
    assert normalize("Code Cipher's finale") == "code cipher finale"
    assert normalize("don't stop") == "dont stop"
    assert normalize("2010-2015") == "2010 to 2015"


def test_repeated_letters_in_names_are_preserved():
    assert normalize("Aarav and Meeera") == "aarav and meeera"          # no "mmovie -> movie" style collapsing


def test_osa_transposition_is_one_edit():
    assert osa_distance("nizhal", "nihzal", 3) == 1
    assert osa_distance("kitten", "sitting", 3) == 3
    assert osa_distance("abc", "xyzabcdef", 2) == 3                   # length gap beyond the limit


def test_trigrams_padded():
    assert "  r" in trigrams("raktha") and "ha " in trigrams("raktha")


def test_pii_scrubbed():
    clean, hit = scrub_pii("call 9876543210 or mail me@example.com about horror")
    assert hit and "9876543210" not in clean and "example.com" not in clean
    assert scrub_pii("tamil horror movies") == ("tamil horror movies", False)
