#!/usr/bin/env bash
set -euo pipefail

# canon 归一的生产上线脚本：备份 → 停 Action → 单组演练 → 全库四阶段 → 迁移 B 收口 → 重开 Action。
#
# 为什么要有这个脚本，而不是照着文档手敲四条命令：
#
# 1. **顺序错了会直接失败或者白跑**。purge 必须在 rebuild 之前（垃圾行没清掉会凭空
#    造出十几万个垃圾 Work）；rebuild 必须在 resolve 之前（规则能搬掉大部分重复，
#    LLM 只打残局，顺序反了就是拿钱烧确定性能解决的问题）；迁移 B 必须在 merge 之后
#    （它要把 media.work_id 设成 NOT NULL，还有行没归属就会报错回滚）。
# 2. **中间必须有人看一眼**。这是对生产库 89 万行 media 的破坏性改写，误并不可逆
#    （两部不同的剧并成一部之后，没有信息能把它们分回去），漏并还能再跑。所以单组
#    演练和 LLM 探针之后各有一道人工闸门。
# 3. **每个阶段都要能中断续跑**。四个阶段合起来要跑几个小时，断网、Ctrl-C、关笔记本
#    都是常态。阶段级断点存在 .run/canon-rollout/ 下，重跑会跳过已完成的。
#
# 用法:
#   scripts/canon-rollout.sh check            # 全程空跑：不写库、不花 token、不碰 Action
#   scripts/canon-rollout.sh apply [选项]      # 真跑
#   scripts/canon-rollout.sh status           # 看库当前状态 + 跑到哪个阶段了
#   scripts/canon-rollout.sh backup           # 只备份
#   scripts/canon-rollout.sh reset            # 清掉断点标记，下次 apply 从头来
#
# apply 的选项:
#   --yes          跳过所有人工闸门（无人值守用；意味着你已经看过 check 的报告）
#   --skip-guard   不管 Action 的开关状态（你已经手工停掉了，或者确定这会儿不会触发）
#   --from <阶段>   从这个阶段开始，它之前的全部视作已完成
#
# 阶段名依次是: backup guard rehearse purge rebuild resolve merge finalize reopen
#
# 环境变量:
#   FUNFLIX_CMD   跑 funflix 的命令，默认 "uv run --frozen funflix"（工作树里的源码）
#   FUNFLIX_PY    跑 python 的命令，要能 import funflix，默认 "uv run --frozen python"
#   CANON_KEY     单组演练用的归一键，默认 大主宰
#   GH_REPO       Action 所在仓库，默认 farfarfun/funflix
#
# 预期耗时：check 约 40 分钟，apply 几个小时（resolve 取决于残局规模和模型速度）。
# canon 的每个阶段都要全表扫 media 算归一键 —— series_norm_key 不是存储列，没法
# 下推成 SQL 过滤，所以哪怕 --key 只要一组，那一趟扫描也躲不掉（约 8 分钟）。

root=$(cd "$(dirname "$0")/.." && pwd)
run_dir="$root/.run/canon-rollout"
#: 必须是脚本级变量而不是 pg_connect 的 local —— EXIT trap 是在函数返回之后
#: 才执行的，那时候 local 已经销毁，set -u 下 trap 自己会报 unbound variable
#: 并把退出码带成 1（症状：一切正常但脚本报错退出，密码文件还留在盘上）。
pgpass_file="$run_dir/.pgpass"
pgenv_file="$run_dir/.pgenv"
backup_root="$HOME/.farfarfun/funflix/backup"
canon_key=${CANON_KEY:-大主宰}
gh_repo=${GH_REPO:-farfarfun/funflix}
# `--frozen`：按 uv.lock 原样跑，既不校验也不更新它。上线过程中重新解析依赖
# 是纯风险（会悄悄换掉某个传递依赖的版本），而且不加这个的话 `uv run` 会把
# lock 里的 `[options] prerelease-mode` 抹掉 —— 一次上线顺手改了依赖解析规则。
read -r -a funflix_cmd <<<"${FUNFLIX_CMD:-uv run --frozen funflix}"
read -r -a funflix_py <<<"${FUNFLIX_PY:-uv run --frozen python}"

