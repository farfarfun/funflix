#!/usr/bin/env bash
set -euo pipefail

usage() { echo "用法: $0 <dev|prod> <start|stop|restart|run|status>"; exit 2; }
[ "$#" -eq 2 ] || usage
env_name=$1
action=$2
case "$env_name" in
  dev) command=(uv run funflix worker) ;;
  prod) command=(funflix worker) ;;
  *) usage ;;
esac

root=$(cd "$(dirname "$0")/.." && pwd)
run_dir="$root/.run"
pid_file="$run_dir/funflix-worker.pid"
log_file="$run_dir/funflix-worker.log"
lock_dir="$run_dir/funflix-worker.lock"
is_running() { [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; }

case "$action" in
  start)
    is_running && { echo "worker 已运行（PID $(cat "$pid_file")）"; exit 0; }
    mkdir -p "$run_dir"
    mkdir "$lock_dir" 2>/dev/null || { echo "worker 启动锁已存在" >&2; exit 1; }
    trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT
    cd "$root"
    nohup "${command[@]}" >>"$log_file" 2>&1 &
    echo $! >"$pid_file"
    echo "worker 已启动（PID $(cat "$pid_file")）"
    ;;
  stop)
    if ! is_running; then rm -f "$pid_file"; echo "worker 未运行"; exit 0; fi
    kill "$(cat "$pid_file")"
    rm -f "$pid_file"
    echo "worker 已停止"
    ;;
  restart) "$0" "$env_name" stop; exec "$0" "$env_name" start ;;
  run) mkdir -p "$run_dir"; cd "$root"; exec "${command[@]}" ;;
  status)
    if is_running; then echo "worker 正在运行（PID $(cat "$pid_file")）"; else echo "worker 未运行"; exit 1; fi
    ;;
  *) usage ;;
esac
