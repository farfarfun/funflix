# CHANGELOG

本文件记录 funflix 的版本变更，按版本倒序排列。

## [未发布]

### 新增

- **作品实体改成两层：`work`（一部剧）+ `media`（一季）**。搜「大主宰」原先返回
  448 行 media（同一部作品的噪声变体），现在是一条「大主宰（3 季 / 78 资源）」。
  根因是旧身份 `(norm_key, media_type, year)` 太脆 —— 同一部剧在不同分享里年份被
  识别成 0/2023/2025、类型被判成 anime/tv/unknown 就会裂成多行，生产库 92 万行
  media 对应 89 万个不同 norm_key。新身份：`work.norm_key` 唯一，
  `media` 是 `(work_id, season)` 唯一，`year`/`media_type` 退化成属性。
- `services/canon/`：归一流水线 —— `purge`（删页面文案/提取码/纯数字那类假作品，
  resource 行保留）、`rebuild`（规则重算 + 确定性建 Work）、`resolver`（LLM 裁决
  残局）、`merge`（应用裁决）。各阶段都支持 `--dry-run` / `--key` / `--limit`、
  按组提交可中断续跑。裁决结果落 `title_canon` 表，是完整审计记录，重跑 parse
  或新数据入库都不会把已经并好的东西重新拆开。
- `services/counters.refresh_work_counters()` 与 `refresh_counters_for_media()`：
  后者是**两级联动的默认入口**。搜索列表读的是 `work` 上的计数，季级刷完不往上
  滚一层的话，新入库的资源在结果里看不见而且不报任何错。作品归属必须在刷季
  **之前**问出来 —— 刷季会顺手物理删除零资源的季，行删掉就再也查不到它曾属于
  哪部作品。
- `MediaType` 新增 `book` / `comic` / `other` 三个**非影视**类型。采集源里混着大量
  小说、漫画、课程的分享，它们是真资源，所以保留但默认不进搜索结果
  （`services/search.VIDEO_MEDIA_TYPES`）；要查就显式传 `media_type`。
  `unknown` **留在**默认可见集合里：它是「还没判出类型」而不是「不是影视」，
  库里 40 多万部作品是这个值。
- `services/text/normalize`：`series_norm_key()` / `extract_season()` /
  `strip_season()` / `looks_like_junk_title()` / `strip_scrape_labels()`，以及噪声
  词表的大幅补齐（`更至03`、`每周自动更新`、`4K高码率版`、`《》「」`、URL 碎片、
  抓取字段标签等）。`extract_season` 只在高置信写法上给出季号，拿不准返回 `None`
  交给 LLM —— 真实数据上激进的正则抽季误报率不可接受。
- `tests/test_packaging.py`：`requires-python`、classifiers、Ruff `target-version`
  三处版本声明必须自洽。改一处漏两处不会有任何报错，而 Ruff 落后尤其阴险 ——
  它按旧版本的语法上限检查，新语法能用却被标成错误（本次就是这么发现的）。
- `tests/test_setup_script.py`：把 `scripts/setup.sh` 复制到临时目录、用只含必需命令的
  干净 PATH 驱动，覆盖 `bash -n`、用法、prod/dev 入口缺失、启动即退出、重复启动被拒、
  `status` 返回非 0、陈旧 PID 文件清理。
