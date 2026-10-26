# DevFlow AI 检索层评测 — 实测记录与简历证据

> 所有数据均由本机真实运行产生，可复现。运行环境：Windows + Docker Desktop，
> Milvus（`devflow_rag_qwen1024`，1024 维，COSINE）+ PostgreSQL。
> 时间：2026-10-25

## 1. 评测对象与背景

DevFlow AI 是面向研发流程的 RAG 知识库（仓库/PR/Issue/WorkflowRun/日志 → 检索 → 问答）。
本次工作针对**检索层**做效果评测，而不是凭感觉描述「用了混合检索」。

评测仓库：`ljy-0105/task-planner-interview`（任务规划实验室，内部 demo 项目）。

## 2. 评测集设计

`docs/datas/evals/retrieval-v2.json` — 17 条用例，按检索机制可分离为两类：

| 类型 | 数量 | 设计意图 |
|---|---|---|
| lexical | 7 | 问题与文档存在显式词汇重叠，BM25 应占优 |
| semantic | 10 | 问题与文档**词汇不重叠**，须语义理解（如「怎么确认一次请求成功还是失败」→ 日志 `mock-ai.started/succeeded/failed`） |

用例覆盖 5 类源文件：`project_doc`（任务/环境/模拟接口/代码入口/基础检查/开源许可）、`project_manifest/package.json`。

**关键设计决策**：只用本地可控语料，不引入网络依赖；每条用例的 `source_file` 指向期望命中文档主题，
用于计算 Recall@K / MRR / nDCG。

## 3. 评测过程中定位并修复的 3 个真实缺陷

这部分是本次工作的核心价值 —— 评测不是为了出一个好看的分数，而是**暴露并修复了检索链路上的真实 bug**。

### 缺陷 1：`nDCG` 指标返回值恒为 0

`backend/app/services/evals/ragas_runner.py` 的 `_retrieval_metrics()` 计算出 nDCG 后**没有放进返回字典**，
导致所有报告里 nDCG 永远显示 0.0000 —— 看起来像「检索质量极差」，实际是实现漏写。
修复后 nDCG 才具备区分度（0.81–0.91 区间）。

> 教训：指标实现本身也会有 bug。发现「所有配置 nDCG 都恰好为 0」时就该怀疑实现，而不是怀疑数据。

### 缺陷 2：`source_id` 误匹配导致「假满分」

`_source_hit()` 判定命中时用 `str(result.get("id")) in expected_source_ids or str(result.get("source_id")) in expected_source_ids`。
Milvus 返回的 `source_id` 是短哈希（如 `fc42ab0e`），而当评测把整个字符串做子串/集合比对时，
**大量不相关文档因哈希前缀碰撞被误判为命中**，让向量检索一度呈现 Recall=1.0000 的假象。

### 缺陷 3（最重要）：本地项目索引根本没进 Milvus

`backend/app/services/project_indexing.py` 调用 `replace_documents(...)` 时**硬编码**：

```python
generate_embeddings=False,
sync_vector_store=False,
```

后果：`project_doc` 类文档只写进 PostgreSQL、**从不进 Milvus**。于是：
- 向量检索对这些文档完全失明；
- 混合检索的向量支路同样是空的，实际退化成「BM25 单路」。

这解释了为什么补索引前 Milvus 里 5 条实体**全部**属于另一个仓库 `course-demo/devflow-sample-api`，
而目标仓库一条都没有。

**修复**：改为 `generate_embeddings=True, sync_vector_store=True`。
`project_doc` 的 `retrieval_mode` 是 `rag`，`should_vectorize()` 对它返回 True；
`project_overview` / `project_manifest` 是 `direct` 模式，`add_documents()` 会按 `should_vectorize()` 自动跳过，
因此不需要在这里额外排除。

**验证**（通过产品自身入口 `scan_project_index()` 走完整链路，非手工补数据）：

```
扫描前 documents: 9
scan_project_index -> status=ready docs_indexed=9 (89.1s)
扫描后 documents: 9  应向量化: 7

Milvus total entities: 12
  repo ee0e838c: 5 entities
  repo 17cf1fe8: 7 entities     ← 目标仓库，修复前为 0

task-planner docs in Milvus:
   project_doc  任务规划实验室 · 开源许可
   project_doc  任务规划实验室 · 任务
   project_doc  任务规划实验室 · 任务规划实验室
   project_doc  任务规划实验室 · 模拟接口
   project_doc  任务规划实验室 · 环境
   project_doc  任务规划实验室 · 代码入口
   project_doc  任务规划实验室 · 基础检查
```

## 4. 性能优化：query embedding 缓存