#: apply 的阶段顺序。改这里要同步改上面的用法说明和各 stage_* 函数。
STAGES=(backup guard rehearse purge rebuild resolve merge finalize reopen)

#: canon 碰的表。备份就备这五张 —— resource / raw_document 合起来 5 GB 多，
#: 而 canon 全程不写它们（垃圾 media 删除时只删关联，resource 行原地保留）。
TABLES=(media media_resource media_tag work title_canon)

die() {
  echo "错误：$*" >&2
  exit 1
}

# 查一个 workflow 当前是 active 还是 disabled_manually。
# 用 `gh workflow list` 而不是 `gh workflow view` —— 后者不支持 --json，
# 只能解析它给人看的表格输出，文案一改就碎。
wf_state() {
  gh workflow list --all --repo "$gh_repo" --json path,state \
    -q "map(select(.path | endswith(\"/$1\"))) | .[0].state // empty" 2>/dev/null
}

usage() {
  cat <<'EOF'
用法:
  scripts/canon-rollout.sh check            # 全程空跑：不写库、不花 token、不碰 Action
  scripts/canon-rollout.sh apply [选项]      # 真跑
  scripts/canon-rollout.sh status           # 看库当前状态 + 跑到哪个阶段了
  scripts/canon-rollout.sh backup           # 只备份
  scripts/canon-rollout.sh reset            # 清掉断点标记，下次 apply 从头来

apply 的选项:
  --yes          跳过所有人工闸门（无人值守用；意味着你已经看过 check 的报告）
  --skip-guard   不管 Action 的开关状态（你已经手工停掉了）
  --from <阶段>   从这个阶段开始，它之前的全部视作已完成

阶段依次是: backup guard rehearse purge rebuild resolve merge finalize reopen
详细说明见脚本顶部的注释。
EOF
  exit 2
}

# ───────────────────────────── 数据库连接 ─────────────────────────────

# 把连接参数准备好，让后面所有 psql 调用都能裸着跑。
#
# 密码从 funsecret 经 funflix 的 settings 取出，由 python 直接写进 600 权限的
# PGPASSFILE，**不经过 shell 变量、不出现在 argv 里** —— 后者会被同机任何用户
# 从 ps 看到，也会被 set -x 打进日志。
pg_connect() {
  command -v psql >/dev/null || die "找不到 psql。装一个 postgresql-client 即可（版本不限，见下面关于 pg_dump 的说明）。"
  mkdir -p "$run_dir"
  rm -f "$pgpass_file" "$pgenv_file"
  (
    umask 077
    "${funflix_py[@]}" - "$pgpass_file" "$pgenv_file" <<'PY' >/dev/null
import pathlib
import sys

from sqlalchemy.engine import make_url

from funflix.base.config import get_settings

url = make_url(get_settings().database_url)
if not url.drivername.startswith("postgresql"):
    sys.exit(
        f"数据库不是 PostgreSQL（{url.drivername}）。这个脚本是为生产 PG 写的：\n"
        "        备份走 psql 的 COPY、不变量检查用 PG 方言。本地 SQLite 上直接跑\n"
        "        funflix canon 各子命令就好，不需要这一套。"
    )
host, port = url.host or "localhost", url.port or 5432
pathlib.Path(sys.argv[1]).write_text(f"{host}:{port}:{url.database}:{url.username}:{url.password}\n")
# 非敏感的四项单独落一个文件给 shell source —— 走文件而不是 stdout，
# 因为 funsecret / 配置加载会往 stdout 打日志，解析输出会串行。
pathlib.Path(sys.argv[2]).write_text(
    f"PGHOST={host}\nPGPORT={port}\nPGDATABASE={url.database}\nPGUSER={url.username}\n"
)
PY
  ) || die "取数据库连接参数失败（上面是原始输出）。检查 funsecret 里的数据库配置。"
  [ -s "$pgenv_file" ] || die "连接参数文件是空的：$pgenv_file"
  # 跑完就删掉 —— 哪怕是 600 权限，也没必要在盘上长期留一份明文密码；
  # 每次运行都会重新生成。
  #
  # 要同时挂 INT/TERM：这脚本一跑几个小时，Ctrl-C 是常态，而被信号杀掉时
  # bash **不会**执行 EXIT trap。kill -9 和 SIGPIPE 仍然收不到任何信号，
  # 所以 pg_connect 开头还留了一次无条件 rm 兜底。
  trap 'rm -f "$pgpass_file"' EXIT
  trap 'rm -f "$pgpass_file"; exit 130' INT TERM
  # shellcheck disable=SC1090
  . "$pgenv_file"
  export PGHOST PGPORT PGDATABASE PGUSER
  export PGPASSFILE="$pgpass_file"
  echo "数据库：$PGDATABASE @ $PGHOST:$PGPORT（用户 $PGUSER）"
}