- **持续修复机制 `services/repair/` + `funflix repair` 三条子命令**（见
  `docs/DESIGN.md` §7.5）。解析规则永远加不完，每加一条就留下一批「按旧规则算
  出来、现在看是错的」数据，所以这不是一次性迁移脚本，而是可以反复跑的三条命令：
  `scan`（只读 media + 只写 `repair_task`，规则没变就零写入）、`apply`（破坏性、
  不可逆、带限额和爆炸半径闸门）、`requeue`（把规则版本过期的文档打回 parse 队列）。
  三条都默认 dry-run，`--apply` 才写库。要点：
  - 检测和应用**必须能分开调度**：误并不可逆（关联迁走、败者行删掉，没有信息
    能分回去），而检测只读且便宜。`scan` 把结论落成 `repair_task` 行，它同时是
    审计记录。`UNIQUE (kind, media_id) WHERE status = 'pending'` 这条部分唯一索引
    是幂等的支点 —— 每轮都会重新发现同一批行，没有它任务表会无限膨胀。索引挡不住
    **跨 kind** 的并存，所以诊断结论变了的旧任务要显式撤掉。
  - 目标值**复用 `canon/lookup.py::resolve_target`**，不另写规则。修复的终点必须和
    parse 现在会产出的结果一字不差，否则 repair 改成 A、parse 改回 B，无限循环。
  - 类型和年份**只补不改**（书刊是唯一例外）。反过来写的话，一轮 scan 就能把全库
    的 `media_type` 刷成 unknown —— 绝大多数行的类型来自正文 `类型:电影`，而只看
    标题会得到 `UNKNOWN`。
  - 判据取**库里存着的** `media.title`，不是再洗一遍的结果：`clean_title` 会把
    `作者:奏光 txt` 整段剥掉，洗完书刊信号就不存在了；`(2019)` 洗完年份也没了。
  - 爆炸半径闸门按 **media 总行数**算（不是队列长度 —— 拿队列当分母的话攒得越多
    越容易通过，正好反了）：删除 > 5% 或重挂 > 20% 就拒绝，要 `--force`。
    dry-run 时也检查，因为 dry-run 的作用就是让人提前看到这个拦截。
  - `collect.yml` 新增第四个 job `repair`，和 collect / parse / verify **完全并列、
    不设 `needs`**，刻意不传 `--force`，靠 `--limit` 慢慢刷。
- `repair_task` 表 + `raw_document.parse_rules_version` 列（迁移 `d6e7f8a9b0c1`）。
  版本戳是深层重解析的廉价预筛 —— 213 万份文档不可能每次改规则都全量重跑。
  `PARSE_RULES_VERSION` 手工 bump，**刻意不用源码哈希**：改个注释不该让 213 万份
  文档重排队。
- **`funflix parse --shard i/N`**（`services/extract/concurrent_runner.py::shard_condition`）。
  落库全程只有一个消费者线程，所以单进程调大 `--concurrency` 不会提速 —— 瓶颈是
  每条文档那两次数据库往返，不是抽取的 CPU。要提吞吐只能多开进程，而多开进程
  必须分片：否则各进程的翻页游标从同一处起步，把同一批文档重复解析一遍。
  按 `id` **末位十六进制字符**取模分片 —— uuid7 的低 74 位是随机的，末位因此均匀
  （213 万行实测每片 6.25%±2%），而高位是时间戳、拿去分片会让各片的文档按入库
  时间聚堆。谓词互斥、零额外往返；漏跑一片只是把那片文档留在 `pending`。
- 迁移 `e5f6a7b8c9d0`：索引 `ix_raw_document_parse_scan (last_parsed_at NULLS FIRST, id)`。
  parse 的翻页是 keyset 分页，排序键就是这两列；没有这个索引，213 万行的全表
  排序每翻一页重做一遍。**PostgreSQL 专有** —— SQLite 的索引语法里声明不了
  `NULLS FIRST`，所以迁移里按方言跳过。
- `tests/test_pipeline_workflow.py`：把 `.github/workflows/collect.yml` 当代码测 ——
  解析出每条 `uv` 调用，拿去跟 click 的命令树逐个核对（命令、子命令、每个选项
  真的存在），再核对几条「怎么调」的约定：有 `--apply` 的必须传（漏了就是
  **绿着什么也不写**的 dry-run，无人值守时最隐蔽）、有 `--yes` 的必须传（runner
  没有 tty，漏了会挂到 timeout）、有 `--limit` 的必须传（`repair scan` 是记录在案
  的例外）、`repair apply` 不许传 `--force`、分片号是 `0..N-1` 的完整覆盖且分母跟
  矩阵对得上、每个 job 都有 timeout 和**各自**的 concurrency group、以及看门狗的
  阈值大于 cron 间隔。为此给 `dev` extra 加了 `pyyaml`（运行时代码不碰 yaml）。
- `tests/test_normalize.py::TestCleanTitleIdempotent`：`clean_title` 的幂等性用例，
  语料一半是生产库 `repair scan` 实测吐出来的真实标题。见「修复」里那两条 ——
  这条性质此前只写在 `repair/plan.py` 的 docstring 里（「`clean_title` 的每一步都是
  减法」），没有任何东西守着。

### 变更

