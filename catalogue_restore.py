"""Restore the canonical public catalogue without falling back from corruption."""
import argparse
from urllib.error import HTTPError
from urllib.request import Request,urlopen
import catalogue_transport as codec

BASE="https://huggingface.co/buckets/vn-archive/mirrors/resolve"


def restore(destination,*,base=BASE,opener=None,max_size=codec.MAX_SIZE,reserve=codec.RESERVE):
    opener=opener or urlopen
    try:
        response=opener(Request(base.rstrip("/")+"/index.sqlite3.gz",headers={"Accept-Encoding":"identity"}),timeout=60)
    except HTTPError as error:
        if error.code!=404:
            error.close();raise
        error.close()
    else:
        with response:
            proof=codec.restore(response,destination,max_size=max_size,reserve=reserve)
        return dict(proof,transport="gzip")
    # Only the absence of gzip permits bootstrap from raw. Connection errors,
    # 403/429/5xx and malformed/truncated gzip must preserve the deployed site.
    with opener(Request(base.rstrip("/")+"/index.sqlite3",headers={"Accept-Encoding":"identity"}),timeout=60) as response:
        proof=codec.restore(response,destination,max_size=max_size,reserve=reserve,compressed=False)
    return dict(proof,transport="raw")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",default="archive/index.sqlite3")
    args=parser.parse_args()
    print(restore(args.output),flush=True)
