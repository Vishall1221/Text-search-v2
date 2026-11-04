"""Compare fixed, hand-labelled synthetic planner cases. No DB/model is required.

python -m scripts.evaluate_relevance --output runtime/relevance/before.json
python -m scripts.evaluate_relevance --output runtime/relevance/after.json --compare runtime/relevance/before.json

This measures interpretation/filter correctness, not dense retrieval quality.
The ordered listing simulates only existing title/browse service ordering. Semantic
queries need a separate real-DB/end-to-end evaluation and are never counted as
successful recommendations here.
"""
import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import statistics
import time

from app.planner import plan_query
from app.service import SearchService
from tests.relevance_cases import CASES,catalog


def evaluate(cat):
    outcomes=[]
    for case in CASES:
        start=time.perf_counter()
        plan=plan_query(case['query'],cat,date(2026,11,4))
        elapsed=(time.perf_counter()-start)*1000
        kinds=sorted({f.kind for f in plan.facets})
        allowed=None if plan.allowed is None else sorted(plan.allowed)
        failures=[]
        if case['expected'] is not None and allowed != case['expected']:
            failures.append('allowed')
        if bool(plan.clarification) != case['clarify']:
            failures.append('clarification')
        if case['kinds'] is not None and kinds != sorted(case['kinds']):
            # Content-type is grammatical context, not a competing entity role.
            if sorted(k for k in kinds if k!='content_type') != sorted(case['kinds']):
                failures.append('entity_role')
        if set(kinds)&set(case['forbidden']): failures.append('forbidden_facet')
        if case['mode'] is not None and plan.mode != case['mode']: failures.append('mode')
        ordering=(lambda k: SearchService._listing_key(None,plan,cat,k)
                  if hasattr(SearchService,'_listing_key') else (cat.assets[k].title_norm,k))
        listing=(sorted(plan.allowed if plan.allowed is not None else cat.assets,key=ordering)
                 if plan.mode in {'title','browse'} and not plan.clarification else None)
        expected=set(case['expected'] or [])
        actual=set(allowed or [])
        outcomes.append(dict(**case,passed=not failures,failures=failures,actual_allowed=allowed,
            actual_kinds=kinds,actual_mode=plan.mode,
            clarification=None if not plan.clarification else plan.clarification.reason,
            listing_order=listing,first_relevant_rank=(next((i for i,k in enumerate(listing,1) if k in expected),None)
                if listing is not None and expected else None),
            false_inclusions=sorted(actual-expected) if case['expected'] is not None else None,
            missing=sorted(expected-actual) if case['expected'] is not None else None,
            terms=plan.terms,trace=plan.trace,notes=plan.notes,elapsed_ms=round(elapsed,3)))
    return outcomes


def benchmark(cat):
    times=[]
    queries=[c['query'] for c in CASES]
    for _ in range(3):
        for query in queries:
            start=time.perf_counter()
            plan_query(query,cat,date(2026,11,4))
            times.append((time.perf_counter()-start)*1000)
    ordered=sorted(times)
    return dict(assets=len(cat.assets),requests=len(times),p50_ms=round(statistics.median(times),3),
                p95_ms=round(ordered[int(.95*(len(ordered)-1))],3),max_ms=round(max(times),3),
                scope='in-process planner only; serial warm runs; no database, BGE or HTTP')


def filter_metrics(outcomes):
    labelled=[case for case in outcomes if case['expected'] is not None]
    tp=fp=fn=0
    for case in labelled:
        expected=set(case['expected'])
        actual=set(case['actual_allowed'] or [])
        tp+=len(expected&actual)
        fp+=len(actual-expected)
        fn+=len(expected-actual)
    return dict(scope='hard-filter eligibility on finite-set judgments only; not retrieval precision',
                queries=len(labelled),true_positive=tp,false_positive=fp,false_negative=fn,
                micro_precision=round(tp/max(tp+fp,1),4),micro_recall=round(tp/max(tp+fn,1),4))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--compare',type=Path)
    args=parser.parse_args()
    outcomes=evaluate(catalog())
    report=dict(scope='Synthetic fixed catalog; planner/filter safety, not actual Mac catalog accuracy',
                judgments_sha256=hashlib.sha256(json.dumps(CASES,sort_keys=True).encode()).hexdigest(),
                passed=sum(c['passed'] for c in outcomes),total=len(outcomes),
                groups={g:dict(passed=sum(c['passed'] for c in outcomes if c['group']==g),
                               total=sum(c['group']==g for c in outcomes))
                        for g in sorted({c['group'] for c in outcomes})},
                filter_metrics=filter_metrics(outcomes),benchmark_2357=benchmark(catalog(2357)),cases=outcomes)
    if args.compare:
        before=json.loads(args.compare.read_text(encoding='utf-8'))
        assert before['judgments_sha256']==report['judgments_sha256'],'Judgments changed; comparison invalid'
        previous={c['query']:c for c in before['cases']}
        report['comparison']=dict(
            improved=[c['query'] for c in outcomes if c['passed'] and not previous[c['query']]['passed']],
            regressed=[c['query'] for c in outcomes if not c['passed'] and previous[c['query']]['passed']],
            still_failed=[c['query'] for c in outcomes if not c['passed'] and not previous[c['query']]['passed']],
            before_passed=before['passed'],before_filter_metrics=filter_metrics(before['cases']))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2))
    for case in outcomes:
        if not case['passed']:
            print('FAIL',case['query'],case['failures'],'->',case['actual_mode'],case['actual_allowed'],case['actual_kinds'])


if __name__=='__main__': main()
