#!/bin/bash
set -euo pipefail
umask 077
BENTO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$BENTO_ROOT"
if [[ "$(uname -s)" != Darwin ]]; then
  echo 'Run this installer on macOS (a work Mac or EC2 Mac).' >&2; exit 1
fi
if [[ "$EUID" == 0 ]]; then
  echo 'Run as the dedicated macOS build user, not root.' >&2; exit 1
fi
BENTO_PYTHON="${BENTO_PYTHON:-}"
if [[ -z "$BENTO_PYTHON" ]]; then
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
    if [[ -x "$candidate" ]] && "$candidate" -c 'import sys;sys.exit(sys.version_info < (3,10))'; then BENTO_PYTHON="$candidate"; break; fi
  done
fi
if [[ -z "$BENTO_PYTHON" ]]; then
  if command -v brew >/dev/null; then
    HOMEBREW_NO_AUTO_UPDATE=1 brew install python@3.12
    BENTO_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
  else
    echo 'Python 3.10+ is required. Install approved Python, then run BENTO_PYTHON=/path/to/python3 bash setup.sh.' >&2; exit 1
  fi
fi
if [[ ! -x .venv/bin/python ]]; then "$BENTO_PYTHON" -m venv .venv; fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
chmod +x bento
mkdir -p data runtime generated tools
./bento setup "$@"
