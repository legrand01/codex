#!/usr/bin/env bash
# =============================================================================
# Local Development Setup Script
# Autonomous Postgres DBA Agent Platform
#
# This script installs dependencies, starts services, and confirms readiness.
# The application must respond to HTTP requests within 60 seconds.
# =============================================================================

set -euo pipefail

# Configuration
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
MAX_WAIT_SECONDS=60
HEALTH_ENDPOINT="http://localhost:${PORT}/health"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Cleanup function for graceful shutdown
cleanup() {
    log_info "Cleaning up background processes..."
    if [ -n "${BACKEND_PID:-}" ]; then
        kill "$BACKEND_PID" 2>/dev/null || true
    fi
    if [ -n "${FRONTEND_PID:-}" ]; then
        kill "$FRONTEND_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

cd "$PROJECT_ROOT"

# =============================================================================
# Step 1: Check prerequisites
# =============================================================================
log_info "Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    log_error "Python 3 is not installed. Please install Python 3.9+."
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
log_info "Python version: ${PYTHON_VERSION}"

if ! command -v pip &>/dev/null && ! command -v pip3 &>/dev/null; then
    log_error "pip is not installed. Please install pip."
    exit 1
fi

# =============================================================================
# Step 2: Create environment file if not exists
# =============================================================================
if [ ! -f .env ]; then
    log_info "Creating .env from .env.example..."
    cp .env.example .env
else
    log_info ".env file already exists, skipping."
fi

# =============================================================================
# Step 3: Install Python dependencies
# =============================================================================
log_info "Installing Python dependencies..."

if [ -d "venv" ]; then
    log_info "Virtual environment already exists."
else
    python3 -m venv venv
    log_info "Created virtual environment."
fi

source venv/bin/activate
pip install -e ".[dev]" --quiet 2>/dev/null || pip install -e "." --quiet
log_info "Python dependencies installed."

# =============================================================================
# Step 4: Install frontend dependencies (if Node.js available)
# =============================================================================
if command -v node &>/dev/null; then
    log_info "Installing frontend dependencies..."
    cd frontend
    if [ -f package-lock.json ]; then
        npm ci --silent 2>/dev/null || npm install --silent
    else
        npm install --silent
    fi
    cd "$PROJECT_ROOT"
    log_info "Frontend dependencies installed."
else
    log_warn "Node.js not found. Skipping frontend setup."
fi

# =============================================================================
# Step 5: Start infrastructure services (Docker-based)
# =============================================================================
if command -v docker &>/dev/null && command -v docker-compose &>/dev/null || command -v docker compose &>/dev/null; then
    log_info "Starting PostgreSQL and Redis via Docker..."
    docker compose up -d postgres redis 2>/dev/null || docker-compose up -d postgres redis 2>/dev/null || true
    sleep 3
else
    log_warn "Docker not available. Ensure PostgreSQL and Redis are running locally."
fi

# =============================================================================
# Step 6: Start backend with hot-reload
# =============================================================================
log_info "Starting backend with hot-reload on port ${PORT}..."
uvicorn backend.main:app --host "$HOST" --port "$PORT" --reload &
BACKEND_PID=$!
log_info "Backend started (PID: ${BACKEND_PID})"

# =============================================================================
# Step 7: Start frontend dev server (if Node.js available)
# =============================================================================
if command -v node &>/dev/null && [ -d "frontend/node_modules" ]; then
    log_info "Starting frontend dev server on port ${FRONTEND_PORT}..."
    cd frontend
    npm run dev -- --port "$FRONTEND_PORT" &
    FRONTEND_PID=$!
    cd "$PROJECT_ROOT"
    log_info "Frontend started (PID: ${FRONTEND_PID})"
fi

# =============================================================================
# Step 8: Wait for HTTP readiness (within 60 seconds)
# =============================================================================
log_info "Waiting for application to be ready (max ${MAX_WAIT_SECONDS}s)..."

ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT_SECONDS ]; do
    if curl -sf "$HEALTH_ENDPOINT" > /dev/null 2>&1; then
        log_info "Application is ready! Health check passed at ${HEALTH_ENDPOINT}"
        echo ""
        log_info "==================================="
        log_info " Development environment is ready!"
        log_info "==================================="
        log_info "Backend API:  http://localhost:${PORT}"
        log_info "API Docs:     http://localhost:${PORT}/docs"
        log_info "Health:       http://localhost:${PORT}/health"
        if [ -n "${FRONTEND_PID:-}" ]; then
            log_info "Frontend:     http://localhost:${FRONTEND_PORT}"
        fi
        echo ""
        log_info "Press Ctrl+C to stop all services."
        wait
        exit 0
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

# Startup failure
log_error "Application failed to respond within ${MAX_WAIT_SECONDS} seconds."
log_error "Component failure: backend service did not pass health check."
exit 1