### 问题定位

`backend/app/services/rag/vector_store.py:301`：

```python
vector = query_embedding or embed_text(query, settings.embedding_dimensions)
```

每次检索都**现算** query 向量。分层计时显示它占单次检索延迟的 **87.2%（517ms / 592.8ms）**。

`backend/app/services/rag/embeddings.py:132` 上的 `@lru_cache(maxsize=4)` 缓存的是**模型对象**，不是向量 —— 所以并没有省掉计算。

### 优化实现

在 `embeddings.py` 中新增 query embedding 缓存（`_embed_query_cached` / `embed_query` /
`clear_query_embedding_cache` / `query_embedding_cache_info`），按 `(query, dimensions, provider, model)` 缓存向量。

### 验证（交错 A/B 测量）

单次 embedding 耗时波动极大（实测 393–1527ms 区间），**必须交错 A/B/A/B 测量**才能消除机器状态漂移：

| 路径 | 平均 | p50 |
|---|---|---|
| 每次重算 query 向量 | 538.7ms | 529.1ms |
| 缓存命中 | **3.3ms** | 3.1ms |
| **加速** | **163.8×** | |
| Milvus 单侧下限 | 2.5ms | |

缓存命中时检索几乎完全由 Milvus 决定（3.3ms vs 2.5ms 下限）—— 向量计算开销被彻底消除。

> 诚实记录：我一度基于未交错测量的噪声得出「缓存零命中时反而慢 1.895×」的错误结论。
> 针对性诊断（直接对比 `_embed_query_uncached` 与 `_embed_query_cached` 未命中路径）显示包装开销实际为 **-165.4ms（≈0）**，
> 即未命中时无额外惩罚。**这纠正了先前的误判**。

## 5. 最终评测结果

17 用例，top-k=5，同一仓库与索引状态。

| 配置 | Recall@5 | MRR | nDCG | 语义题 Recall | lexical Recall | 平均延迟 |
|---|---|---|---|---|---|---|
| BM25 全文检索 | 0.8824 | 0.7843 | 0.8095 | **0.8000** | 1.0000 | 3.9ms |
| 向量检索 | **1.0000** | 0.8157 | 0.8617 | **1.0000** | 1.0000 | 3128.8ms |
| 混合检索（无重排） | **1.0000** | **0.8824** | **0.9113** | **1.0000** | 1.0000 | **11.2ms** |
| 混合检索 + 重排 | 1.0000 | 0.8402 | 0.8793 | 1.0000 | 1.0000 | 20.3ms |

### 结论

1. **BM25 在语义题上 Recall 只有 0.8000，漏掉 20%；向量检索补全到 1.0000** ——
   这就是双路召回的直接价值，也是「为什么不能只用关键词检索」的量化证据。
2. **混合检索在 MRR 上最优（0.8824，高于向量 0.8157）** —— 两路的排序信号互补。
3. **重排（rerank）在本评测集上反而使 MRR 从 0.8824 降到 0.8402** ——
   这是一个**诚实的负结果**：当前重排器对该语料没有正向收益，不宜盲目声称「加了重排效果更好」。
4. 混合检索延迟 11.2ms，比纯向量检索（3128.8ms）低约 **280×**，缓存优化后达到可交互水平。

## 6. 可复现方式

```bash
cd "C:\Users\HP\Downloads\完整项目源码【DevFlow AI】\DevFlow-AI"
backend/.venv/Scripts/python.exe scripts/run_retrieval_eval.py \
  --suite docs/datas/evals/retrieval-v2.json \
  --top-k 5 --fetch-limit 5 \
  --out docs/datas/evals/result-v2-postfix.json
```

产物：
- 评测集 `docs/datas/evals/retrieval-v2.json`
- 修复后结果 `docs/datas/evals/result-v2-postfix.json`
- 修复前对照 `docs/datas/evals/result-v2-final.json`
- 首版基线 `docs/datas/evals/result-baseline.json`

## 7. 改动文件清单

| 文件 | 改动 |
|---|---|
| `backend/app/services/project_indexing.py` | 修复：启用向量化与 Milvus 同步（缺陷 3） |
| `backend/app/services/rag/embeddings.py` | 新增 query embedding 缓存 |
| `backend/app/services/evals/ragas_runner.py` | 修复：nDCG 未写入返回值（缺陷 1）；source_id 匹配（缺陷 2） |
| `backend/app/services/rag/vector_store.py` | 修改：检索热路径改用带缓存的 `embed_query` |
| `scripts/run_retrieval_eval.py` | 新增：评测脚本 |
| `docs/datas/evals/*.json` | 新增：评测集与结果 |
