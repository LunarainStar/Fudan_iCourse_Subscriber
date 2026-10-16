# 课堂点名 / 提问监测

给 iCourse 摘要流程加的一条旁路：如果某节课的录音里老师**点了名**、或者
**提问到某位同学**，就在原有的「已生成 N 份智能文档」邮件顶部高亮一段，
列出命中时间与原文，让你事后能核对有没有被点到。

设计日期 2026-10-17。与 WorkBuddy relay 无关，是独立功能（见
[README.md](README.md) 那套 relay 文档）。

## 1. 它怎么工作

两段式，先便宜后精确：

1. **规则粗筛（离线、零成本）** — `src/ai/rollcall.py:coarse_filter`
   扫 ASR 分片（`{start_ms, end_ms, text}`，由 `src/ai/transcriber.py`
   产生），只保留像点名 / 提问 / 念到你的名字学号的分片，
   相邻命中（间隔 ≤ 20 s）合并成事件，再按得分裁到上限。
   一节课（135 分钟）通常只剩几条候选。
2. **LLM 精判** — `rollcall.judge` 只把**这几条候选**（不是整篇转写）
   交给已经配好的 provider 链（modelscope → deepseek → workbuddy），
   要求逐条给出 `A`（点到你）/ `B`（向你提问）/ `C`（与你无关）。

只有 A / B 会进邮件。判定失败（例如模型全部超时）会自动退回「规则命中
且含你的名字/学号」的候选，不会因为识别失败丢掉摘要。

规则清单在 `rollcall.py` 顶部：`_ROLLCALL_RE`（点名、签到、考勤、应到实到、
举手、第几排…）、`_QUESTION_RE`（提问、你来回答、后面那位同学、听懂了吗…）。
命中「名字/学号」权重最高，其次是点名，最后是泛提问。

## 2. 怎么启用

只有一个**可选**的 Secret（不加也能用，只是只能靠学号匹配）：

| 名称 | 必填 | 内容 |
| --- | --- | --- |
| `STUDENT_NAME` | 否（推荐） | 你的称呼或名字，逗号分隔可写多个别名 / ASR 常见误识别，例如 `张三,张珊,张山` |

在仓库 Settings → Secrets and variables → Actions → New repository secret
里加。`StuId`（已存在，登录用）会自动复用：取学号全文、后四位、以及
「一二三四」这种中文读法，覆盖老师念学号式的点名。注意只匹配后四位时
会跳过「2024」这类像年份的数字。

两者都没有 → 功能整体静默关闭，行为与加它之前完全一致（不扫描、不调
LLM、邮件内容一字不变）。

`scripts/provider_env.py:EXTRA_SECRET_ENV_VARS` 已经把 `STUDENT_NAME`
列进去，所以**不需要改 workflow**——这也是刻意为之，改
`.github/workflows/` 会让 GitHub 把每次 run 挂成 `action_required`
（见 README「踩到的坑」）。

## 3. 可调变量

都是环境变量，没配就用默认值：

| 名称 | 默认 | 作用 |
| --- | --- | --- |
| `ROLLCALL_DETECT` | `1` | 总开关。`0` 则完全不扫描。 |
| `ROLLCALL_LLM` | `1` | 是否把候选交 LLM 精判。`0` 则只用规则，零额外 LLM 调用。 |
| `ROLLCALL_MAX_CANDIDATES` | `40` | 每节课最多送多少条候选给 LLM，控 token。 |

## 4. 邮件长什么样

有命中时，主题变成：

```
[FiCS] ⚠ 点名/提问 2 处 · 已生成 1 份智能文档
```

正文顶部多一块橙黄色高亮（HTML 里是 `.callout`，纯文本用 `=` 分隔线）：

```
================================================
⚠ 本节点名 / 提问（可能叫到你）
================================================
【高等数学 第 3 讲（2026-10-16）】
  - 点名 00:30 「上课之前我们先点个名 张三同学到了吗」
  - 提问 15:00 「学号21300123456的同学请回答一下」

（命中片段先由关键词规则粗筛，再交 LLM 逐条精判；时间轴来自录音转写，供你核对。）
```

