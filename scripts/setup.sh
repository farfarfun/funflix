#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "用法: $0 <start|stop|restart|run> <dev|prod>"
  echo "      $0 status                       # 不区分环境，报告 worker 当前状态"
  exit 2
}

root=$(cd "$(dirname "$0")/.." && pwd)
run_dir="$root/.run"
pid_file="$run_dir/funflix-worker.pid"
log_file="$run_dir/funflix-worker.log"
lock_dir="$run_dir/funflix-worker.lock"

# 判断 PID 是否真的是本脚本启动的 funflix worker，而不只是"这个号码当前有进程"——
# 进程退出后 PID 可能被系统回收复用给完全无关的进程，单纯 kill -0 会把它误判成
# 服务仍在运行。/proc 不可用（非 Linux）时退化为只看存活性。
pid_is_worker() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 1
  [ -r "/proc/$pid/cmdline" ] || return 0
  tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q "funflix worker"
}

# 三态：running（真实存活）/ stale（PID 文件存在但进程已退出或已被复用）/ absent（无 PID 文件）
pid_state() {
  [ -f "$pid_file" ] || { echo absent; return; }
  if pid_is_worker "$(cat "$pid_file")"; then echo running; else echo stale; fi
}

clear_stale_pid_file() {
  echo "检测到陈旧 PID 文件（PID $(cat "$pid_file") 已退出或已被其他进程复用），已清理"
  rm -f "$pid_file"
}

action=${1:-}
[ -n "$action" ] || usage

if [ "$action" = "status" ]; then
  [ "$#" -eq 1 ] || usage
  case "$(pid_state)" in
    running)
      echo "worker 正在运行（PID $(cat "$pid_file")）"
      ;;
    stale)
      echo "worker 未运行（陈旧 PID 文件：PID $(cat "$pid_file")）"
      exit 1
      ;;
    absent)
      echo "worker 未运行"
      exit 1
      ;;
  esac
  exit 0
fi

[ "$#" -eq 2 ] || usage
env_name=$2
case "$env_name" in
  dev) command=(uv run funflix worker) ;;
  prod) command=(funflix worker) ;;
  *) usage ;;
esac

case "$action" in
  start)
    case "$(pid_state)" in
      running) echo "worker 已运行（PID $(cat "$pid_file")）"; exit 0 ;;
      stale) clear_stale_pid_file ;;
    esac
    mkdir -p "$run_dir"
    mkdir "$lock_dir" 2>/dev/null || { echo "worker 启动锁已存在" >&2; exit 1; }
    trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT
    cd "$root"
    nohup "${command[@]}" >>"$log_file" 2>&1 &
    echo $! >"$pid_file"
    echo "worker 已启动（PID $(cat "$pid_file")）"
    ;;
  stop)
    case "$(pid_state)" in
      running)
        kill "$(cat "$pid_file")"
        rm -f "$pid_file"
        echo "worker 已停止"
        ;;
      stale) clear_stale_pid_file ;;
      absent) echo "worker 未运行" ;;
    esac
    ;;
  restart)
    "$0" stop "$env_name"
    exec "$0" start "$env_name"
    ;;
  run)
    mkdir -p "$run_dir"
    cd "$root"
    exec "${command[@]}"
    ;;
  *) usage ;;
esac
