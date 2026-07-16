# 已知问题 / Known Issues

> 本文件记录已发现、但当下决定暂不处理的问题，方便下次单独接手时快速恢复上下文。
> 记录人：克克（Claude Code）。每条注明发现日期、现状、候选解法、以及未处理的原因。

---

## 1. breath 关键词检索：加权打分被稀释，命中桶压不过 fuzzy_threshold

- **发现日期**：2026-07-16（B7 切换到 migrate-2.0.3 后的验证环节顺带发现）
- **严重度**：中——不影响数据安全，但影响「用关键词搜到某条记忆」这个核心体验
- **是否本次切换引入**：否。切换前的 `main` 分支打分公式几乎相同（只少 touch/BM25 两维），同样的输入大概率也过不了线。这是历史遗留结构问题，**不是 migrate-2.0.3 引入的新问题，也不是 B7 的目标**。

### 现象
`breath(query=...)` 关键词检索模式下，多个明明存在的记忆搜不到，一律返回「没有匹配到『xxx』相关的记忆」。

### 诊断过程（复现路径）
1. 用 4 个 stackchan 相关关键词测 `POST /api/public/breath` → 全部「没有匹配到」。
2. `GET /api/public/list` 核实原始数据 → 确认 11 条含 stackchan 的桶真实存在（不经过打分逻辑）。
3. 只读这些桶的 frontmatter（未读正文）→ `importance` 是 8~10、**不是 null**，也没有 `resolved` / `dont_surface` 标记。
   → 说明**这些桶不是「null importance 静默丢桶」那个 bug 的受害者**（那个 bug 在 `bucket_manager.py` 已修复：`int(meta.get("importance") or 5)`）。
4. 对照实验：一个 `pinned=true`、`importance=10`、tag 精确含「告别信」的核心桶（id `717de80a7e85`），查「告别信」→ **同样搜不到**。
5. 空 query 浮现模式正常（能浮出核心记忆）→ 证明 breath 本身工作，故障局限在**关键词检索路径的打分/阈值**。

### 根因分析
打分公式（`bucket_manager.py` `search()` → `_calc_topic_score()`）：

- 文本相关性子分 `topic_score` 内部构成：`name(×3) + domain(×2.5) + tag(×2) + content(×1)`，再归一化到 0~1。
- **系统里所有桶的 `name` 字段都是随机 hex id**（如 `a946995908ab`），不是描述性标题。
  → 权重最高的 `name(×3)` 项对任何中文关键词 `partial_ratio` ≈ 0，**永远白占 3/8.5 的权重**。
- 归一化后 `topic_score` 再乘外层 `w_topic=4.0`，与 emotion(×2.0，无情绪查询时固定中性 0.5)、time(×1.5)、importance(×1.0)、touch(×1.0) 一起摊进 `weight_sum`。
  → 即便 tag 精确命中，稀释后 normalized 分常压不过 `matching.fuzzy_threshold = 50`。
- 手算「告别信」精确命中约 ~42 分 < 50，与实测「搜不到」吻合。

### 待查的旁支
- migrate-2.0.3 有 BM25 维度（`w_bm25=1.5`，软依赖 `rank-bm25` + `jieba`）。理论上关键词应能从 BM25 通道补分，但实测仍搜不到。
  → 需确认线上 BM25 是否真的装上并生效（软依赖缺包会静默降级 `bm25_scores={}`）。这可能是「即使有 BM25 也没救回来」的补充原因，下轮一并查。

### 两种候选解法（下次单独评估后再决定，两者也可组合）

**解法 A：降低 `fuzzy_threshold`（config `matching.fuzzy_threshold`，50 → 例如 30~40）**
- 优点：只改一个配置值，不碰任何桶数据，全局立即生效，零数据风险，易回退。
- 缺点：治标不治本（name×3 权重被浪费的结构问题仍在）；门槛降低会让弱相关桶也漂进结果，**误召回率上升**，需要试出一个「召回够 vs 噪音可接受」的平衡点。