sql() { psql -q -v ON_ERROR_STOP=1 "$@"; }

# ───────────────────────────── 不变量检查 ─────────────────────────────

#: 归并前后必须对得上的量。核心是三条"一条资源都不能丢"：
#: 关联行数、未归属 resource、孤儿关联。计数列的口径照 services/counters.py：
#: media.resource_count = 关联行数，media.valid_resource_count = 其中 check_status
#: 为 valid 的条数，work 的三个计数都是对下属各季的纯求和。
#:
#: 怎么看这张表 —— 分三类，别一律要求是 0：
#:
#: - **必须始终是 0**：两个「孤儿关联」。不是 0 就说明删 media 时漏删了关联，
#:   或者关联指向了不存在的 resource，这是真的数据损坏。
#: - **必须是 0 才能跑迁移 B**：「work_id 为空」和「(work_id,season) 撞车」。
#:   前者过不了 NOT NULL，后者过不了唯一索引。stage_finalize 会自己先查。
#: - **只看趋势，不要求是 0**：两个「计数不一致」。2026-10-05 的基线就是
#:   64 / 80（迁移 A 时期建的那 80 个 Work 计数列从没刷过），canon 各阶段
#:   只对自己动过的行重算计数，所以它应该一路往下走、最后归零；中途不为 0
#:   不代表出错。「未归属 resource」同理，基线 42,493。
#:
#: 每次快照都存一份到 .run/canon-rollout/inv-<标签>.txt，前后 diff 比盯单次数字有用。
invariants() {
  local snap
  snap="$run_dir/inv-${1:-adhoc}-$(date +%H%M%S).txt"
  echo "（扫 3M 行关联表，约一两分钟；快照存 ${snap##*/}）"
  sql -c "
    select 项, 值 from (values
      ( 1, 'media 总行数',            (select count(*)::text from media)),
      ( 2, 'work_id 为空',            (select count(*)::text from media where work_id is null)),
      ( 3, 'Work 总数',               (select count(*)::text from work)),
      ( 4, '资源关联行数',            (select count(*)::text from media_resource)),
      ( 5, '标签关联行数',            (select count(*)::text from media_tag)),
      ( 6, 'resource 总行数',         (select count(*)::text from resource)),
      ( 7, '未归属 resource',         (select count(*)::text from resource r
                                        where not exists (select 1 from media_resource mr
                                                           where mr.resource_id = r.id))),
      ( 8, '孤儿关联 media 侧',       (select count(*)::text from media_resource mr
                                        where not exists (select 1 from media m where m.id = mr.media_id))),
      ( 9, '孤儿关联 resource 侧',    (select count(*)::text from media_resource mr
                                        where not exists (select 1 from resource r where r.id = mr.resource_id))),
      (10, '(work_id,season) 撞车',   (select count(*)::text from (select work_id, season from media
                                        where work_id is not null group by 1, 2 having count(*) > 1) t)),
      (11, '季计数不一致',            (select count(*)::text from media m
                                        left join (select mr.media_id,
                                                          count(*) n,
                                                          sum(case when r.check_status = 'valid' then 1 else 0 end) v
                                                     from media_resource mr
                                                     join resource r on r.id = mr.resource_id
                                                    group by mr.media_id) a on a.media_id = m.id
                                        where m.resource_count <> coalesce(a.n, 0)
                                           or m.valid_resource_count <> coalesce(a.v, 0))),
      (12, '作品计数不一致',          (select count(*)::text from work w
                                        left join (select work_id,
                                                          count(*) s,
                                                          coalesce(sum(resource_count), 0) r,
                                                          coalesce(sum(valid_resource_count), 0) v
                                                     from media where work_id is not null
                                                    group by work_id) a on a.work_id = w.id
                                        where w.season_count <> coalesce(a.s, 0)
                                           or w.resource_count <> coalesce(a.r, 0)
                                           or w.valid_resource_count <> coalesce(a.v, 0))),
      (13, 'title_canon pending',     (select count(*)::text from title_canon where status = 'pending')),
      (14, 'title_canon decided',     (select count(*)::text from title_canon where status = 'decided')),
      (15, 'title_canon rejected',    (select count(*)::text from title_canon where status = 'rejected')),
      (16, 'alembic 版本',            (select version_num from alembic_version))
    ) t(序, 项, 值) order by 序;" | tee "$snap"
}

