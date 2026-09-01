#!/bin/bash
# weekly_run.sh — 本地跑 weekly-ai-report 全管线，并把本周产出部署到 GitHub Pages
# 触发: Windows Task Scheduler "WeeklyAIReportLocal" 每周一 08:00（可手动执行）
set -uo pipefail
cd /home/loanne/weekly-ai-report || exit 1
mkdir -p logs
TS=$(date +%Y%m%d-%H%M%S)
LOG="logs/local_run_${TS}.log"
export HF_HUB_OFFLINE=1   # 聚类用本地 fastembed 模型，禁止访问 HF

echo "=== [$(date '+%F %T')] 本地周报管线启动，日志: $LOG ===" | tee -a "$LOG"

# ---- 1. 跑全管线 ----
venv/bin/python main.py >> "$LOG" 2>&1
RC=$?
if [ $RC -ne 0 ]; then
  echo "=== 管线失败 exit=$RC，不推送。详见 $LOG ===" | tee -a "$LOG"
  exit $RC
fi

# ---- 2. 计算本周标签（与 main.py 一致: UTC isocalendar）----
WEEK=$(venv/bin/python -c "from datetime import datetime,timezone; y,w,_=datetime.now(timezone.utc).isocalendar(); print(f'{y}-W{w:02d}')" 2>>"$LOG")
if [ -z "$WEEK" ] || [ ! -d "output/$WEEK" ]; then
  echo "=== 未找到 output/$WEEK，跳过部署 ===" | tee -a "$LOG"
  exit 0
fi

# ---- 3. 部署: push output/ 触发 GitHub Actions -> Pages ----
git add -f output/
if git diff --cached --quiet; then
  echo "=== 本周产出无变化，无需部署 ===" | tee -a "$LOG"
  exit 0
fi
git commit -m "deploy $WEEK to Pages (local run)" >> "$LOG" 2>&1
if ! git push origin master >> "$LOG" 2>&1; then
  echo "=== 推送失败（网络/认证），产出已生成在 output/$WEEK，见 $LOG ===" | tee -a "$LOG"
  exit 1
fi
echo "=== 完成: $WEEK 已推送，GitHub Pages 将自动部署（约1分钟）===" | tee -a "$LOG"