- `PARSE_RULES_VERSION` bump 到 `rules-v2`（`models/raw.py`）。上面两条
  `clean_title` 幂等性修复收紧了「剥前缀」规则，产出会变，按该常量的约定必须
  bump。实际效果：`repair requeue` 据此把 40,384 份已解析完、版本对不上的文档
  打回了 parse 队列。
- **删掉 `scripts/canon-rollout.sh`（587 行）和 `tests/test_canon_rollout_script.py`。**
  它整套设计（COPY binary 备份、停 Action、九个人工闸门、迁移 B 收口）是为「原地改
  89 万行历史数据」服务的，而历史数据不需要保留 —— 只有采集（`raw_document`）和
  验证（`link_check`）的成果要留，其余都可以重建。而且实查发现它要原地迁移的历史
  Work 数据**根本不存在**：生产库 89 万行 media 里只有 80 行 `work_id` 非空，
  `title_canon` 80 行全是 pending。全量重建是唯一能产出可用 work 层的路径，也比
  原地改干净 —— 没有「迁移没覆盖到」的残留。持续修复的常态化手段改由
  `funflix repair` 承担。
- `services/maintenance.py::relink_checks()` 从逐行 `session.scalar` 改成攒批
  executemany：生产库有 848,416 个去重链接，原先就是 84 万次网络往返，全量重建
  卡在这一步要跑到天荒地老。现在一条流式查询读出每个链接的最新结论，按 5,000
  行一批走带 `bindparam` 的 UPDATE，往返降到 170 次左右。
- **`collect.yml` 补成五个并列节点，并把「每轮刷一小批」落实到每一步。**
  `parse` 改成 4 片矩阵（`--shard ${{ matrix.shard }}/4 --limit 5000`，
  `fail-fast: false`，concurrency group **带片号** —— 不带的话四片会被 GitHub 当成
  同一组、push 时互相取消，只剩一片真的在跑）；新增 `canon` job
  （`resolve --limit 200` 攒裁决、`merge` 落库，两步分开，`--limit` 在这里是**钱**
  的闸门）；`verify` 在探测前插一步 `db relink-checks` —— `link_check` 和
  `resource` 之间没有外键，重解析出来的新 resource 虽然 `check_status` 是
  UNCHECKED，库里却存着它（按 provider + share_id 认）历史上最后一次的结论，
  先填完能省掉几十万次无谓探测。
- **`collect.yml` 每个 job 的 `uv sync` 和每一条 `uv run` 都带上 `--extra zh`**
  （`canon` job 另加 `--extra llm`）。`uv sync` 不带 extra 时**不装** opencc，而
  `normalize._to_simplified()` 在 opencc 缺失时原样返回、不报错 —— 于是 Action 侧
  把 `唐伯虎點秋香` 归一到「點」、本地归一到「点」，同一部剧解析出两行 work，
  正是这套流水线要消灭的那种重复。又因为 `uv run` 每次都按**本次请求的** extra
  重新同步环境（前一步装好的会被下一步一条光秃秃的 `uv run` 卸掉），必须每条
  都带。统一走 workflow 级的 `env.UV_SYNC_EXTRAS`，由
  `tests/test_pipeline_workflow.py::TestExtras` 守着。
- **搜索的主体从 `Media` 改成 `Work`**（`services/search.py`）：两个后端都返回
  `list[Work]`，`search_media`/`count_media` 改名为 `search_works`/`count_works`。
  资源筛选（`valid_only` / `provider`）因此要多穿一层，走完
  `work → media → media_resource → resource` 四张表。`SearchQuery` 新增
  `media_type` 与 `with_seasons`。
- `funflix search` 改成一部剧一条、季是子层的两级展示，新增 `--links` 控制每季
  列几条链接（默认 5）—— 热门剧某一季有 793 条分享，全列出来是几百屏噪声。
- `schemas/media.py` 新增 `WorkSummary` / `WorkDetail` / `SeasonSummary` /
  `SeasonDetail`，年份哨兵的处理抽成共享基类 `_YearBlanked`。
- `extract/runner.py`、`maintenance.py` 的计数刷新路径统一改走
  `refresh_counters_for_media`。
- `docs/DESIGN.md` §3.3 / §3.6 / §4.3 / §7.2 / §7.3 与 README 的流水线图、接口段
  按两层模型重写。
