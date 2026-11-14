"""Consistent, disposable SQLite read model for public Pages generation."""
from contextlib import closing
from pathlib import Path
import shutil
from itertools import zip_longest
import sqlite3
import tempfile

import archiver as a
import catalogue_transport as codec

TABLES=('manufacturers','manufacturer_aliases','products','items','item_catalog','catalog_classification_state')
MARKER='mirror_pages_snapshot'


def validate_connection(db):
    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables!=set(TABLES)|{MARKER}:raise ValueError('unexpected Pages snapshot tables')
    rows=db.execute('SELECT format_version,purpose,item_count FROM '+MARKER).fetchall()
    if len(rows)!=1 or rows[0][0]!=1 or rows[0][1]!='public-pages':raise ValueError('invalid Pages snapshot purpose')
    if rows[0][2]!=db.execute('SELECT count(*) FROM items').fetchone()[0]:raise ValueError('Pages snapshot item count mismatch')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:raise ValueError('broken Pages snapshot relations')
    if db.execute('SELECT 1 FROM items i LEFT JOIN item_catalog c ON c.item_id=i.id WHERE c.item_id IS NULL LIMIT 1').fetchone():
        raise ValueError('Pages snapshot is missing catalogue associations')


def _copy_table(db,name):
    db.execute('INSERT INTO main."'+name+'" SELECT * FROM source."'+name+'"')



def _verify_tables(db):
    # Stream both sides under the same source read transaction. Do not build
    # the full public JSON (or retain all source records) on the collection host.
    for name in TABLES:
        source=db.execute('SELECT * FROM source."'+name+'" ORDER BY 1')
        target=db.execute('SELECT * FROM main."'+name+'" ORDER BY 1')
        try:
            if any(left!=right for left,right in zip_longest(source,target,fillvalue=object())):
                raise ValueError('Pages snapshot source parity failed')
        finally:source.close();target.close()


def export(store,destination,*,reserve=codec.RESERVE):
    """Copy only read-model tables in one source transaction, retaining schemas."""
    if store.db.in_transaction:raise ValueError('Pages export requires committed source metadata')
    if reserve<0:raise ValueError('invalid Pages export reserve')
    destination=codec._destination(destination);source=(store.root/'index.sqlite3').resolve()
    if destination.resolve()==source:raise ValueError('Pages export cannot replace the SSOT')
    estimate=store.db.execute('PRAGMA page_count').fetchone()[0]*store.db.execute('PRAGMA page_size').fetchone()[0]
    if shutil.disk_usage(destination.parent).free<2*estimate+reserve:raise OSError('disk reserve reached before Pages export')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(prefix='.pages-',dir=destination.parent,delete=False) as stream:temporary=Path(stream.name)
        with closing(sqlite3.connect(temporary,uri=True,timeout=30)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('ATTACH DATABASE ? AS source',(source.as_uri()+'?mode=ro',));db.execute('BEGIN')
            if db.execute("SELECT 1 FROM source.sqlite_master WHERE type='table' AND name=?",(MARKER,)).fetchone():
                raise ValueError('Pages derivative cannot be an authoritative source')
            schemas={name:db.execute("SELECT sql FROM source.sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() for name in TABLES}
            if any(row is None for row in schemas.values()):raise ValueError('missing Pages source table')
            for name in TABLES:db.execute(schemas[name][0])
            for name in TABLES:_copy_table(db,name)
            _verify_tables(db)
            db.execute('CREATE TABLE '+MARKER+"(id INTEGER PRIMARY KEY CHECK(id=1),format_version INTEGER NOT NULL,purpose TEXT NOT NULL,created_at TEXT NOT NULL,item_count INTEGER NOT NULL)")
            count=db.execute('SELECT count(*) FROM items').fetchone()[0]
            db.execute('INSERT INTO '+MARKER+' VALUES(1,1,?,?,?)',('public-pages',a.now(),count))
            validate_connection(db);db.commit()
        codec.validate(temporary,pages=True)
        temporary.replace(destination)
        return dict(item_count=count,size=destination.stat().st_size)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
