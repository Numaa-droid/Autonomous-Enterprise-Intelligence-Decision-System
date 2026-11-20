#!/bin/sh
# One command for local work: services, migrations, TableLens, AEIDS API and worker (aeids/start.py).
#   ./start.sh                  everything
#   ./start.sh --no-tablelens   without parsing
#   ./start.sh --help
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "no .venv: uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt" >&2
  exit 1
fi
exec .venv/bin/python -m aeids.start "$@"