- SQLite 兜底库路径从 `~/.cache/farfarfun/funflix/funflix.db` 改为
  `~/.farfarfun/funflix/funflix.db`，与组织统一的 `~/.farfarfun/<包名>/` 约定对齐
  （funflix-api 的 `api/`、funflix-web 的 `web/` 也都挂在这个目录下）。放在
  `.cache` 下本来就不妥：那是「丢了可以重建」的语义，而这是数据库。
- README 里 `FUNFLIX_DATABASE_URL` 的默认值原先写的是 `sqlite+aiosqlite:///./funflix.db`
  （CWD 相对路径），与代码里的绝对路径兜底不符，改成真实值。
- **Python 下限统一为 `>=3.12`**（与 funflix-api 一致）。此前短暂降到 `>=3.10` 的那套
  3.10 兼容层整体撤掉：删掉 `src/funflix/compat.py`，`enum.StrEnum` / `datetime.UTC`
  改回从标准库直接导入，5 处 `typing.TypeVar` 回到 PEP 695 的 `def f[T]()` / `class C[T]`
  写法。Ruff 的 `target-version` 当时留在 `py310` 没跟着改回来，是这次唯一真的不一致的
  地方 —— 三处声明现在由 `tests/test_packaging.py` 守着。
- `drives` extra（fundrive）补注释说明：`src/` 下目前没有任何模块 import 它，
  它是给 `docs/DESIGN.md` §6 规划的 `FundriveProbe` 预留的。
- `docs/DESIGN.md` §9「打包」原文照抄的 toml 片段写着 `requires-python = ">=3.12"` 和
  fastapi/uvicorn/anthropic 等本仓库根本没有的依赖，改为说明真实约定并指向 `pyproject.toml`。

### 修复

- **LLM 网关一个 429 就让 `canon resolve` 整轮白花钱**
  （`services/extract/llm/client.py`）。`extract()` 的契约写着「调用失败一律抛
  `LLMCallError`」，调用方（`canon/resolver.py`、`extract/runner.py`）也只接这一种，
  但 openai SDK 自己重试完 `max_retries` 次之后抛的 `RateLimitError` /
  `APITimeoutError` / `APIConnectionError` 从来没被收口。后果在 GitHub Action 上
  实测到了：`canon resolve --limit 200 --concurrency 8` 撞上网关的
  `429 Request Rate Reaches Maximum Limit`，异常穿过 `asyncio.gather` 一路冒到
  CLI，整个 job 退出 1 —— **连已经调完的那些块也不会落库**，`_persist` 根本没执行。
  现在 `APIError` 的任何子类都收口成 `LLMCallError`，那一页留在 pending 下轮重跑，
  其余照常落库。配套：`max_retries` 默认 2 → 5（限流是按速率算的，等几秒就过去，
  比下轮重新付整块输入 token 便宜），workflow 里 `--concurrency` 压到 4。
  程序 bug 类异常仍然原样抛出，不会被当成「这一页失败了」无限重试。
- **一个 `ed2k://` 链接就让整个采集源这一轮全废**（`services/collect/web.py`）。
  `urljoin` / `urlsplit` 遇上 `ed2k://|file|[电影]冰与火之歌.mkv|123|abc|/` 这种
  href 会抛 `ValueError: Invalid IPv6 URL` —— urllib 把 `//` 后面那段当 netloc，
  看见 `[` 就按 IPv6 字面量解析、找不到配对的 `]`。异常从 `_detail_message` /
  `fetch` 的列表循环里一路冒出来，整个源这轮采集到此为止，**同一页上正常的
  夸克链接也一起丢**。实测生产库里 16 个启用中的 web 源长期卡在这一条错误上。
  改成 `_resolve` / `_path` 两个小包装，解析不了就返回 `None` / 空串跳过这条
  href；`_canonical_url` 同样兜住，因为它背的 `normalize_identifier` 是约定了
  「判不出就返回 None」的检测接口。修完 66yingshi / 6v520 / dygang / meijumi
  全部恢复出数，剩下 6 个源的失败都是真实的外部原因（DNS 没了、404、503、
  接口改版）。
