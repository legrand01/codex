#!/usr/bin/env bash
# =============================================================================
# Deployment Script
# Autonomous Postgres DBA Agent Platform
#
# Makes the platform accessible via web browser at configurable host/port.
# Returns valid HTTP response within 30 seconds of script completion.
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Configuration (override via environment variables)
DEPLOY_HOST="${HOST:-0.0.0.0}"
DEPLOY_PORT="${PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-80}"
MAX_WAIT_SECONDS=30

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[DEPLOY]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[DEPLOY]${NC} $1"; }
log_error() { echo -e "${RED}[DEPLOY]${NC} $1"; }

# =============================================================================
# Step 1: Validate environment
# =============================================================================
log_info "Deploying Autonomous Postgres DBA Agent Platform"
log_info "Backend:  ${DEPLOY_HOST}:${DEPLOY_PORT}"
log_info "Frontend: ${DEPLOY_HOST}:${FRONTEND_PORT}"
echo ""

# Check for .env file
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        log_info "Creating .env from .env.example..."
        cp .env.example .env
        # Update settings for deployment
        sed -i "s/^HOST=.*/HOST=${DEPLOY_HOST}/" .env 2>/dev/null || true
        sed -i "s/^PORT=.*/PORT=${DEPLOY_PORT}/" .env 2>/dev/null || true
        sed -i "s/^DEBUG=.*/DEBUG=false/" .env 2>/dev/null || true
    else
        log_error ".env file not found and no .env.example to copy from."
        exit 1
    fi
fi

# =============================================================================
# Step 2: Build and start services with Docker Compose
# =============================================================================
if command -v docker &>/dev/null; then
    COMPOSE_CMD=""
    if docker compose version &>/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD="docker-compose"
    fi

    if [ -n "$COMPOSE_CMD" ]; then
        log_info "Building Docker images..."
        if ! $COMPOSE_CMD build --quiet 2>/dev/null; then
            log_error "Docker build failed. Component failure: container build."
            exit 1
        fi

        log_info "Starting all services..."
        if ! $COMPOSE_CMD up -d 2>/dev/null; then
            log_error "Failed to start services. Component failure: docker-compose up."
            exit 1
        fi

        log_info "Services started. Waiting for health checks..."

        # Wait for services to be healthy
        ELAPSED=0
        while [ $ELAPSED -lt $MAX_WAIT_SECONDS ]; do
            if curl -sf "http://localhost:${DEPLOY_PORT}/health/ready" > /dev/null 2>&1; then
                log_info "Backend is healthy!"
                echo ""
                log_info "============================================"
                log_info " Deployment successful!"
                log_info "============================================"
                log_info "Backend API:     http://localhost:${DEPLOY_PORT}"
                log_info "API Docs:        http://localhost:${DEPLOY_PORT}/docs"
                log_info "Frontend:        http://localhost:${FRONTEND_PORT}"
                log_info "Readiness Check: http://localhost:${DEPLOY_PORT}/health/ready"
                echo ""
                log_info "To stop: $COMPOSE_CMD down"
                log_info "To view logs: $COMPOSE_CMD logs -f"
                exit 0
            fi
            sleep 2
            ELAPSED=$((ELAPSED + 2))
        done

        log_error "Services did not become healthy within ${MAX_WAIT_SECONDS} seconds."
        log_error "Component failure: application health check timed out."
        $COMPOSE_CMD logs --tail=20 app 2>/dev/null || true
        exit 1
    fi
fi

# =============================================================================
# Fallback: Direct deployment without Docker
# =============================================================================
log_warn "Docker not available. Falling back to direct deployment..."

# Activate virtual environment if available
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -d "venv" ]; then
    source venv/bin/activate
else
    log_info "Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -e "." --quiet
fi

# Verify dependencies
if ! python -c "import fastapi" 2>/dev/null; then
    log_info "Installing dependencies..."
    pip install -e "." --quiet
fi

# Start the application
log_info "Starting application server..."
export HOST="$DEPLOY_HOST"
export PORT="$DEPLOY_PORT"

uvicorn backend.main:app \
    --host "$DEPLOY_HOST" \
    --port "$DEPLOY_PORT" \
    --workers 2 \
    --access-log &
APP_PID=$!

# Wait for HTTP readiness
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT_SECONDS ]; do
    if curl -sf "http://localhost:${DEPLOY_PORT}/health/ready" > /dev/null 2>&1; then
        log_info "Application is ready!"
        echo ""
        log_info "============================================"
        log_info " Deployment successful!"
        log_info "============================================"
        log_info "Backend API:     http://localhost:${DEPLOY_PORT}"
        log_info "API Docs:        http://localhost:${DEPLOY_PORT}/docs"
        log_info "Readiness Check: http://localhost:${DEPLOY_PORT}/health/ready"
        echo ""
        log_info "Application PID: ${APP_PID}"
        log_info "To stop: kill ${APP_PID}"
        exit 0
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

# Startup failure
log_error "Application failed to respond within ${MAX_WAIT_SECONDS} seconds."
log_error "Component failure: backend service did not pass health check."
kill "$APP_PID" 2>/dev/null || true
exit 1
