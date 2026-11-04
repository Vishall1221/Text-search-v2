import pytest
from app.planner import plan_query
from tests.relevance_cases import catalog


@pytest.mark.parametrize('query,excluded',[
    ('movies without a character named Vijay',{'M05'}),
    ('movies without actor named Vijay',{'M03','M04'}),
    ('movies except director named Shankar',{'M03'}),
    ('movies not directed by Shankar',{'M03'}),
])
def test_exclusion_stays_with_the_requested_relationship(query,excluded):
    cat=catalog()
    plan=plan_query(query,cat)
    assert plan.clarification is None
    assert plan.allowed==set(cat.assets)-excluded,(plan.allowed,plan.trace)
    assert not plan.ignored_negations
