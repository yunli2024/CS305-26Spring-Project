#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/home/w1369/miniforge3/envs/cs305/bin/python}"
OSKEN_MANAGER="${OSKEN_MANAGER:-/home/w1369/miniforge3/envs/cs305/bin/osken-manager}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/test_logs}"

mkdir -p "$LOG_DIR"

CONTROLLER_PID=""
SUDO_KEEPALIVE_PID=""
FAILED=0

cleanup() {
    if [[ -n "$CONTROLLER_PID" ]] && kill -0 "$CONTROLLER_PID" 2>/dev/null; then
        kill "$CONTROLLER_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$CONTROLLER_PID" 2>/dev/null || true
    fi
    if [[ -n "$SUDO_KEEPALIVE_PID" ]] && kill -0 "$SUDO_KEEPALIVE_PID" 2>/dev/null; then
        kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    fi
    sudo -n mn -c >/dev/null 2>&1 || true
}
trap cleanup EXIT

mark_fail() {
    echo "[FAIL] $1"
    FAILED=1
}

mark_pass() {
    echo "[PASS] $1"
}

require_log() {
    local name="$1"
    local pattern="$2"
    local log_file="$3"
    if grep -Eq "$pattern" "$log_file"; then
        return 0
    fi
    echo "  missing pattern: $pattern"
    echo "  log: $log_file"
    return 1
}

require_count_at_least() {
    local name="$1"
    local pattern="$2"
    local expected="$3"
    local log_file="$4"
    local count
    count="$(grep -Ec "$pattern" "$log_file" || true)"
    if (( count >= expected )); then
        return 0
    fi
    echo "  expected at least $expected matches for: $pattern"
    echo "  actual matches: $count"
    echo "  log: $log_file"
    return 1
}

clean_mininet() {
    sudo -n mn -c >/dev/null 2>&1 || true
}

run_python_test() {
    local name="$1"
    local script="$2"
    local stdin_text="${3:-}"
    local log_file="$LOG_DIR/${name}.log"

    echo
    echo "===== $name ====="
    clean_mininet

    if [[ -n "$stdin_text" ]]; then
        printf "%b" "$stdin_text" | sudo -n "$PYTHON_BIN" "$ROOT_DIR/$script" >"$log_file" 2>&1
    else
        sudo -n "$PYTHON_BIN" "$ROOT_DIR/$script" >"$log_file" 2>&1
    fi

    local status=$?
    if (( status != 0 )); then
        tail -n 80 "$log_file"
        mark_fail "$name exited with status $status"
        return 1
    fi

    mark_pass "$name completed"
    return 0
}

echo "Using python: $PYTHON_BIN"
echo "Using osken-manager: $OSKEN_MANAGER"
echo "Logs: $LOG_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python binary not executable: $PYTHON_BIN"
    exit 1
fi
if [[ ! -x "$OSKEN_MANAGER" ]]; then
    echo "osken-manager not executable: $OSKEN_MANAGER"
    exit 1
fi

echo "Requesting sudo once for Mininet..."
sudo -v || exit 1
(
    while true; do
        sudo -n true
        sleep 30
    done
) &
SUDO_KEEPALIVE_PID=$!

cd "$ROOT_DIR" || exit 1

echo
echo "===== py_compile ====="
if "$PYTHON_BIN" -m py_compile controller.py dhcp.py firewall.py dns_server.py; then
    mark_pass "py_compile"
else
    mark_fail "py_compile"
fi

clean_mininet
CONTROLLER_LOG="$LOG_DIR/controller.log"
echo
echo "===== controller ====="
"$OSKEN_MANAGER" --observe-links controller.py >"$CONTROLLER_LOG" 2>&1 &
CONTROLLER_PID=$!
sleep 3
if kill -0 "$CONTROLLER_PID" 2>/dev/null; then
    mark_pass "controller started"
else
    tail -n 80 "$CONTROLLER_LOG"
    echo "Controller failed to start"
    exit 1
fi

run_python_test "switching_basic" "tests/switching_test/test_network.py" $'pingall\nexit\n'
require_log "switching_basic" '0% dropped \(6/6 received\)' "$LOG_DIR/switching_basic.log" || mark_fail "switching_basic assertion"

