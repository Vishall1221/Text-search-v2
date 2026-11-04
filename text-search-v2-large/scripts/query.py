"""Read-only CLI smoke check of the same service used by the API."""
import argparse
import json
from app.embedding import load_embedder
from app.service import SearchService
from app.runtime import run_async

async def run(args):
    svc=SearchService(None if args.lexical_only else load_embedder())
    try:
        await svc.start()
        print(json.dumps(await svc.search(args.query,debug=True),indent=2,default=str))
    finally:
        await svc.stop()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--query',required=True)
    p.add_argument('--lexical-only',action='store_true')
    run_async(run(p.parse_args()))
