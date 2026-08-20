#!/usr/bin/env python3
"""
下载项目所需的预训练模型（ModelScope 优先，HuggingFace 镜像备选）。

用法:
    python scripts/download_models.py                 # 下载 bge（唯一本地模型）
    python scripts/download_models.py --list          # 查看模型信息
    python scripts/download_models.py --source hf     # 强制使用 HuggingFace 镜像

2026-08-16 起 LLM 推理走 DeepSeek API（decision record），本地仅保留
bge-small-zh-v1.5 embedding（~95MB，CPU）；原 7B/1.5B/LayoutLMv3 条目
已随 M4 方向变更作废删除。

模型存放: 项目根目录 models/（不入 git）；容器部署时经 docker cp 复制进
finreport-models 卷（见 deploy/.env.example）。
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# 项目根目录（脚本位于 scripts/ 下，根目录是上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"
MODELS_DIR.mkdir(exist_ok=True)


# ============================================================================
# 模型清单（M4.09：仅 bge-small-zh-v1.5）
# ============================================================================

MODELS = {
    "bge": {
        "name": "bge-small-zh-v1.5",
        "description": "本地 embedding（512 维，Milvus fin_kb 配套）",
        "modelscope_id": "Xorbits/bge-small-zh-v1.5",
        "hf_id": "BAAI/bge-small-zh-v1.5",
        "size_gb": 0.1,
        "target_subdir": "bge-small-zh-v1.5",
        "used_in": "M4.07+ (embed / M5 RAG)",
    },
}


# ============================================================================
# 下载函数
# ============================================================================

def download_via_modelscope(model_id: str, target_dir: Path) -> bool:
    """使用 modelscope CLI 下载（国内推荐）"""
    print(f"  [ModelScope] {model_id}")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "modelscope", "download",
        "--model", model_id,
        "--local_dir", str(target_dir),
    ]
    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode == 0
    except FileNotFoundError:
        print("  [错误] modelscope 未安装，请运行: pip install modelscope")
        return False


def download_via_hf(hf_id: str, target_dir: Path) -> bool:
    """使用 huggingface_hub 下载（通过 hf-mirror 镜像）"""
    print(f"  [HF-Mirror] {hf_id}")
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  [错误] huggingface_hub 未安装，请运行: pip install huggingface_hub")
        return False

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            repo_id=hf_id,
            local_dir=str(target_dir),
            resume_download=True,
        )
        return True
    except Exception as e:
        print(f"  [错误] 下载失败: {e}")
        return False


def download_model(key: str, source: str = "auto") -> bool:
    """下载单个模型"""
    m = MODELS[key]
    target_dir = MODELS_DIR / m["target_subdir"]
    print(f"\n{'=' * 70}")
    print(f"[{key}] {m['name']}")
    print(f"     用途: {m['description']}")
    print(f"     阶段: {m['used_in']}")
    print(f"     大小: ~{m['size_gb']} GB")
    print(f"     目标: {target_dir}")
    print(f"{'=' * 70}")

    if target_dir.exists() and any(target_dir.iterdir()):
        print(f"  [跳过] 目录已存在且非空: {target_dir}")
        print("  如需重新下载，请先删除该目录")
        return True

    # 选择下载源
    if source == "modelscope":
        ok = download_via_modelscope(m["modelscope_id"], target_dir)
    elif source == "hf":
        ok = download_via_hf(m["hf_id"], target_dir)
    else:  # auto: 先 modelscope，失败则回退 hf
        ok = download_via_modelscope(m["modelscope_id"], target_dir)
        if not ok:
            print("  [回退] ModelScope 失败，尝试 HuggingFace 镜像...")
            ok = download_via_hf(m["hf_id"], target_dir)

    if ok:
        print(f"  [完成] {target_dir}")
    else:
        print("  [失败] 请检查网络或手动下载")
    return ok


# ============================================================================
# 主入口
# ============================================================================

def list_models():
    print("\n=== 模型清单（2026-08-16 起 LLM 走 DeepSeek API，本地仅 embedding）===\n")
    print(f"{'Key':<10} {'名称':<25} {'大小':<10} {'阶段'}")
    print("-" * 70)
    for key, m in MODELS.items():
        print(f"{key:<10} {m['name']:<25} ~{m['size_gb']}GB     {m['used_in']}")

    print(f"\n存放目录: {MODELS_DIR}")
    total_gb = sum(m["size_gb"] for m in MODELS.values())
    print(f"模型总量: ~{total_gb:.1f} GB")

    print("\n=== 下载命令 ===")
    print("  # 下载全部本地模型（当前仅 bge，~0.1GB）")
    print("  python scripts/download_models.py")
    print("\n  # 强制使用 HuggingFace 镜像")
    print("  python scripts/download_models.py --source hf")
    print("\n=== 容器部署（模型进 finreport-models 卷）===")
    print("  docker cp ./models/. finreport-models:/models/")


def check_dependencies(source: str = "auto") -> bool:
    """检查并提示安装下载依赖（按 source 只检查实际需要的客户端）。"""
    needed = []
    if source in ("auto", "modelscope"):
        needed.append("modelscope")
    if source in ("auto", "hf"):
        needed.append("huggingface_hub")

    missing = []
    for pkg in needed:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        print("\n[提示] 以下依赖未安装:")
        for pkg in missing:
            print(f"  - {pkg}")
        print("\n建议安装:")
        print(f"  pip install {' '.join(missing)}")
        print("\n或使用 conda:")
        print(f"  conda run -n env1-py311 pip install {' '.join(missing)}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="下载 FinReport Agent 依赖的预训练模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "示例:",
            "  python scripts/download_models.py              # 下载 bge",
            "  python scripts/download_models.py --list       # 查看模型清单",
            "  python scripts/download_models.py --source hf  # 全部用 HF 镜像",
        ]),
    )
    parser.add_argument("--model", choices=list(MODELS.keys()), help="下载指定模型")
    parser.add_argument("--list", action="store_true", help="列出所有模型信息")
    parser.add_argument(
        "--source",
        choices=["auto", "modelscope", "hf"],
        default="auto",
        help="下载源: auto(默认) / modelscope / hf",
    )
    args = parser.parse_args()

    if args.list:
        list_models()
        return

    # M4.09：仅剩 bge 一个模型，无参数默认下载全部（即 bge）。
    keys = [args.model] if args.model else list(MODELS.keys())

    # 检查依赖（按下载源检查实际需要的客户端）
    if not check_dependencies(args.source):
        sys.exit(1)

    total_gb = sum(MODELS[k]["size_gb"] for k in keys)
    print(f"\n将下载 {len(keys)} 个模型，共 ~{total_gb:.1f} GB")
    print(f"存放目录: {MODELS_DIR}")
    print(f"下载源: {args.source}")

    success = []
    failed = []
    for key in keys:
        ok = download_model(key, source=args.source)
        (success if ok else failed).append(key)

    print(f"\n{'=' * 70}")
    print(f"下载完成: {len(success)}/{len(keys)}")
    if success:
        print(f"  成功: {', '.join(success)}")
    if failed:
        print(f"  失败: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
