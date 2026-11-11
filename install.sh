#!/bin/sh
# Install or upgrade comms-graph (and its `comms` alias) for this user.
#
#   sh install.sh                  latest GitHub release
#   sh install.sh --from <dir>     a checkout's python/ directory
#
# Safe to run again: it reuses the venv, reinstalls the package, and relinks.
set -eu

REPO="${COMMS_INSTALL_REPO:-dpa-plus/comms}"
VENV="$HOME/.local/share/comms-graph"
BIN="$HOME/.local/bin"
FROM=""

while [ $# -gt 0 ]; do
  case "$1" in
    --from) [ $# -ge 2 ] || { echo "error: --from needs a directory" >&2; exit 2; }; FROM="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "error: unknown argument $1 (try --help)" >&2; exit 2 ;;
  esac
done

if command -v python3.11 >/dev/null 2>&1; then PY=python3.11; else PY=python3; fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "error: Python 3.11 or newer is required ($PY is older)" >&2; exit 1; }

mkdir -p "$HOME/.local/share" "$BIN"
if [ ! -x "$VENV/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --quiet --python "$PY" "$VENV"
  else
    "$PY" -m venv "$VENV"
  fi
fi

pip_install() {
  if command -v uv >/dev/null 2>&1; then
    uv pip install --quiet --python "$VENV/bin/python" "$@"
  else
    "$VENV/bin/python" -m pip install --quiet "$@"
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -n "$FROM" ]; then
  [ -f "$FROM/pyproject.toml" ] || { echo "error: $FROM has no pyproject.toml" >&2; exit 1; }
  SRC="$(cd "$FROM" && pwd)"
  # Build from a copy so the checkout does not grow build/ and egg-info/.
  mkdir "$TMP/src"
  (cd "$SRC" && tar --exclude .venv --exclude build --exclude dist --exclude '*.egg-info' \
      --exclude __pycache__ -cf - .) | (cd "$TMP/src" && tar -xf -)
  TARGET="$TMP/src"
else
  API="https://api.github.com/repos/$REPO/releases/latest"
  URL="$(curl -fsSL "$API" | "$PY" -c '
import json, sys
for a in json.load(sys.stdin).get("assets", []):
    if a["name"].endswith(".whl"):
        print(a["browser_download_url"]); break')"
  [ -n "$URL" ] || { echo "error: no wheel in the latest release of $REPO" >&2; exit 1; }
  TARGET="$TMP/$(basename "$URL")"
  curl -fsSL -o "$TARGET" "$URL"
fi

# Dependencies first, then the package itself unconditionally: an unchanged
# version number must still replace the files, or a stale install stays stale.
pip_install "$TARGET"
if command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --python "$VENV/bin/python" --reinstall --no-deps "$TARGET"
else
  "$VENV/bin/python" -m pip install --quiet --force-reinstall --no-deps "$TARGET"
fi

link() {
  name="$1"
  [ -e "$VENV/bin/$name" ] || return 0
  if [ -e "$BIN/$name" ] && [ ! -L "$BIN/$name" ]; then
    echo "left $BIN/$name alone: it is a real file, not a link. Move it aside, then rerun." >&2
    return 0
  fi
  ln -sf "$VENV/bin/$name" "$BIN/$name"
}
link comms-graph
link comms
link graphify

OTHER="$(command -v comms 2>/dev/null || true)"
if [ -n "$OTHER" ] && [ "$OTHER" != "$BIN/comms" ]; then
  echo "note: 'comms' on your PATH is $OTHER, which shadows $BIN/comms. Remove it or reorder PATH." >&2
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to your PATH" >&2 ;; esac

"$VENV/bin/python" -m comms_graph.doctor --fix || echo "doctor still lists fixes; run: $BIN/comms-graph doctor" >&2
"$BIN/comms-graph" version