#: 单组演练的验收口径。期望：`$canon_key` 收成一个 Work（底下按季分开），
#: 而「天命大主宰」「诛天大主宰」「北灵少年志之大主宰」「深空彼岸大主宰4」
#: 这些**不同的作品**各自独立 —— 这一条比并得多不多重要得多。
key_report() {
  # 从 stdin 喂 SQL、由 bash 插值，而不是 psql 的 -v + :'k' —— psql **不会**在
  # -c 给的字符串里做变量插值（会原样发给服务器，报 syntax error at or near ":"）。
  # 插值前把单引号翻倍，这是 SQL 字符串字面量的标准转义（CANON_KEY 来自环境变量）。
  local k=${canon_key//\'/\'\'}
  echo "── 含「$canon_key」的 Work（各季是「季号:资源数」）"
  sql <<EOF
select w.title, w.media_type as 类型, nullif(w.year, 0) as 年份,
       w.season_count as 季数, w.resource_count as 资源数,
       (select string_agg(m.season || ':' || m.resource_count, ', ' order by m.season)
          from media m where m.work_id = w.id) as 各季
  from work w
 where w.title like '%$k%' or w.norm_key like '%$k%'
 order by w.resource_count desc limit 40;
select count(*) as 还没归属到Work的行数 from media
 where work_id is null and title like '%$k%';
EOF
}

# ───────────────────────────── 阶段 ─────────────────────────────

stage_backup() {
  # 不用 pg_dump：本机客户端是 16.x，对 18.x 的服务器会直接
  # `aborting because of server version mismatch` 拒跑，而 psql 跨大版本查询没问题。
  # COPY 二进制格式的代价是**不含表结构**，恢复前目标表必须已经存在且列顺序一致。
  local free_mb
  free_mb=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')
  [ "$free_mb" -ge 1024 ] || die "$HOME 只剩 ${free_mb}MB，备份要 ~200MB 加富余量。先腾点地方。"

  local dir
  dir="$backup_root/pre-canon-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$dir"
  echo "备份到 $dir"
  # 校验失败就把目录改名再退出 —— 一份半截的备份比没有备份更危险，
  # 它会在最需要它的时候才暴露，而那时候原始数据已经改掉了。
  bad_backup() {
    mv "$dir" "$dir.FAILED"
    die "$1
      这一份已改名成 ${dir##*/}.FAILED，不要拿它恢复。"
  }

  local t f head6 tail2
  for t in "${TABLES[@]}"; do
    f="$dir/$t.bin.gz"
    psql -q -v ON_ERROR_STOP=1 -c "\\copy $t to stdout with (format binary)" | gzip -6 >"$f"
    # 三道校验，各管一种坏法：
    #   gzip -t    —— 整个流的 CRC，管「传输中断导致文件截断」
    #   PGCOPY 头  —— 管「psql 一开始就连不上，写出来一个空壳」
    #   ffff 尾    —— 管「psql 中途报错退出，而 gzip 仍然产出了一个完整合法的
    #                 .gz，只是里面装着半张表」。这一道最关键，gzip -t 查不出来。
    gzip -t "$f" || bad_backup "$t 的备份 gzip 校验失败"
    # 两行末尾的 `|| true` 不是偷懒，是必需的：`head -c 6` 读够就关管道，
    # gzip 吃到 SIGPIPE 非零退出，pipefail 把整条管道判失败 —— 而赋值语句的
    # 退出码就是命令替换的退出码，于是 set -e 会在这里把脚本直接带走
    # （症状是只备完第一张表就静默退出，连错误都不打）。
    head6=$(gzip -dc "$f" | head -c 6) || true
    tail2=$(gzip -dc "$f" | tail -c 2 | od -An -tx1 | tr -d ' \n') || true
    [ "$head6" = PGCOPY ] || bad_backup "$t 的备份缺 PGCOPY 文件头（拿到的是 '$head6'）"
    [ "$tail2" = ffff ] || bad_backup "$t 的备份缺 COPY 结束标记，是截断的（尾部是 '$tail2'）"
    printf '  %-16s %8.1f MB\n' "$t" "$(bc -l <<<"$(stat -c %s "$f") / 1000000")"
  done

  {
    echo "# pre-canon 备份"
    echo
    echo "- 时间：$(date -Iseconds)"
    echo "- 服务器：$(psql -tAc 'select version()')"
    echo "- 库：$PGDATABASE @ $PGHOST"
    echo "- alembic_version：$(psql -tAc 'select version_num from alembic_version')"
    echo
    # shellcheck disable=SC2016  # 反引号是 Markdown 的代码标记，不要展开
    echo '格式是 `COPY ... WITH (FORMAT binary)`，只在同大版本的服务器之间可移植，'
    echo '且**不含**表结构/索引/约束 —— 恢复前目标表必须已存在且列顺序一致。'
    echo
    echo '## 恢复'
    echo
    echo '```bash'
    echo '# 外键顺序：先灌 work，再 media，最后两张关联表'
    echo "export PGPASSFILE=...   # 或 PGPASSWORD，从 funsecret 取，别写进脚本"
    echo "psql -c 'truncate media_tag, media_resource, media, work cascade'"
    for t in work media media_resource media_tag title_canon; do
      echo "gzip -dc $t.bin.gz | psql -c '\\copy $t from stdin with (format binary)'"
    done
    echo '```'
  } >"$dir/MANIFEST.md"
  echo "$dir" >"$run_dir/last-backup"
  echo "清单：$dir/MANIFEST.md"
}

# collect.yml 的 parse job 每两小时跑一次（cron 13 */2 * * *），push 到 master 也触发，
# 它会在 canon 搬 media 的同时往同一批表里写 —— 并发 insert 正撞上 (work_id, season)
# 的合并逻辑。所以全库跑之前必须停掉。
#
# 停之前先把原状态记下来：本来就是 disabled 的（你之前手工关的）最后不该被我重新打开。
stage_guard() {
  if [ "$skip_guard" = 1 ]; then
    echo "--skip-guard：不检查 Action 状态。确保这几个小时里没有 collect/watchdog 在跑。"
    return 0
  fi
  command -v gh >/dev/null || die "找不到 gh，无法停 Action。
      要么装 gh 并 gh auth login，要么自己到 GitHub 网页上停掉 collect.yml 和
      pipeline-watchdog.yml，然后加 --skip-guard 重跑。"
  local wf state
  for wf in collect.yml pipeline-watchdog.yml; do
    # `|| true` 同上：gh 查不到 workflow 会非零退出，而赋值语句会把那个状态
    # 交给 set -e。这里要的是「查不到就跳过」，不是「查不到就终止上线」。
    state=$(wf_state "$wf") || true
    if [ -z "$state" ]; then
      echo "  $wf：查不到（仓库里没有这个 workflow？跳过）"
      continue
    fi
    echo "$state" >"$run_dir/$wf.prev-state"
    if [ "$state" = active ]; then
      echo "  $wf：active → 停掉"
      gh workflow disable "$wf" --repo "$gh_repo"
    else
      echo "  $wf：$state（本来就没开，收尾时不会被打开）"
    fi
  done
}

# 最关键的一关。只动一个归一键，跑完人工核对再放开全库。
stage_rehearse() {
  echo "── 单组演练：--key $canon_key"
  "${funflix_cmd[@]}" canon purge --key "$canon_key" --apply --yes
  "${funflix_cmd[@]}" canon rebuild --key "$canon_key" --apply --yes
  key_report
  invariants rehearse
  gate "核对上面三件事，确认无误才放开全库：
    1) 「$canon_key」是否收成了一个 Work、各季分开？
    2) 其他同名但**不是同一部作品**的（天命大主宰 / 诛天大主宰 /
       北灵少年志之大主宰 / 深空彼岸大主宰4 / 各种同名小说）是否各自独立？
       这一条比并得多不多重要得多 —— 误并不可逆。
    3) 两个「孤儿关联」是否都是 0？（计数不一致不用是 0，基线就是 64/80）"
}

