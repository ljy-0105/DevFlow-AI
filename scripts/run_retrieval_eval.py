"""检索层效果评测：对同一评测集跑多种检索配置，输出 Recall@K / Precision@K / MRR / nDCG 与延迟。

设计目标：
- 复用 app.services.rag.retrieval.search_similar_documents 与 app.services.evals.ragas_runner._retrieval_metrics，
  不重造指标实现，保证与产品内评测口径一致。
- 只评测检索层（不调用 LLM），因此无需 API key、可离线复现。
- 支持配置对比：fulltext / vector / hybrid(无 rerank) / hybrid(含 rerank)，用于产出优化前后对比数据。

用法：
    python scripts/run_retrieval_eval.py --suite docs/datas/evals/retrieval-baseline.json
    python scripts/run_retrieval_eval.py --suite ... --out docs/datas/evals/result-<name>.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 默认对齐产品运行配置（Milvus + qwen_local 1024 维），这样评测反映真实线上行为。
# 传 --offline 可切到 deterministic embedding + 关闭 Milvus，用于无 GPU / 无网络的复现。
os.environ.setdefault("MILVUS_ENABLED", "true")
os.environ.setdefault("EMBEDDING_PROVIDER", "qwen_local")

from sqlalchemy.orm import Session  # noqa: E402

from app.db.models import Document, Repository  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.evals.ragas_runner import RagEvalCase, _retrieval_metrics  # noqa: E402
from app.services.rag.retrieval import search_similar_documents  # noqa: E402


def _doc_keys(doc: Document) -> list[str]:
    """一个文档可以被多种键命中：标题全名、标题尾段（主题）、元数据文件名。"""
    meta = doc.__dict__.get("metadata") or {}
    keys: list[str] = []
    title = str(doc.title or "")
    if title:
        keys.append(title.casefold())
        for sep in ("·", "|", "/", "："):
            if sep in title:
                keys.append(title.split(sep)[-1].strip().casefold())
    for field in ("filename", "display_title", "source_file"):
        value = Path(str(meta.get(field) or "")).name.casefold()
        if value:
            keys.append(value)
    return [k for k in dict.fromkeys(keys) if k]


def load_suite(path: Path, db: Session, repo: Repository, top_k: int) -> list[RagEvalCase]:
    """把评测集里声明的 source_file 解析成真实 Document UUID，作为期望来源。

    source_file 支持三种写法：`<source_type>/<主题>`、纯主题名、元数据里的文件名。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    documents = db.query(Document).filter(Document.repo_id == repo.id).all()
    by_key: dict[str, list[Document]] = {}
    for doc in documents:
        for key in _doc_keys(doc):
            by_key.setdefault(key, []).append(doc)

    cases: list[RagEvalCase] = []
    kinds: dict[str, str] = {}
    unresolved: list[str] = []
    for item in payload["cases"]:
        source_file = str(item.get("source_file") or "").strip()
        expected: list[str] = []
        if source_file:
            lookup = source_file.casefold()
            if "/" in lookup:
                prefix, _, rest = lookup.partition("/")
                matches = [
                    doc for doc in by_key.get(rest.strip(), [])
                    if str(doc.source_type).casefold() == prefix.strip()
                ]
            else:
                matches = by_key.get(lookup, [])
            if not matches:
                unresolved.append(source_file)
                continue
            for doc in matches:
                # 只用 chunk 级唯一的 documents.id 作为期望标识。
                # documents.source_id 是按「文件路径」生成的（project_indexing 里
                # stable_source_id(repo.id, source_type, relative_path)），同一文件
                # 切出的多个 chunk 会共享同一个 source_id；把它放进期望集会令
                # 「命中任一 chunk」被判为「命中该文件全部 chunk」，使 Recall 恒为 1。
                expected.append(str(doc.id))
        case = RagEvalCase(
            id=str(item.get("id") or ""),
            question=str(item["question"]),
            reference=str(item.get("reference") or ""),
            expected_source_ids=list(dict.fromkeys(expected)),
            source_file=source_file or None,
        )
        cases.append(case)
        kinds[case.id] = str(item.get("kind") or "unspecified")
    if unresolved:
        print(f"[warn] 评测集中 {len(set(unresolved))} 个 source_file 在当前仓库里找不到对应文档，相关用例已跳过：")
        for name in sorted(set(unresolved)):
            print(f"        - {name}")
    load_suite.kinds = kinds  # type: ignore[attr-defined]
    return cases


