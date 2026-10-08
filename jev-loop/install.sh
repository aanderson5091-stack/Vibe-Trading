#!/usr/bin/env bash
# jev-loop local installer (macOS / Linux).
#
# Copies this skill into ~/.claude/skills/jev-loop/ so Claude Code can load it,
# then creates the venv and runs the test suite. Paper-trading by default;
# going live is a separate, deliberate three-gate opt-in documented in SKILL.md.
#
# Usage:  bash install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/skills/jev-loop"

echo "jev-loop installer"
echo "  source: $SRC"
echo "  dest:   $DEST"

if ! command -v uv >/dev/null 2>&1; then
  echo "note: 'uv' not found. Install it first:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "      then re-run this script."
fi

mkdir -p "$HOME/.claude/skills"

if [ -e "$DEST" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  BAK="$HOME/.claude/skills/.jev-loop.bak.$STAMP"
  mv "$DEST" "$BAK"
  echo "previous install backed up to $BAK"
fi

mkdir -p "$DEST"
# Copy everything except local-only artifacts.
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '*.egg-info' --exclude '.env' --exclude 'install.sh' \
    --exclude 'install.ps1' \
    "$SRC"/ "$DEST"/
else
  cp -R "$SRC"/. "$DEST"/
  rm -rf "$DEST/.venv" "$DEST"/**/__pycache__ "$DEST"/*.egg-info "$DEST/.env" \
         "$DEST/install.sh" "$DEST/install.ps1" 2>/dev/null || true
fi

echo "copied skill -> $DEST"

if command -v uv >/dev/null 2>&1; then
  ( cd "$DEST" && uv venv >/dev/null 2>&1 && uv pip install -q -e '.[dev]' && uv run python -m pytest -q )
  echo
  echo "✓ installed and tests pass."
else
  echo "skill files in place; install 'uv' then run:  cd \"$DEST\" && uv venv && uv pip install -e '.[dev]' && uv run python -m pytest -q"
fi

cat <<EOF

Next:
  1. cp "$DEST/.env.example" "$DEST/.env"   and add your Alpaca *paper* keys
     (optionally a Jev key; with none, the loop uses a clearly-labelled mock).
  2. cd "$DEST" && set -a; source .env; set +a
  3. uv run python -m jevloop run --paper --symbol BTC/USD --ticks 30
  4. uv run python -m jevloop serve     # live dashboard

Paper by default. Read SKILL.md before considering live trading.
EOF
