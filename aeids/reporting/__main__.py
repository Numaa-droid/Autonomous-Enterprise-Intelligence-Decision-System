"""Print or save a workflow's checked report (M7).

    .venv/bin/python -m aeids.reporting <workflow_id> [--tenant eval] [-o reports/answer.md] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aeids.reporting")
    ap.add_argument("workflow_id", type=uuid.UUID)
    ap.add_argument("--tenant", default=None, help="tenant name (default: the configured default tenant)")
    ap.add_argument("-o", "--out", type=Path, help="write to this file instead of printing")
    ap.add_argument("--json", action="store_true", help="the report content as JSON instead of Markdown")
    args = ap.parse_args(argv)

    from sqlalchemy import select

    from aeids.config import get_settings
    from aeids.db.models import Tenant
    from aeids.db.session import make_engine, make_sessionmaker
    from aeids.documents import DocumentError
    from aeids.orchestration import views
    from aeids.reporting import report

    settings = get_settings()
    name = args.tenant or settings.default_tenant_name
    with make_sessionmaker(make_engine(settings.database_url))() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.name == name))
        if tenant is None:
            print(f"no tenant named {name!r}", file=sys.stderr)
            return 1
        try:
            row = report.ensure(session, views.get_workflow(session, tenant.id, args.workflow_id))
        except DocumentError as e:
            print(e.message, file=sys.stderr)
            return 1
        text = json.dumps(report.view(row), indent=2) if args.json else row.markdown
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"report v{row.version} ({row.status}) written to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