即使这一轮**没有**新生成任何智能文档，只要有命中也会发这封邮件
（`main.py:_publish_to_wps` 的 callout-only 分支）；没有命中则行为
完全照旧，只有文档列表。

## 5. 成本

- 规则粗筛：纯字符串匹配，可忽略。
- LLM 精判：**每节课一次**额外调用，输入是几条候选（通常几百 token），
  远小于摘要本身的输入。想完全省掉就把 `ROLLCALL_LLM=0`。

## 6. 已知边界

- **需要带时间轴的转写分片。** 走 ASR 和走 iCourse 官方转写都会带分片；
  只有「命中数据库里缓存的整篇转写」这一条路径没有分片，会打印
  `[RollCall] no timestamped segments — skipped`。那条路径通常也已经有
  摘要，本来就会在 Phase A 直接跳过。
- **ASR 名字识别错**是主要误报来源。所以 `STUDENT_NAME` 支持写多个
  别名 / 同音字，且最终判断交给 LLM 按上下文语义做，不只看字面。
- **规则是启发式**，覆盖的是常见说法。漏报比误报多，属于预期——
  宁可少报，也不要天天发假警报。
- 命中不写数据库，只在本次 run 的邮件里出现一次；想留档就看邮件。

## 7. 改动清单

| 文件 | 改动 |
| --- | --- |
| `src/ai/rollcall.py` | **新增**，398 行，只用标准库。检测 + 判定 + 两种格式化。 |
| `src/ai/summarizer.py` | 新增 `Summarizer.complete(system, user, timeout=180)`，复用同一条 provider 回退链做一次性问答。 |
| `src/runtime/config.py` | 新增 `STUDENT_NAMES`、`ROLLCALL_DETECT/LLM/MAX_CANDIDATES`；`import re`。 |
| `src/pipeline/lecture_runner.py` | `__init__` 加 `callouts` 汇总槽；Phase F2 在摘要之后、落库之前调 `_collect_callouts`；失败只打日志。 |
| `src/api/notifier.py` | `notify_published(rows, callouts=None)`；高亮块 + `.callout` 样式；无文档但有命中时也发信。 |
| `main.py` | 建 `callouts` 槽并串到 `_drive_lectures` / `_publish_to_wps`；抽出 `_send_notification` 统一发信与日志。 |
| `scripts/provider_env.py` | `EXTRA_SECRET_ENV_VARS` 加 `STUDENT_NAME`。 |

## 8. 验证

三个脚本都在开发用的工作副本里（`icourse-pipeline/`，未提交进仓库），
不需要网络、不需要 iCourse 账号：

```bash
# 检测器本身：4 个环境档位（正常 / LLM 挂掉回落 / 未配置 / 关掉开关）
# 内含假 SMTP 服务器与假 provider
python _verify_rollcall.py            # RESULT: PASS

# 接线：main.py / lecture_runner.py / provider_env.py 的 AST + 真实执行
python _verify_rollcall_wiring.py     # RESULT: PASS

# 真链路：起 workbuddy relay，用真实 WorkBuddy 后端跑一遍 A/B/C 精判
# 需要本机 WorkBuddy 登录态（本机没装 openai SDK 时自动用 _openai_min 替身）
python _verify_rollcall_live.py       # RESULT: PASS
```

`_verify_rollcall.py` 会把 `smtplib.SMTP_SSL` 换成假服务器，断言命中
真的进了邮件正文、主题带 `⚠ 点名/提问 N 处`、没有命中时主题与正文
一字不变（零回归）、以及纯文本分隔线不会漏进 HTML 部分。

`_verify_rollcall_live.py` 用一段构造的课堂转写（2 处真点名/提问 +
3 处干扰）跑通
`coarse_filter → build_judge_prompt → Summarizer.complete → relay → 网关`，
断言「叫你名字」和「念你学号」两条都判成 `A`/`B`，且 `collect()` 的
`source` 是真实模型名而不是 `rule-only` 回落。