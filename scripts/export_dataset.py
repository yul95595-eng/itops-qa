# -*- coding: utf-8 -*-
# scripts/export_dataset.py
# 把 Python 里写死的测试集导出成「版本化数据文件」
#
# ============================================================================
# 为什么要有这一步
# ============================================================================
# 原来测试集写在 data/eval/independent_set.py 里:
#   - 加一道题 = 改代码, 没法 diff、没法回滚、不会 Python 的人加不了题
#   - 评测跑完之后, 没人知道"这次的 40 题"和"上次的 40 题"是不是同一批
#   - 换了题却拿旧报告对比, 得出的"提升了 2 个点"是假的
#
# 导出之后:
#   data/eval/datasets/v1.jsonl            <- 唯一真相, 冻结不再改
#   data/eval/datasets/v1.retrieval.jsonl
#   data/eval/datasets/manifest.json       <- 版本登记 + sha256
#
# 每次评测都会把 dataset_sha256 写进结果, 对比时校验一致性。
#
# 用法:
#   python scripts/export_dataset.py              # 导出 v1 (已存在则跳过)
#   python scripts/export_dataset.py --force      # 强制重写 v1 (慎用: v1 冻结)
#   python scripts/export_dataset.py --check      # 校验 py 与 jsonl 是否一致
#   python scripts/export_dataset.py --version v2 # 从当前 py 导出下一版
# ============================================================================
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config

DATASET_DIR = os.path.join(config.PROJECT_ROOT, "data", "eval", "datasets")
MANIFEST = os.path.join(DATASET_DIR, "manifest.json")

# suite 名 -> (来源模块, 源列表名, 文件后缀)
SOURCES = {
    "quality": ("data.eval.independent_set", "INDEPENDENT_CASES", ".jsonl"),
    "retrieval": ("data.eval.retrieval_set", "RETRIEVAL_CASES", ".retrieval.jsonl"),
}


def _load_source(suite):
    """从 python 模块里把 case 列表读出来"""
    import importlib
    mod_name, attr, _ = SOURCES[suite]
    mod = importlib.import_module(mod_name)
    cases = list(getattr(mod, attr))
    return cases


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dump_jsonl(cases):
    """一行一题, 保证稳定排序(按 id)和稳定字段序"""
    ordered = sorted(cases, key=lambda c: c["id"])
    return "".join(
        json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in ordered
    )


def _path(suite, version):
    return os.path.join(DATASET_DIR, f"{version}{SOURCES[suite][2]}")


def _read_manifest():
    if not os.path.exists(MANIFEST):
        return {"schema_version": 1, "current": None, "versions": {}}
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def _write_manifest(m):
    os.makedirs(DATASET_DIR, exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def export(version="v1", force=False):
    """把 py 里的测试集导出成 jsonl, 并登记到 manifest"""
    os.makedirs(DATASET_DIR, exist_ok=True)
    manifest = _read_manifest()
    entry = manifest["versions"].setdefault(version, {})
    created = []

    for suite in SOURCES:
        path = _path(suite, version)
        content = _dump_jsonl(_load_source(suite))
        digest = _sha256(content)

        if os.path.exists(path) and not force:
            old = _sha256(open(path, encoding="utf-8").read())
            if old != digest:
                # 关键保护: v1 已冻结, py 被改过但没人重新导出
                print(f"  [跳过] {os.path.basename(path)} 已存在且内容不同")
                print(f"         若确认要覆盖(会破坏历史对比), 加 --force")
                entry[suite] = dict(
                    file=os.path.basename(path), sha256=old,
                    cases=sum(1 for _ in open(path, encoding="utf-8")),
                    frozen=True, created_at=entry.get(suite, {}).get("created_at", ""),
                    drift_from_source=True,
                )
                continue
            print(f"  [一致] {os.path.basename(path)} sha256={digest[:12]}")
            entry[suite] = dict(
                file=os.path.basename(path), sha256=old,
                cases=sum(1 for _ in open(path, encoding="utf-8")),
                frozen=True,
                created_at=entry.get(suite, {}).get("created_at") or datetime.now().strftime("%Y-%m-%d"),
                drift_from_source=False,
            )
            continue

        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        n = len(content.splitlines())
        entry[suite] = dict(
            file=os.path.basename(path), sha256=digest, cases=n,
            frozen=True, created_at=datetime.now().strftime("%Y-%m-%d"),
            drift_from_source=False,
        )
        created.append(f"{os.path.basename(path)} ({n} 题)")
        print(f"  [写入] {os.path.basename(path)}  {n} 题  sha256={digest[:12]}")

    manifest["current"] = manifest.get("current") or version
    _write_manifest(manifest)
    if created:
        print(f"\n已导出 {version}: " + ", ".join(created))
    return manifest


def check(version=None):
    """校验 py 与 jsonl 是否仍然一致。返回 0=一致, 1=有漂移"""
    manifest = _read_manifest()
    version = version or manifest.get("current")
    if not version:
        print("manifest 里没有可用版本, 先跑 export")
        return 1

    drift = 0
    print(f"校验版本 {version} 与源 .py 的一致性：")
    for suite in SOURCES:
        path = _path(suite, version)
        if not os.path.exists(path):
            print(f"  ❌ {os.path.basename(path)} 不存在")
            drift += 1
            continue
        src_digest = _sha256(_dump_jsonl(_load_source(suite)))
        file_digest = _sha256(open(path, encoding="utf-8").read())
        recorded = manifest["versions"].get(version, {}).get(suite, {}).get("sha256")

        if src_digest != file_digest:
            print(f"  ❌ {os.path.basename(path)}：源 .py 已改，但数据文件没重新导出")
            drift += 1
        elif recorded and recorded != file_digest:
            print(f"  ❌ {os.path.basename(path)}：文件被手工改过（与 manifest 记录不符）")
            drift += 1
        else:
            print(f"  ✅ {os.path.basename(path)} 一致")

    if drift:
        print("\n结论: 有漂移。评测结果不可用于对比——"
              "要么重新导出（新版本号），要么把 .py 的改动撤回。")
        return 1
    print("\n结论: 一致，可以放心用于回归对比。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="测试集版本化导出/校验")
    ap.add_argument("--version", default="v1", help="版本号, 默认 v1")
    ap.add_argument("--force", action="store_true", help="强制覆盖已存在的版本(会破坏历史对比)")
    ap.add_argument("--check", action="store_true", help="只校验, 不导出")
    args = ap.parse_args()

    if args.check:
        sys.exit(check(args.version))

    export(args.version, force=args.force)


if __name__ == "__main__":
    main()
