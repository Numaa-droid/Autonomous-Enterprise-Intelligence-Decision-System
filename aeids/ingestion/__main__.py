"""Command line for ingestion.

    python -m aeids.ingestion inspect <name>_structure.json [--table p32-t1]
        Normalise and chunk a TableLens output without a database; print what AEIDS sees.
    python -m aeids.ingestion attach <document_id> <version_id> <name>_structure.json
        Parse a stored document version from its TableLens output (AEIDS_DATABASE_URL).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections import Counter
from pathlib import Path

from aeids.config import get_settings
from aeids.db.session import make_engine, make_sessionmaker
from aeids.documents import DocumentError, get_or_create_tenant
from aeids.ingestion.chunking import build_chunks
from aeids.ingestion.normalize import NormTable
from aeids.ingestion.pipeline import ingest_structure
from aeids.ingestion.structure import parse_structure
from aeids.storage import LocalStore


def _print_table(t: NormTable) -> None:
    print(f"\n{t.key} pages {t.page_start}-{t.page_end} · {t.title or '(no title)'}")
    print(f"  unit caption: {t.unit_caption!r}")
    print(f"  periods: {t.column_periods[1:]}")
    for w in t.warnings:
        print(f"  warning: {w}")
    for r in t.rows:
        first = r.cells[0] if r.cells else None
        typed = (f"{first.value} {first.unit or '?'} {first.currency or ''} {first.scale or '?'} "
                 f"({first.scale_source or '-'}) {first.period or '-'}") if first and first.value is not None else ""
        print(f"  r{r.row:<3} {r.row_type:<7} {r.label[:55]:<55} {typed}")


def inspect(path: Path, table: str | None) -> int:
    try:
        result = build_chunks(parse_structure(path.read_bytes()))
    except DocumentError as exc:
        print(f"error ({exc.code}): {exc.message}", file=sys.stderr)
        return 1
    kinds = Counter(c.kind for c in result.chunks)
    print(f"{path.name}: {len(result.chunks)} chunks {dict(kinds)} · {len(result.tables)} tables "
          f"({sum(t.is_financial for t in result.tables)} financial)")
    for w in result.warnings:
        print(f"warning: {w}")
    for t in result.tables:
        if table is None and t.is_financial or table == t.key:
            _print_table(t)
            if table is None:
                continue
    return 0


def attach(document_id: str, version_id: str, path: Path) -> int:
    settings = get_settings()
    sessions = make_sessionmaker(make_engine(settings.database_url))
    with sessions() as session:
        tenant = get_or_create_tenant(session, settings.default_tenant_name)
        session.commit()
        try:
            result = ingest_structure(session, LocalStore(settings.storage_root), tenant.id,
                                      uuid.UUID(document_id), uuid.UUID(version_id),
                                      path.read_bytes())
        except DocumentError as exc:
            print(f"error ({exc.code}): {exc.message}", file=sys.stderr)
            return 1
    print(f"parsed: {result.chunk_count} chunks, {result.table_count} tables "
          f"({result.financial_table_count} financial); status {result.version.parser_status}")
    for w in result.warnings:
        print(f"warning: {w}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aeids.ingestion")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("inspect", help="normalise + chunk a _structure.json, no database")
    i.add_argument("structure", type=Path)
    i.add_argument("--table", help="print one table by key, e.g. p32-t1")
    a = sub.add_parser("attach", help="parse a stored version from its _structure.json")
    a.add_argument("document_id")
    a.add_argument("version_id")
    a.add_argument("structure", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "inspect":
        return inspect(args.structure, args.table)
    return attach(args.document_id, args.version_id, args.structure)


if __name__ == "__main__":
    sys.exit(main())