stage_purge() {
  "${funflix_cmd[@]}" canon purge --apply --yes
  invariants purge
}

stage_rebuild() {
  "${funflix_cmd[@]}" canon rebuild --apply --yes
  invariants rebuild
}

# resolve 是唯一花钱的阶段，也是真实模型路径第一次在生产数据上跑
# （在此之前 title_canon 里的裁决全是手写的测试样例）。所以先用 --limit 3
# 打个小额探针，看裁决长什么样，再放开全量。
#
# 中断了直接重跑：decided 的键会被跳过，不重复付费。
stage_resolve() {
  echo "── LLM 探针：只送 3 个候选块"
  "${funflix_cmd[@]}" canon resolve --limit 3 --apply --yes
  echo "── 刚写进 title_canon 的裁决"
  sql -c "
    select norm_key, work_title, season as 季, media_type as 类型,
           nullif(year, 0) as 年份, is_junk as 垃圾, round(confidence::numeric, 2) as 置信度, model
      from title_canon
     where status = 'decided' and decided_at is not null
     order by decided_at desc limit 40;"
  gate "核对上面的裁决：有没有把不同作品并到同一个 work_title 上？
    （误并不可逆，漏并还能再跑，所以宁可保守）confidence 是否普遍偏低？
    确认无误才放开全量 resolve"
  "${funflix_cmd[@]}" canon resolve --apply --yes
  sql -c "select status as 状态, count(*) as 条数 from title_canon group by status order by 2 desc;"
}