**解法 B：给 `name` 字段补有意义的标题**
- 优点：根治。让权重最高的 ×3 项真正发挥作用，检索质量本质提升，也顺带改善浮现/展示可读性。
- 缺点：
  - 要**回填历史全部桶的 name**（约 82 个），属于批量写操作 → **碰数据，必须先备份、分批、可回退**（今天已有 2026-07-16 的桌面+D盘双份备份可兜底）。
  - **绝对红线**：回填时**跳过桶 `ec9f02ae`**（CLAUDE.md 红线，不查询/不读 tag/不改动）。
  - 新记忆的 `hold` 流程也要相应保证能生成有意义的 `name`，否则问题会再长回来。
  - 生成标题若用 LLM 概括正文，要注意别把 feel/红线类内容发给外部模型（与 embedding 红线同源的顾虑）。

### 未处理原因
瑾儿要先单独评估 A / B 两种解法的利弊与副作用后再决定，B7 当天不动。

### 上游 v3 是否已修（2026-07-16 只读排查 upstream/main）

**结论：v3 已针对这个问题做了专门修复**，思路很值得借鉴，且不必合并整个 v3。

修复在 v3 的 `src/bucket_manager.py`（仍是传统 bucket_manager 那套的增量改进，**不在** kernel/eventsourcing/policy 等微内核模块里，移植成本低）。作者注释原话就点了同一根因：「因加权分被各维度稀释到 fuzzy_threshold 以下而整条搜不到」。

核心机制叫 **literal_hit 召回保障**（`_LITERAL_MATCH_BONUS = 25.0`）：
1. 把查询串原样（lowercase）在 `name + tags + domain + 正文` 拼成的文本里做子串匹配：`literal_hit = q_norm in hay`。
2. 命中后**双管齐下**：
   - **无条件放行**：判定改成 `text_match = normalized >= fuzzy_threshold OR literal_hit` —— 用 OR 短路，字面命中就召回，**不再依赖加权分过阈值**（正好绕开 name×3 被 hex 白占、分数被稀释的困境）。
   - **排序加分**：`normalized += 25`，让字面命中的桶在结果里排得靠前。
3. 另外并联了**语义召回**：`semantic_match = semantic_score >= 0.65`，与 text_match 取 OR。但这条依赖 embedding，红线关闭时用不上——**对我们有价值的是 literal_hit 那条，它纯文本层面工作，与红线保护不冲突**。

**相对我两种候选解法的关系**：
- v3 的 literal_hit 比「解法 A（降 fuzzy_threshold）」更精准——A 是全局降门槛、会连带抬高误召回；literal_hit 只对「用户显式搜的词原样命中」放行，噪音更可控。
- 也比「解法 B（回填 name 标题）」成本低——不用批量改 82 桶数据、不碰红线桶，只改打分函数一段逻辑。
- 可以只借这段思路移植到 migrate-2.0.3（约十几行：literal_hit 判定 + OR 放行 + bonus 常量），**不合并 v3**。这实际上是「解法 C」，下次评估时应作为首选候选。

（未处理，仅记录供下次接续；若决定不自己改，也可把「literal_hit 思路本就是原作者 v3 里的方案」这点反馈回去，确认能否 backport 到非 v3 线。）

---

## 2. 待核实是否回归：decay_engine stopped / 关键配置显示未设

- **发现日期**：2026-07-16（B7 验证时顺带观察到，非本次目标）
- **状态**：**待核实是否为回归**，今天不处理。

切换到 migrate-2.0.3 后，两个只读健康端点返回了几个存疑信号：

- `GET /health` → `"decay_engine": "stopped"`
  - 衰减引擎未在运行。不确定是新部署尚未触发启动、还是真的没起来。
- `GET /api/onboarding/status` → `"first_run": true`，且：
  - `"dashboard_password_set": false`（dashboard 密码显示未设）
  - `"gemini_key_set": false`（gemini/embedding key 显示未设）
  - `"embedding_enabled": false` ← **这一项是预期内的**，红线要求 embedding 保持 disabled，符合设计，不算问题。

### 需要核实的点
- `dashboard_password` / `gemini_key` 显示「未设」：是新部署没识别到旧的 Zeabur 环境变量（可能变量名不匹配，如历史上 `PASSWORD` vs 代码认的 `OMBRE_DASHBOARD_PASSWORD`），还是本来就一直没设？
- `decay_engine: stopped`：是否影响记忆的正常衰减/固化节奏？
- **缺少对照基准**：没有留存 6 月旧 `main` 版本的 `/health` 原始输出，无法直接判断这几项是「切换后回归」还是「历来如此」。下次可先翻部署历史或旧记录比对。

### 未处理原因
非 B7 目标，且不影响当天验收的四项（接口、数据、null 修复、红线、服务健康）。留待单独核实。