run_python_test "switching_complex" "tests/switching_test/complex_topology.py"
require_count_at_least "switching_complex" '0% dropped \(12/12 received\)' 2 "$LOG_DIR/switching_complex.log" || mark_fail "switching_complex assertion"

run_python_test "dhcp_basic" "tests/dhcp_test/test_network.py" $'h1 ifconfig h1-eth0\nh2 ifconfig h2-eth0\nh1 ping -c2 -W1 192.168.1.3\nexit\n'
require_log "dhcp_basic h1" 'inet 192\.168\.1\.2' "$LOG_DIR/dhcp_basic.log" || mark_fail "dhcp_basic h1 IP"
require_log "dhcp_basic h2" 'inet 192\.168\.1\.3' "$LOG_DIR/dhcp_basic.log" || mark_fail "dhcp_basic h2 IP"
require_log "dhcp_basic ping" '0% packet loss' "$LOG_DIR/dhcp_basic.log" || mark_fail "dhcp_basic ping"

run_python_test "firewall_basic" "tests/firewall_test/test_network.py" $'exit\n'
require_log "firewall_basic blocked_icmp" '100% packet loss' "$LOG_DIR/firewall_basic.log" || mark_fail "firewall_basic blocked ICMP"
require_log "firewall_basic allowed_icmp" '0% packet loss' "$LOG_DIR/firewall_basic.log" || mark_fail "firewall_basic allowed ICMP"
require_log "firewall_basic blocked_tcp80" 'HTTP_CODE=000' "$LOG_DIR/firewall_basic.log" || mark_fail "firewall_basic blocked TCP/80"
require_log "firewall_basic allowed_tcp8080" 'HTTP_CODE=200' "$LOG_DIR/firewall_basic.log" || mark_fail "firewall_basic allowed TCP/8080"

run_python_test "dns_bonus" "tests/dns_test/test_network.py"
require_log "dns_bonus" 'Summary: 4/4 checks passed' "$LOG_DIR/dns_bonus.log" || mark_fail "dns_bonus assertion"

run_python_test "firewall_complex" "tests/firewall_test/firewall_complex_test.py"
require_log "firewall_complex" 'Summary: 7/7 checks passed' "$LOG_DIR/firewall_complex.log" || mark_fail "firewall_complex assertion"

run_python_test "dhcp_offer_timeout" "tests/dhcp_test/test_offer_timeout.py"
require_log "dhcp_offer_timeout" '\[Test3\] OFFER Timeout Reclaim: \[PASSED\]' "$LOG_DIR/dhcp_offer_timeout.log" || mark_fail "dhcp_offer_timeout assertion"

run_python_test "dhcp_concurrent_exhaust" "tests/dhcp_test/test_concurrent_exhaust.py"
require_log "dhcp_concurrent_exhaust test4" '\[Test4\] Concurrent Multi-Client Allocation: \[PASSED\]' "$LOG_DIR/dhcp_concurrent_exhaust.log" || mark_fail "dhcp_concurrent_exhaust test4"
require_log "dhcp_concurrent_exhaust test5" '\[Test5\] Pool Exhaustion No Reply:[[:space:]]+\[PASSED\]' "$LOG_DIR/dhcp_concurrent_exhaust.log" || mark_fail "dhcp_concurrent_exhaust test5"

run_python_test "dhcp_lease_expiry" "tests/dhcp_test/test_lease_expiry.py"
require_log "dhcp_lease_expiry test1" 'Test 1 \(Lease Expiry\): \[PASSED\]' "$LOG_DIR/dhcp_lease_expiry.log" || mark_fail "dhcp_lease_expiry test1"
require_log "dhcp_lease_expiry test2" 'Test 2 \(Lease Renewal\): \[PASSED\]' "$LOG_DIR/dhcp_lease_expiry.log" || mark_fail "dhcp_lease_expiry test2"

echo
echo "===== summary ====="
if (( FAILED == 0 )); then
    echo "ALL TESTS PASSED"
    exit 0
fi

echo "SOME TESTS FAILED"
echo "Check logs in: $LOG_DIR"
exit 1