stage_merge() {
  "${funflix_cmd[@]}" canon merge --apply --yes
  key_report
  invariants merge
}

# 迁移 B：work_id 设 NOT NULL、season 默认 0 且 NOT NULL、加 UNIQUE(work_id, season)、
# 删 uq_media_identity。前面没归属干净的话这一步会报错回滚 —— 先自己查一遍，
# 把「迁移失败」变成「脚本告诉你还差多少行」。
stage_finalize() {
  local left dup
  left=$(psql -tAc 'select count(*) from media where work_id is null')
  dup=$(psql -tAc 'select count(*) from (select work_id, season from media
                    where work_id is not null group by 1, 2 having count(*) > 1) t')
  [ "$left" = 0 ] || die "还有 $left 行 media 没归属到 Work，迁移 B 的 NOT NULL 会失败。
      先看看它们是什么：select title from media where work_id is null limit 50;
      大概是 resolve 没覆盖到的残局 —— 重跑 canon resolve / merge，或者手工补
      title_canon 再 merge。"
  [ "$dup" = 0 ] || die "有 $dup 组 (work_id, season) 撞车，uq_media_season 唯一索引加不上。
      重跑 canon merge 应该能把它们并掉；并不掉的要手工看。"
  "${funflix_cmd[@]}" db upgrade
  invariants finalize
}

# 只重开那些本来是 active 的。stage_guard 把原状态记在 .prev-state 里了。
stage_reopen() {
  if [ "$skip_guard" = 1 ]; then
    echo "--skip-guard：Action 不是我停的，也就不由我开。记得自己开回去。"
    return 0
  fi
  local wf prev
  for wf in collect.yml pipeline-watchdog.yml; do
    prev=$(cat "$run_dir/$wf.prev-state" 2>/dev/null || echo "")
    case "$prev" in
      active)
        echo "  $wf：开回 active"
        gh workflow enable "$wf" --repo "$gh_repo"
        ;;
      "") echo "  $wf：没有记录（guard 阶段没跑过？）不动它" ;;
      *) echo "  $wf：原本是 $prev，保持不动" ;;
    esac
  done
}

# ───────────────────────────── 编排 ─────────────────────────────

gate() {
  if [ "$assume_yes" = 1 ]; then
    echo "[--yes] 跳过闸门：$1"
    return 0
  fi
  # 不在非交互环境下自动放行 —— 这是生产库上的破坏性改写，"没人看着也继续"
  # 必须是显式选择（--yes），不能是环境凑巧导致的默认行为。
  [ -t 0 ] || die "需要人工确认，但 stdin 不是终端：
      $1
      要无人值守地跑，显式加 --yes。"
  echo
  echo ">>> $1"
  printf '>>> 继续? [y/N] '
  local ans
  read -r ans
  case "$ans" in
    y | Y | yes | YES) ;;
    *) die "已中止。断点保留在 $run_dir，重跑 apply 会从这一阶段继续。" ;;
  esac
}

