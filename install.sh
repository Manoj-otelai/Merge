#!/usr/bin/env bash
# MergeGuard one-command installer
set -euo pipefail

REQUIRED_PYTHON="3.11"
VENV_DIR=".venv"

info() { echo "  ▶ $*"; }
ok()   { echo "  ✓ $*"; }
err()  { echo "  ✗ $*" >&2; exit 1; }

echo ""
echo "  🛡️  MergeGuard — Cross-MR Semantic Collision Sentinel"
echo "  ─────────────────────────────────────────────────────"
echo ""

# ── Python version check ─────────────────────────────────────────────────────
info "Checking Python version..."
PYTHON=$(command -v python3 || command -v python || err "Python 3.11+ required")
PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
  ok "Python $PY_VERSION"
else
  err "Python 3.11+ required (found $PY_VERSION)"
fi

# ── Virtual environment ──────────────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating virtual environment..."
  "$PYTHON" -m venv "$VENV_DIR"
  ok "Virtual environment created at $VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
info "Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
ok "Dependencies installed"

# ── Environment configuration ────────────────────────────────────────────────
if [[ ! -f ".env" ]]; then
  info "Creating .env from template..."
  cat > .env << 'EOF'
# Required
GITLAB_URL=https://gitlab.com
GITLAB_TOKEN=your-personal-access-token-here
GITLAB_WEBHOOK_SECRET=generate-a-random-secret-here

# Orbit configuration
# Set ORBIT_USE_LOCAL=true to use glab orbit local (DuckDB) instead of Remote
ORBIT_USE_LOCAL=false

# MergeGuard configuration
MERGEGUARD_DB=mergeguard.db
MERGEGUARD_WEBHOOK_URL=http://localhost:8080
MERGEGUARD_API_TOKEN=generate-an-api-token-here
EOF
  ok ".env created — edit it with your GitLab credentials before starting"
fi

# ── AI Catalog skill registration ────────────────────────────────────────────
if command -v glab &>/dev/null; then
  info "Registering MergeGuard skill with glab..."
  glab skills install . 2>/dev/null && ok "Skill registered" || info "Skill registration skipped (requires GitLab Duo)"
fi

echo ""
echo "  ─────────────────────────────────────────────────────"
echo "  Installation complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit .env with your GITLAB_TOKEN and GITLAB_WEBHOOK_SECRET"
echo "  2. Start MergeGuard:"
echo "       source .venv/bin/activate && uvicorn src.main:app --port 8080"
echo "     or:"
echo "       docker-compose up -d"
echo "  3. Configure a GitLab group webhook:"
echo "       URL: \$MERGEGUARD_WEBHOOK_URL/webhook/mr"
echo "       Secret: \$GITLAB_WEBHOOK_SECRET"
echo "       Trigger: Merge request events"
echo "  4. Open the collision map: http://localhost:8080"
echo "  ─────────────────────────────────────────────────────"
echo ""
