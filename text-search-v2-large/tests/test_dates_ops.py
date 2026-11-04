from datetime import date

from app.dates import extract_years
from app.operators import extract_operators

TODAY = date(2026, 10, 29)


def years(text):
    toks = text.split()
    yr, used = extract_years(toks, set(), TODAY)
    return (yr.gte, yr.lte) if yr else None


def test_year_phrases():
    assert years("movies before 2000") == (None, 1999)
    assert years("after 2020") == (2021, None)
    assert years("since 2015") == (2015, None)
    assert years("released in 2019") == (2019, 2019)
    assert years("between 2010 and 2015") == (2010, 2015)
    assert years("2010 to 2015") == (2010, 2015)
    assert years("in the 90s") == (1990, 1999)
    assert years("1980s") == (1980, 1989)
    assert years("early 2000s") == (2000, 2003)
    assert years("nineties") == (1990, 1999)
    assert years("last year") == (2025, 2025)
    assert years("last 5 years") == (2021, 2026)
    assert years("2018 or later") == (2018, None)
    assert years("up to 2005") == (None, 2005)
    assert years("2015 onwards") == (2015, None)
    assert years("2001 or earlier") == (None, 2001)
    assert years("earlier than 1990") == (None, 1989)
    assert years("released after 2010") == (2011, None)
    assert years("no dates here") is None


def ops(text, starts=None):
    toks = text.split()
    return extract_operators(toks, set(), starts)


def test_sort_and_popularity():
    assert ops("latest movies").sort == "latest"
    assert ops("oldest films").sort == "oldest"
    assert ops("recently added thrillers").sort == "added"
    assert ops("most popular movies").popularity == "most popular"
    assert ops("trending now").popularity == "trending"


def test_weak_sort_words_need_a_content_noun():
    assert ops("new movies").sort == "latest"
    assert ops("a new beginning for the hero").sort is None
    assert ops("classic car chase").sort is None


def test_top_n():
    assert ops("top 5 thrillers").limit == 5
    assert ops("give me three movies").limit == 3
    assert ops("first 10 results").limit == 10
    assert ops("3 horror films", {1}).limit == 3
    assert ops("the 2 brothers meet").limit is None            # no cue, no noun
    assert ops("1993 movies").limit is None                     # 4 digits are years, never counts
