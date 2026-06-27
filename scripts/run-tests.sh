#!/usr/bin/env bash
# =============================================================================
# Automated Test Suite Runner
# Autonomous Postgres DBA Agent Platform
#
# Executes the full test suite covering:
# - Guardrail enforcement tests
# - Loop execution tests
# - Evidence collection tests
# - Plan generation tests
#
# Exit code: 0 when all tests pass, non-zero on failure.
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_section() { echo -e "\n${BLUE}[SUITE]${NC} $1"; }
log_error() { echo -e "${RED}[FAIL]${NC} $1"; }
log_pass() { echo -e "${GREEN}[PASS]${NC} $1"; }

FAILED=0
TOTAL_SUITES=0

run_test_suite() {
    local suite_name="$1"
    shift
    local test_files=("$@")

    TOTAL_SUITES=$((TOTAL_SUITES + 1))
    log_section "Running: ${suite_name}"

    if python -m pytest "${test_files[@]}" \
        --tb=short \
        --no-header \
        -q \
        2>&1; then
        log_pass "${suite_name} - ALL PASSED"
    else
        log_error "${suite_name} - FAILED"
        FAILED=$((FAILED + 1))
    fi
}

# =============================================================================
# Setup
# =============================================================================
log_info "Starting Autonomous Postgres DBA Agent Platform Test Suite"
log_info "Project root: ${PROJECT_ROOT}"
echo ""

# Activate virtual environment if available
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

# Verify pytest is available
if ! command -v pytest &>/dev/null && ! python -m pytest --version &>/dev/null 2>&1; then
    log_error "pytest is not installed. Run: pip install -e '.[dev]'"
    exit 1
fi

# =============================================================================
# Test Suite 1: Guardrail Enforcement Tests
# =============================================================================
GUARDRAIL_TESTS=()
[ -f tests/test_guardrail_allowlist.py ] && GUARDRAIL_TESTS+=(tests/test_guardrail_allowlist.py)
[ -f tests/test_guardrail_safety.py ] && GUARDRAIL_TESTS+=(tests/test_guardrail_safety.py)
[ -f tests/test_risk_scoring.py ] && GUARDRAIL_TESTS+=(tests/test_risk_scoring.py)

if [ ${#GUARDRAIL_TESTS[@]} -gt 0 ]; then
    run_test_suite "Guardrail Enforcement" "${GUARDRAIL_TESTS[@]}"
else
    log_section "Guardrail Enforcement - No test files found, skipping"
fi

# =============================================================================
# Test Suite 2: Loop Execution Tests
# =============================================================================
LOOP_TESTS=()
[ -f tests/test_loop_worker.py ] && LOOP_TESTS+=(tests/test_loop_worker.py)
[ -f tests/test_runs_api.py ] && LOOP_TESTS+=(tests/test_runs_api.py)
[ -f tests/test_verification.py ] && LOOP_TESTS+=(tests/test_verification.py)

if [ ${#LOOP_TESTS[@]} -gt 0 ]; then
    run_test_suite "Loop Execution" "${LOOP_TESTS[@]}"
else
    log_section "Loop Execution - No test files found, skipping"
fi

# =============================================================================
# Test Suite 3: Evidence Collection Tests
# =============================================================================
EVIDENCE_TESTS=()
[ -f tests/test_evidence_api.py ] && EVIDENCE_TESTS+=(tests/test_evidence_api.py)
[ -f tests/test_evidence_buffer.py ] && EVIDENCE_TESTS+=(tests/test_evidence_buffer.py)
[ -f tests/test_host_agent.py ] && EVIDENCE_TESTS+=(tests/test_host_agent.py)

if [ ${#EVIDENCE_TESTS[@]} -gt 0 ]; then
    run_test_suite "Evidence Collection" "${EVIDENCE_TESTS[@]}"
else
    log_section "Evidence Collection - No test files found, skipping"
fi

# =============================================================================
# Test Suite 4: Plan Generation Tests
# =============================================================================
PLAN_TESTS=()
[ -f tests/test_plans_api.py ] && PLAN_TESTS+=(tests/test_plans_api.py)
[ -f tests/test_ai_planning.py ] && PLAN_TESTS+=(tests/test_ai_planning.py)
[ -f tests/test_reports.py ] && PLAN_TESTS+=(tests/test_reports.py)

if [ ${#PLAN_TESTS[@]} -gt 0 ]; then
    run_test_suite "Plan Generation" "${PLAN_TESTS[@]}"
else
    log_section "Plan Generation - No test files found, skipping"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "============================================"
if [ $FAILED -eq 0 ]; then
    log_pass "All ${TOTAL_SUITES} test suites passed!"
    echo "============================================"
    exit 0
else
    log_error "${FAILED} of ${TOTAL_SUITES} test suites failed."
    echo "============================================"
    exit 1
fi