- **夸克 `41004 文件不存在` 被判成 ERROR，5.7 万条链接永远探不出结论**
  （`services/verify/quark.py`）。它是生产库 `link_check` 里第二多的失效码
  （57,697 条），却不在 `_GONE_CODES` 表上，于是落到 ERROR —— 而 ERROR 的语义是
  「判不出来，排退避重试」，这批链接每轮都被重新探一遍，还挤掉真正待校验链接的
  名额。它与 `41006 分享不存在` 的区别只是夸克那边分享还在、里面的文件被删了，
  对使用者是一样的：点进去拿不到东西。码表与文案兜底表同时补上。刻意**不**收
  `15000 inner error`（HTTP 500，88 条）—— 那是服务端抽风，重试才是对的。
  UC 网盘共用这套 `classify`，一处修两个网盘都好。
- **阿里云盘在默认 5 次/秒下几乎全被风控**（`services/verify/runner.py`）。
  `verify --limit 300` 实测 238 条返回 `{"code":"TooManyRequests"}`，生产库因此
  积压 14,284 条 `rate_limited` 资源。限速原先是一个全局值，而各网盘的耐受度差
  一个数量级：同一批 25 条阿里分享，2 次/秒仍有 11 条被限流，1 次/秒降到 2 条，
  0.5 次/秒为 0；夸克在 5 次/秒下毫无限流迹象（10,980 valid / 6,585 invalid）。
  所以加的是**按网盘覆盖**（`PROVIDER_RATE_LIMITS`，两个限流器实现共用一张折算
  逻辑），而不是把全局值调慢 —— 夸克才是队列里的大头。阿里取 1 次/秒而不是
  0.5：被限流的响应不会误判成失效，只是白跑一次，要最大化的是单位时间内**探出
  结论**的条数（1.0 × 92% > 0.5 × 100%）。
- **`clean_title` 不幂等，每轮 repair 吃掉片名开头一个拉丁字母**
  （`services/text/normalize.py`）。剥「表格残留的单字母列名」那条规则是
  `^\s*[a-z]\s+`，不管后面是什么都剥：`E T 外星人` → `T 外星人` → `外星人`，
  `K Pop 猎魔女团` → `Pop 猎魔女团`。这不是「显示难看」级别的问题 —— `repair scan`
  每 2 小时拿**库里存着的** `media.title` 重新 `clean_title` 一遍判标题漂移，而
  `media.original_title` 全库为空，所以这是一条无人值守的数据销毁回路，一天削 12 个
  字母。加了「后面必须紧跟汉字」的约束（`(?=[一-鿿])`）—— 真正要剥的是
  `D 大主宰 动漫版`、`L 狼的孩子雨和雪`、`G 灌篮高手` 这种中文片名前挂的列名，
  而会被误伤的 `K Pop`、`E T`、`G I G N` 后面跟的都是拉丁字母。
- **同一个回路的第二条：数字前缀规则把被拆开的文件大小当行号。**
  `^\s*\d{3,8}\s+` 遇上 `大小：440.41MB`（走到这条规则时点号已经被换成空格、
  成了 `440 41MB`）会剥成 `41MB`。而 `looks_like_junk_title('440 41MB')` 是 `False`，
  所以它不会被 `repair/plan.py` 判成删除、会真的进 retitle 循环。加了 `(?!\d)`。
- **`tag.media_count` 的丢更新与锁串行化**（`services/extract/runner.py`）。ORM 的
  `tag.media_count += 1` 会刷成绝对值（读到 5 就写 `SET media_count = 6`），两个
  parse 进程同时处理挂了同一标签的文档时，后提交的把前一个的 +1 盖掉 —— 这正是
  `maintenance.recount_tags` 存在的原因。更要命的是锁：生产库只有一千多个标签行，
  而几乎每条文档都挂「夸克」「电视剧」这类大热标签，自增发生在落库阶段二、行锁
  要一直握到外层事务提交（一整批上百条文档、好几秒），于是所有分片进程在那几行上
  排队。实测 8 个分片进程跑出 2.6 条/s，**比单进程的 4.6 条/s 还慢**，
  `pg_blocking_pids` 上就是 `UPDATE tag SET media_count=...` 的
  `Lock: transactionid`，最长等了 57 秒。改成把增量攒在 dict 里、收尾时一条
  `media_count = media_count + CASE ...` 算术 UPDATE（`_apply_tag_count_deltas`）：
  加法由数据库在持有行锁时自己算，没有丢更新窗口，行锁窗口也降到毫秒级。
