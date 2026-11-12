"""Cross-process locks for mutable public archive checkpoints."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time


@contextmanager
def exclusive_file(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a+b") as stream:
        acquired=False
        try:
            if os.name=="nt":
                import msvcrt
                stream.seek(0,2)
                if stream.tell()==0:stream.write(b"\0");stream.flush()
                while not acquired:
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);acquired=True
                    except OSError as error:
                        if error.errno not in {errno.EACCES,errno.EAGAIN,errno.EDEADLK}:raise
                        time.sleep(.2)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX);acquired=True
            yield
        finally:
            if acquired:
                if os.name=="nt":
                    stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
