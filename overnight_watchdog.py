#!/usr/bin/env python3
"""Overnight watchdog: keep polymarket_rewards_app.py + the reward monitor
running, restart on crash, and pop a macOS notification on each restart so
you find out in the morning instead of at the end of an idle 8h.

Tweak APP_CMD / MONITOR_CMD below if your venv path differs.

Run:
    python3 overnight_watchdog.py
"""
import os, signal, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent
PYTHON = sys.executable
APP_CMD     = [PYTHON, str(REPO / "polymarket_rewards_app.py")]
MONITOR_CMD = [PYTHON, str(REPO / "polymarket_reward_monitor.py")]
RESTART_BACKOFF = [2, 5, 15, 60, 300]  # seconds between successive restarts


def notify(title, msg):
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "{title}"'],
                       check=False)
    except Exception:
        pass


def supervise(name, cmd):
    backoff_idx = 0
    while True:
        print(f"[{name}] start: {' '.join(cmd)}")
        try:
            p = subprocess.Popen(cmd)
        except Exception as e:
            print(f"[{name}] launch failed: {e}")
            time.sleep(RESTART_BACKOFF[min(backoff_idx, len(RESTART_BACKOFF)-1)])
            backoff_idx += 1; continue
        rc = p.wait()
        msg = f"exit={rc}"
        notify(f"watchdog: {name}", msg)
        print(f"[{name}] {msg}; backing off "
              f"{RESTART_BACKOFF[min(backoff_idx, len(RESTART_BACKOFF)-1)]}s")
        time.sleep(RESTART_BACKOFF[min(backoff_idx, len(RESTART_BACKOFF)-1)])
        backoff_idx = min(backoff_idx + 1, len(RESTART_BACKOFF) - 1)


def main():
    import threading
    threads = [
        threading.Thread(target=supervise, args=("rewards_app", APP_CMD), daemon=True),
        threading.Thread(target=supervise, args=("monitor", MONITOR_CMD), daemon=True),
    ]
    for t in threads: t.start()
    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt:
        print("watchdog: shutting down (children will be reaped by Popen exit)")


if __name__ == "__main__":
    main()
