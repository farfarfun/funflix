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

# SPEC §6.1：prod 只能跑已安装的正式包，缺失就报错退出，绝不回退到源码。
# 这里在 start/run 真正拉起进程之前先校验入口可用，而不是等后台进程失败后
# 才从日志里发现——后者会让脚本先打印"已启动"，再留下一个空壳 PID 文件。
require_prod_entrypoint() {
  if ! command -v funflix >/dev/null 2>&1; then
    echo "错误：prod 环境要求已安装 funflix 正式包，但 PATH 里找不到 funflix 命令。" >&2
    echo "      请先 pip install funflix（或 uv tool install funflix）后重试；" >&2
    echo "      不要用 dev 环境代替——dev 走 uv run，跑的是工作树里的源码。" >&2
    exit 1
  fi
}

require_dev_toolchain() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "错误：dev 环境要用 uv 运行工作树里的源码，但 PATH 里找不到 uv 命令。" >&2
    echo "      安装方式见 https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
  fi
}

[ "$#" -eq 2 ] || usage
env_name=$2
case "$env_name" in
  dev) command=(uv run funflix worker) ;;
  prod) command=(funflix worker) ;;
  *) usage ;;
esac

# 只有真要拉起进程的动作才做环境校验；restart 由它自己 exec 出去的 start 负责。
if [ "$action" = "start" ] || [ "$action" = "run" ]; then
  case "$env_name" in
    dev) require_dev_toolchain ;;
    prod) require_prod_entrypoint ;;
  esac
fi

case "$action" in
  start)
    case "$(pid_state)" in
      # SPEC §6.1：拒绝重复启动——要以失败状态拒绝，调用方（CI、supervisor、
      # 人工 && 串联）才能发现"这次没真的启动"，exit 0 会把它伪装成成功。
      running)
        echo "错误：worker 已在运行（PID $(cat "$pid_file")），拒绝重复启动。" >&2
        exit 1
        ;;
      stale) clear_stale_pid_file ;;
    esac
    mkdir -p "$run_dir"
    mkdir "$lock_dir" 2>/dev/null || { echo "worker 启动锁已存在" >&2; exit 1; }
    trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT
    cd "$root"
    nohup "${command[@]}" >>"$log_file" 2>&1 &
    child_pid=$!
    # 启动失败（入口缺失、配置错误、数据库连不上）几乎都发生在头一两秒内。
    # 不等就写 PID 文件并打印"已启动"，会把一个已经退出的进程报成运行中。
    sleep "${FUNFLIX_START_WAIT:-2}"
    if ! pid_is_worker "$child_pid"; then
      echo "错误：worker 启动后立即退出（PID $child_pid），最后 20 行日志：" >&2
      tail -n 20 "$log_file" >&2 || true
      echo "完整日志：$log_file" >&2
      exit 1
    fi
    echo "$child_pid" >"$pid_file"
    echo "worker 已启动（PID $child_pid），日志：$log_file"
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