- **`resource.seen_count` 同病同治**（`_apply_resource_seen_deltas`）。改完标签之后
  `pg_blocking_pids` 榜首换成了 `UPDATE resource SET last_seen_at=..., seen_count=...`，
  等待 1 分 55 秒 —— `resource` 有近两百万行、看着不像热行，但分享链接是**被反复
  转发**的，爆款那一份会出现在成百上千条文档里。增量的键是 ORM 对象而不是 id：
  增量在阶段一攒，那时新建的行还没 flush、`id` 要到 flush 才赋上。
- `services/text/normalize.py` 的 OpenCC 转换器改成 `functools.lru_cache` 只建一次。
  原先每次 `_to_simplified()` 都 `OpenCC("t2s")`，而那个构造函数要加载繁简字典 ——
  profile 下来它就是解析的 CPU 热点：全量解析的基准从 53.988s 降到 2.249s（24 倍）。
- **`title_canon` 的键空间错配**（`services/extract/runner.py`）。parse 时的防回退
  查询用的是 `item.norm_key`（逐标题身份），而 `canon/resolver.py` 落裁决行用的是
  `series_norm_key`（系列身份），`canon/apply.py` 回头也按 `series_norm_key` 匹配。
  两个键空间对不上，于是花钱得出的裁决在新数据入库时经常被静默绕过 —— 正是这一层
  要防的事。统一到 `series_norm_key`。
  顺带修掉同一条路径上的一个撞主键：`resolve_target` 碰到「decided 但
  `work_norm_key` 为空」的坏裁决时会回落到 `_fallback`，而 `_fallback` 无条件把
  `needs_pending_row` 置真 —— 那个键在库里已经有行了，再插一行就撞主键、把整个
  SAVEPOINT 带崩。现在插 pending 行要同时满足 `canon is None`。
- **`_SCRAPE_CUT_RE` 补齐书刊字段名** `作者|译者|主播|演播|播音|出版社|字数|连载状态`
  （沿用「必须带冒号」的约束）。`全民攻防:我有签到系统 作者:奏光 txt` 这种小说分享
  在库里有 4,399 行，作者名整条粘在片名里。同时新增 `_BOOK_SIGNAL_RE` 把这个信号
  判成 `MediaType.BOOK`，并让它排在影视关键词**之前** —— 小说正文常顺带写「改编
  动画」「同名电视剧」，先跑影视词表会把 `作者:墨之所想 txt` 判成 anime。
  三个负向回顾（`原著作者:` / `词曲作者:` / `编曲作者:`）把「这是书」和「这改编自
  书 / 这是首歌」分开；刻意不收光杆 `txt`，因为 `_SHEET_MARKER_RE` 正把它当表格
  列名残渣剥，两边会打架。
  另外查清了 926 行「导演」残留的成因：670 行无冒号（`威力导演`、`今敏导演`、
  `导演评论`，冒号约束正好挡住）、19 行落在开头两字内（`:导演你有病` 是真片名，
  `match.start() < 2` 那道闸挡住）、237 行带冒号且全是本规则加上去之前解析的陈旧
  数据 —— 后者是 `funflix repair` 的活，不是词表的问题。
- `services/maintenance.py::relink_checks()` 判「最新一条校验」原先用
  `max(LinkCheck.id)`。id 是 uuid7，只到毫秒级单调 —— 同一毫秒内落库的两条校验，
  id 的大小由随机位决定，于是 `max(id)` 会随机挑一条。一条链接从 valid 变成
  invalid、两条记录又恰好同毫秒时，这个函数会把早已失效的链接恢复成 valid。
  改成按 `checked_at DESC, id DESC` 的 `ROW_NUMBER()` 窗口函数（PG 和 SQLite
  都支持；`DISTINCT ON` 只有 PG 有，单测跑在 SQLite 上）。
- **`db reset` 不再清掉登录账号。** `PRESERVED_TABLES` 原先只有
  `{source, alembic_version}`，`user` 落在清空清单里。生产库只有一个账号，
  而密码哈希是单向的 —— 清掉就只能重新 `funflix user create`，运维区当场锁死。
  登录账号和采集源同属**配置**，不是流水线产物；`db reset --help` 也只承诺
  「采集源配置保留」，顺手清账号属于意料之外的破坏。
