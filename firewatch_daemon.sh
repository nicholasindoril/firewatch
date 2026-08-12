#!/data/data/com.termux/files/usr/bin/bash
# Firewatch daemon — periodically scans for wildfires and writes status
# for Automagic to read. Run this in a Termux session or via termux-job-scheduler.
#
# Usage:
#   bash firewatch_daemon.sh            # run every 15 min
#   bash firewatch_daemon.sh --once     # single scan, exit
#   bash firewatch_daemon.sh --demo     # demo mode every 5 min (for testing)
#
# Automagic control files:
#   trigger.txt   — created by flow to force an immediate scan
#   disabled.txt  — created by flow to pause scanning

set -e
BRIDGE="/data/data/com.termux/files/home/pyscripts/firewatch/firewatch_bridge.py"
TRIGGER="/storage/emulated/0/AutoLogs/firewatch/trigger.txt"
DISABLED="/storage/emulated/0/AutoLogs/firewatch/disabled.txt"
STATUS="/storage/emulated/0/AutoLogs/firewatch/status.txt"
INTERVAL=900  # 15 minutes
DEMO_INTERVAL=300  # 5 minutes for demo

mkdir -p "$(dirname "$TRIGGER")"

run_scan() {
    local mode="$1"
    if [ -f "$DISABLED" ]; then
        echo "[$(date '+%H:%M:%S')] Paused — disabled flag is set"
        return
    fi
    echo "[$(date '+%H:%M:%S')] Scanning..."
    if [ "$mode" = "demo" ]; then
        python "$BRIDGE" --demo
    else
        python "$BRIDGE"
    fi
}

if [ "$1" = "--once" ]; then
    run_scan
    exit 0
fi

if [ "$1" = "--demo" ]; then
    echo "Demo mode — scanning every ${DEMO_INTERVAL}s"
    echo "Control files: $TRIGGER | $DISABLED"
    while true; do
        if [ -f "$DISABLED" ]; then
            echo "[$(date '+%H:%M:%S')] Paused"
        else
            run_scan "demo"
        fi
        # Check for force-scan trigger from Automagic every 15s
        for i in $(seq 1 20); do
            if [ -f "$TRIGGER" ]; then
                echo "[$(date '+%H:%M:%S')] Force scan triggered"
                rm -f "$TRIGGER"
                run_scan "demo"
                break
            fi
            sleep 15
        done
    done
fi

echo "Firewatch daemon started — scanning every ${INTERVAL}s"
echo "Trigger: $TRIGGER | Disabled: $DISABLED"

while true; do
    run_scan

    elapsed=0
    while [ $elapsed -lt $INTERVAL ]; do
        if [ -f "$TRIGGER" ]; then
            echo "[$(date '+%H:%M:%S')] Force scan triggered by Automagic"
            rm -f "$TRIGGER"
            run_scan
            elapsed=0
        fi
        sleep 30
        elapsed=$((elapsed + 30))
    done
done