def run_config(
    db: Session,
    repo: Repository,
    cases: list[RagEvalCase],
    *,
    label: str,
    retrieval_method: str,
    apply_rerank: bool,
    top_k: int,
    fetch_limit: int,
    kinds: dict[str, str] | None = None,
) -> dict:
    rows: list[dict] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        results = search_similar_documents(
            db,
            repo.id,
            case.question,
            limit=fetch_limit,
            retrieval_method=retrieval_method,
            apply_rerank=apply_rerank,
            apply_compression=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        latencies.append(elapsed_ms)
        rows.append({"results": results})

    metrics = _retrieval_metrics(cases, rows, top_k)
    # 按用例给出命中情况，便于定位是哪几条没召回。
    per_case = []
    for case, row in zip(cases, rows, strict=True):
        expected = set(case.expected_source_ids)
        top = row["results"][:top_k]
        hit_ids = [str(r.get("id")) for r in top if str(r.get("id")) in expected]
        first_rank = next(
            (i + 1 for i, r in enumerate(top) if str(r.get("id")) in expected),
            None,
        )
        per_case.append(
            {
                "id": case.id,
                "kind": (kinds or {}).get(case.id, "unspecified"),
                "question": case.question,
                "hit": bool(hit_ids) or not expected,
                "first_hit_rank": first_rank,
                "returned": len(top),
            }
        )

    # 按题型拆分：lexical 考词汇命中，semantic 考语义理解，用来定位两路召回各自的贡献。
    kind_of = kinds or {}
    by_kind: dict[str, dict] = {}
    for kind in sorted({kind_of.get(c.id, "unspecified") for c in cases}):
        picked = [(c, r) for c, r in zip(cases, rows, strict=True) if kind_of.get(c.id, "unspecified") == kind]
        if not picked:
            continue
        picked_cases = [c for c, _ in picked]
        picked_rows = [r for _, r in picked]
        sub = _retrieval_metrics(picked_cases, picked_rows, top_k)
        by_kind[kind] = {"cases": len(picked), **sub}

    return {
        "label": label,
        "retrieval_method": retrieval_method,
        "apply_rerank": apply_rerank,
        "top_k": top_k,
        "fetch_limit": fetch_limit,
        "cases": len(cases),
        "metrics": metrics,
        "by_kind": by_kind,
        "latency_ms": {
            "avg": round(statistics.mean(latencies), 2) if latencies else 0.0,
            "median": round(statistics.median(latencies), 2) if latencies else 0.0,
            "max": round(max(latencies), 2) if latencies else 0.0,
        },
        "hit_rate": round(
            sum(1 for c in per_case if c["hit"]) / len(per_case), 4
        ) if per_case else 0.0,
        "per_case": per_case,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检索层效果评测")
    parser.add_argument("--suite", required=True, help="评测集 JSON 路径")
    parser.add_argument("--repo", help="仓库 full_name，默认取第一个")
    parser.add_argument("--top-k", type=int, default=5, help="计算指标时截断的 K")
    parser.add_argument("--fetch-limit", type=int, default=5, help="每次检索实际返回条数")
    parser.add_argument("--out", help="结果 JSON 输出路径")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="离线模式：deterministic embedding（128 维）且关闭 Milvus，仅评测 BM25 路径",
    )
    args = parser.parse_args()

    if args.offline:
        os.environ["MILVUS_ENABLED"] = "false"
        os.environ["EMBEDDING_PROVIDER"] = "deterministic"
        os.environ["EMBEDDING_DIMENSIONS"] = "128"

    suite_path = Path(args.suite)
    if not suite_path.is_absolute():
        suite_path = (BACKEND_DIR.parent / suite_path).resolve()

    db = SessionLocal()
    try:
        suite_repo = None
        try:
            suite_repo = json.loads(suite_path.read_text(encoding="utf-8")).get("repo")
        except Exception:
            pass
        wanted = args.repo or suite_repo
        repo = (
            db.query(Repository).filter(Repository.full_name == wanted).first()
            if wanted
            else db.query(Repository).first()
        )
        if repo is None:
            print(f"[error] 找不到仓库：{wanted or '(任意)'}")
            return 1
        print(f"仓库：{repo.full_name}  ({repo.id})")

        cases = load_suite(suite_path, db, repo, args.top_k)
        kinds: dict[str, str] = getattr(load_suite, "kinds", {})
        if not cases:
            print("[error] 评测集没有可运行的用例")
            return 1
        print(f"评测集：{suite_path.name}  用例数：{len(cases)}\n")

        configs = [
            ("BM25 全文检索", "fulltext", False),
            ("向量检索", "vector", False),
            ("混合检索（无重排）", "hybrid", False),
            ("混合检索 + 重排", "hybrid", True),
        ]
        results = []
        for label, method, rerank in configs:
            try:
                result = run_config(
                    db, repo, cases,
                    label=label, retrieval_method=method, apply_rerank=rerank,
                    top_k=args.top_k, fetch_limit=args.fetch_limit, kinds=kinds,
                )
            except Exception as exc:  # 向量检索在 Milvus 不可用时会被跳过
                print(f"{label:<20} 跳过：{type(exc).__name__}: {exc}")
                continue
            m = result["metrics"]
            print(
                f"{label:<20} Recall@{args.top_k}={m['recall_at_k']:.4f}  "
                f"Precision@{args.top_k}={m['precision_at_k']:.4f}  "
                f"MRR={m['mrr']:.4f}  nDCG={m['ndcg']:.4f}  "
                f"命中率={result['hit_rate']:.4f}  "
                f"平均延迟={result['latency_ms']['avg']:.1f}ms"
            )
            for kind, km in sorted(result["by_kind"].items()):
                print(
                    f"{'':<20}   └ {kind:<10} n={km['cases']:<3} "
                    f"Recall={km['recall_at_k']:.4f}  MRR={km['mrr']:.4f}  nDCG={km['ndcg']:.4f}"
                )
            results.append(result)

        if not results:
            print("[error] 所有配置都未能执行")
            return 1

        payload = {
            "suite": suite_path.name,
            "repo": repo.full_name,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "embedding_provider": os.environ.get("EMBEDDING_PROVIDER"),
            "milvus_enabled": os.environ.get("MILVUS_ENABLED"),
            "configs": results,
        }
        if args.out:
            out_path = Path(args.out)
            if not out_path.is_absolute():
                out_path = (BACKEND_DIR.parent / out_path).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n结果已写入：{out_path}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())