- **迁移 `d6e7f8a9b0c1` 挪到收口迁移 `c5d6e7f8a9b0` 之前**，链变成
  `b1c2d3e4f5a6 → d6e7f8a9b0c1 → c5d6e7f8a9b0`。两条原先的顺序会死锁：
  `reset_pipeline_data` 的清表清单从 ORM 元数据推导、现在含 `repair_task`，
  PG 分支又是单条 `TRUNCATE ... CASCADE`，表不存在就整条失败 —— 于是 reset
  要求 `d6e7f8a9b0c1` 先上；而 `c5d6e7f8a9b0` 的 `work_id` 非空守卫要求先
  reset 清空 media。`d6e7f8a9b0c1` 纯加法、`repair_task` 刻意不建 media 外键，
  对收口迁移零依赖，挪到前面即可解环。两条迁移在任何环境都没应用过
  （生产库停在 `b1c2d3e4f5a6`），换序没有已部署的库受影响。
- **`httpx` 收紧到 `>=0.27,<1`，锁文件重新解析。** `uv.lock` 的
  `[options] prerelease-mode = "allow"` 让开区间的 `httpx>=0.27` 解析到了
  `1.0.dev6`，而那个开发版已经不再导出 `AsyncClient`。后果不止于测试：
  `services/collect/tencent_text.py` 和 `collect/concurrent_runner.py` 真的会调
  `httpx.AsyncClient(...)`，而 CI 的 collect job 用的是同一份锁文件。
  测试侧的表现是 respx 拦不住请求、整套 pytest 挂在真实网络上超时（本次两次
  600 秒被杀都是这个原因）。重新 `uv lock` 后 `prerelease-mode` 回到默认的
  `if-necessary-or-explicit`，httpx 落到 0.28.1，锁文件只动了 httpx 和 httpcore
  两项。
- `scripts/setup.sh`（SPEC §6.1）：
  - `start`/`run` 在拉起进程前校验环境入口——prod 用 `command -v funflix` 确认正式包
    已安装，缺失立即非 0 退出；dev 对应校验 `uv`。此前 prod 直接执行 `funflix worker`，
    入口不存在时后台进程立刻失败，脚本却照样打印「worker 已启动」。
  - `start` 后台拉起后等待 `FUNFLIX_START_WAIT`（默认 2）秒，确认进程确实存活才写
    PID 文件并报告成功；启动即退出会回显日志尾部 20 行并非 0 退出。
  - 重复 `start` 由 `exit 0` 改为非 0 退出，调用方才能发现「这次没真的启动」。
- 补齐 215 处公开函数/类/协议方法的中文 docstring（SPEC §7），覆盖 `base/`、`models/`、
  `schemas/`、`cli.py`、`services/`（collect/extract/verify/sync/text 及 ingest、
  maintenance、search、stats）与 `worker/`，`src/` 下已无遗漏。
- 清掉两处死代码（无行为变化）：`PgTrgmSearchBackend._keyword_clause` 收了个从不使用的
  `similarity` 形参；`_ParseProcessor` 把 `extractor_override` 存进实例属性后无人读取
  （每条用哪个抽取器由生产者写进 `extractor_kind` 决定）。
- README 的 `scripts/setup.sh` 一节补充 dev/prod 差异、启动确认与非 0 语义。
- 删除 `pyproject.toml` 里空的 `[tool.setuptools]`（构建后端是 hatchling，这段是死配置）。
- 文档与实现对齐：
  - README「快速开始」原来让用户跑 `funbuild install`，但 funbuild 既不是本包的
    依赖也不在 `dev` extra 里，照着做会 command not found；改为 `pip install funflix`
    + `funflix db upgrade`（原来写的 `alembic upgrade head` 要求在仓库目录里）。
    funbuild 作为维护者工具挪到 `docs/DEVELOPMENT.md` 并说明要单独安装。
  - `docs/DESIGN.md` §7「API 设计」描述的 `GET /search`、`check_status` 默认只返
    valid、`SqliteFtsBackend`、`/api/v1/admin/stats` + API Key 都不存在；改为按
    funflix-api 的真实路由重写（`GET /media`、`valid_only` 默认 false、
    两个搜索后端、`GET /api/v1/stats`），并标注 `quality`/`sort`/`reparse`/`recheck`
    仍未实现。
  - `docs/DESIGN.md` §1（主键其实是 UUIDv7 不是自增 BigInteger、抽取器有规则/表格/
    LLM 三类且走 OpenAI 兼容协议）、§3.0/§3.1/§3.5（补 `extra`、`source_id`、
    `next_parse_at`、`last_parsed_at`、`latency_ms` 等字段）、§6.1（`LinkProbe` 实际
    没有 `patterns`/`parse()`，并写明新增网盘要改的四处）、§8 目录结构（按 `src/`
    布局重写）、§10 里程碑一并改正。
  - `docs/TODO.md` §5.6 从「DESIGN.md 多处与代码矛盾」收敛成仅剩的代码侧问题：
    把 `patterns` 收回探针上。