run_stage() {
  local name="$1"
  local marker="$run_dir/$name.done"
  if [ -f "$marker" ]; then
    echo "跳过 $name（已完成于 $(cat "$marker")）"
    return 0
  fi
  echo
  echo "════════════ 阶段 $name ════════════ $(date +%H:%M:%S)"
  "stage_$name"
  date -Iseconds >"$marker"
}

cmd_apply() {
  local started=0 s
  for s in "${STAGES[@]}"; do
    if [ -n "$from_stage" ] && [ "$started" = 0 ]; then
      if [ "$s" = "$from_stage" ]; then
        started=1
      else
        echo "跳过 $s（--from $from_stage）"
        continue
      fi
    fi
    run_stage "$s"
  done
  echo
  echo "════════════ 全部完成 ════════════"
  echo "最后一次备份：$(cat "$run_dir/last-backup" 2>/dev/null || echo 无)"
  echo "下一步：搜一下验证效果 —— curl 'localhost:8000/api/v1/works?keyword=$canon_key'"
  echo "        期望 total 是个位数，而不是 400 多。"
}

# 空跑：四个阶段都用默认的 dry-run，不写库、不发一次 LLM 调用、不碰 Action。
# 耗时和真跑的扫描部分一样（每阶段全表扫 media），所以要 40 分钟左右。
cmd_check() {
  echo "── 当前库状态"
  invariants check-baseline
  key_report
  echo
  echo "── Action 状态（只看不动）"
  if command -v gh >/dev/null; then
    local wf
    for wf in collect.yml pipeline-watchdog.yml; do
      printf '  %-24s %s\n' "$wf" "$(wf_state "$wf" || true)"
    done
  else
    echo "  找不到 gh，跳过"
  fi
  local c
  for c in purge rebuild resolve merge; do
    echo
    echo "════════════ 空跑 canon $c ════════════ $(date +%H:%M:%S)"
    "${funflix_cmd[@]}" canon "$c"
  done
  echo
  echo "════════════ 空跑完毕，一个字都没写 ════════════"
}

cmd_status() {
  echo "── 阶段进度（断点在 $run_dir）"
  local s marker
  for s in "${STAGES[@]}"; do
    marker="$run_dir/$s.done"
    if [ -f "$marker" ]; then
      printf '  %-10s 已完成  %s\n' "$s" "$(cat "$marker")"
    else
      printf '  %-10s 未完成\n' "$s"
    fi
  done
  echo "  最后一次备份：$(cat "$run_dir/last-backup" 2>/dev/null || echo 无)"
  echo
  echo "── 当前库状态"
  invariants status
}

cmd_reset() {
  local s
  for s in "${STAGES[@]}"; do rm -f "$run_dir/$s.done"; done
  echo "断点已清。备份文件和 .prev-state 保留 —— 它们不是进度，是资产。"
}

action=${1:-}
[ -n "$action" ] || usage
shift || true

assume_yes=0
skip_guard=0
from_stage=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes | -y) assume_yes=1 ;;
    --skip-guard) skip_guard=1 ;;
    --from)
      from_stage=${2:-}
      [ -n "$from_stage" ] || die "--from 要跟一个阶段名：${STAGES[*]}"
      [[ " ${STAGES[*]} " == *" $from_stage "* ]] || die "没有这个阶段：$from_stage（可选：${STAGES[*]}）"
      shift
      ;;
    *) die "未知参数：$1" ;;
  esac
  shift
done

cd "$root"
mkdir -p "$run_dir"
log="$run_dir/rollout-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$log") 2>&1
echo "日志：$log"
echo "funflix：${funflix_cmd[*]}"

case "$action" in
  check)
    pg_connect
    cmd_check
    ;;
  apply)
    pg_connect
    cmd_apply
    ;;
  status)
    pg_connect
    cmd_status
    ;;
  backup)
    pg_connect
    stage_backup
    ;;
  reset) cmd_reset ;;
  *) usage ;;
esac
