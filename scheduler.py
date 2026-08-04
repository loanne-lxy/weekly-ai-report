#!/usr/bin/env python3
"""定时调度入口 — 配合 cron / systemd timer 使用

使用方式:
  # 每周一 8:00 运行（添加到 crontab）:
  # 0 8 * * 1 cd ~/weekly-ai-report && source venv/bin/activate && python scheduler.py

  # 或直接手动运行:
  # python scheduler.py
"""
import os
import subprocess
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scheduler.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("scheduler")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def run_weekly():
    logger.info("Starting weekly report generation...")
    result = subprocess.run(
        [sys.executable, "-u", "main.py"],
        capture_output=True,
        text=True,
        cwd=PROJECT_DIR,
        timeout=3600,
    )
    if result.returncode == 0:
        logger.info("Weekly report generated successfully")
        logger.info(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)
    else:
        logger.error(f"Weekly report FAILED:\n{result.stderr}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(run_weekly())