## [1.0.5] - 2026-10-05

### 变更

- 仅版本号变更的重新发布，代码无改动。

## [1.0.4] - 2026-10-05

### 修复

- `.github/workflows/collect.yml` 三个 job 的 `uv sync --locked` 改回 `uv sync`：
  1.0.3 的发布提交把 `uv.lock` 从仓库里删掉了，`--locked` 会因为找不到锁文件直接失败。

## [1.0.3] - 2026-10-03

### 新增

- `scripts/setup.sh`：统一管理 worker 生命周期（`start`/`stop`/`restart`/`run`/`status`），
  运行时文件落在 `.run/`。

### 修复

- 以下为 farfarfun/todo-list#676 的审计修复：
  - `scripts/setup.sh` 参数顺序由 `<dev|prod> <action>` 改为 `<action> <dev|prod>`；
    `status` 改为不区分环境、不需要参数；重复启动检测改为区分「PID 文件存在但进程已
    退出或已被复用」（陈旧 PID，自动清理并提示）与「进程真的存活」两种情况。
  - `dispose_engine()` 释放引擎后清空 `get_engine`/`get_sessionmaker` 的 `lru_cache`，
    避免调用后下一次 `get_engine()` 返回已关闭的旧引擎。
  - `security.py` 的 `hash_password`/`verify_password`、`base/db.py` 的 `create_engine`/
    `get_engine`/`get_sessionmaker`/`dispose_engine` 补充中文 docstring。
  - README 顶层命令表被 `scripts/setup.sh` 用法说明从中截断，导致 `probes`/`extractors`/
    `search`/`doc`/`ingest` 几行脱离表格无法正常渲染；改为先完整列出命令表，再另起一段
    说明 `scripts/setup.sh`。
  - `.gitignore` 补充 `.run/`、`*.rar`、`.idea/`、`.vscode/`。
  - `.github/workflows/collect.yml` 三个 job 改用 `astral-sh/setup-uv` + `uv sync --locked`
    + `uv run`，不再绕开锁文件用 `pip install -e .`。

### 变更

- 发布提交同时删除了 `uv.lock`。该文件此后一直不在仓库里，`uv sync --locked` 因此在
  1.0.4 被改回 `uv sync`。

## [1.0.2] - 2026-09-21

### 新增

- 新增 `CHANGELOG.md`；README 补齐组织介绍区块。

### 修复

- 日志统一改为 `from farlog import getLogger`，移除裸用的 `logging` 模块与 CLI 里的
  `logging.basicConfig` 自定义 handler 配置。
- `services/collect/rss.py` 里解析 feed 描述 HTML 失败时不再静默 `pass`，改为记录带
  异常上下文的 warning 日志，再回退到原始文本。
- `typer` 锁定为 `>=0.12,<0.26`（而不是另行声明 `click` 依赖）：typer 0.26 起把 click
  整份 vendor 进私有模块，`cli.py` 用 click 类型做的 `isinstance` 内省会全部失效。

### 变更

- 接入 uv 管理依赖：补全依赖版本下限（新增 `farlog>=1.1.8`，`funsecret` 抬到 `>=1.4.95`），
  开发文档改为 `uv sync`/`uv run` 系列命令，不再以 `pip install -e` 作为开发环境依赖
  管理方式。该版本曾提交 `uv.lock`，但它在 1.0.3 的发布提交里被删除。

## [0.1.67] - 2026-09-16

### 修复

- 补充搜索、采集和校验公开 API 的中文 docstring。

### 变更

- 0.1.67 及更早版本的变更详情参见 git 历史与 PyPI 发布记录：
  <https://pypi.org/project/funflix/#history>。
