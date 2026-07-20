#!/usr/bin/env python3
"""
下载项目所需的预训练模型（ModelScope 优先，HuggingFace 镜像备选）。

用法:
    python scripts/download_models.py --model 7b          # 只下载 7B GPTQ
    python scripts/download_models.py --model 1.5b        # 只下载 1.5B
    python scripts/download_models.py --model bge         # 只下载 bge
    python scripts/download_models.py --required          # 下载必需模型（7B/1.5B/bge）
    python scripts/download_models.py --all               # 下载全部（含 LayoutLMv3）
    python scripts/download_models.py --list              # 列出所有模型信息
    python scripts/download_models.py --source hf         # 强制使用 HuggingFace 镜像

技术栈说明:
    - 7B: GPTQ-Int4 量化（与 transformers + auto-gptq 兼容）
    - 1.5B: 原版 fp16（QLoRA 微调基座）
    - bge: 原版（LoRA 微调基座）
    - LayoutLMv3: 原版（M4 表格识别用）

模型存放: E:\项目\FinReport Agent\models\（不入 git）
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
# 模型清单
# ============================================================================

MODELS = {
    "7b": {
        "name": "Qwen2.5-7B-Instruct (GPTQ-Int4)",
        "description": "主推理模型 - ReAct 问答 / 报告生成",
        "modelscope_id": "qwen/Qwen2.5-7B-Instruct-GPTQ-Int4",
        "hf_id": "Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4",
        "size_gb": 4.5,
        "target_subdir": "Qwen2.5-7B-Instruct-GPTQ-Int4",
        "required": True,
        "used_in": "M2+ (推理)",
    },
    "1.5b": {
        "name": "Qwen2.5-1.5B-Instruct (fp16)",
        "description": "财报抽取微调基座 - QLoRA",
        "modelscope_id": "qwen/Qwen2.5-1.5B-Instruct",
        "hf_id": "Qwen/Qwen2.5-1.5B-Instruct",
        "size_gb": 3.1,
        "target_subdir": "Qwen2.5-1.5B-Instruct",
        "required": True,
        "used_in": "M2+ (微调+推理)",
    },
    "bge": {
        "name": "bge-small-zh-v1.5",
        "description": "金融领域 embedding 基座 - LoRA",
        "modelscope_id": "Xorbits/bge-small-zh-v1.5",
        "hf_id": "BAAI/bge-small-zh-v1.5",
        "size_gb": 0.1,
        "target_subdir": "bge-small-zh-v1.5",
        "required": True,
        "used_in": "M2+ (向量检索)",
    },
    "layoutlm": {
        "name": "LayoutLMv3-base",
        "description": "财报表格结构识别 - 全参微调",
        "modelscope_id": "AI-ModelScope/layoutlmv3-base",
        "hf_id": "microsoft/layoutlmv3-base",
        "size_gb": 0.5,
        "target_subdir": "layoutlmv3-base",
        "required": False,
        "used_in": "M4 (表格识别)",
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
        print(f"  如需重新下载，请先删除该目录")
        return True

    # 选择下载源
    if source == "modelscope":
        ok = download_via_modelscope(m["modelscope_id"], target_dir)
    elif source == "hf":
        ok = download_via_hf(m["hf_id"], target_dir)
    else:  # auto: 先 modelscope，失败则回退 hf
        ok = download_via_modelscope(m["modelscope_id"], target_dir)
        if not ok:
            print(f"  [回退] ModelScope 失败，尝试 HuggingFace 镜像...")
            ok = download_via_hf(m["hf_id"], target_dir)

    if ok:
        print(f"  [完成] {target_dir}")
    else:
        print(f"  [失败] 请检查网络或手动下载")
    return ok


# ============================================================================
# 主入口
# ============================================================================

def list_models():
    print("\n=== 模型清单 ===\n")
    print(f"{'Key':<10} {'名称':<40} {'大小':<10} {'阶段':<15} {'优先级'}")
    print("-" * 90)
    for key, m in MODELS.items():
        tag = "必需" if m["required"] else "可选"
        print(f"{key:<10} {m['name']:<40} ~{m['size_gb']}GB{'':<5} {m['used_in']:<15} {tag}")

    print(f"\n存放目录: {MODELS_DIR}")
    total_required = sum(m["size_gb"] for m in MODELS.values() if m["required"])
    total_all = sum(m["size_gb"] for m in MODELS.values())
    print(f"必需模型总量: ~{total_required:.1f} GB")
    print(f"全部模型总量: ~{total_all:.1f} GB")

    print("\n=== 下载命令 ===")
    print("  # 下载所有必需模型 (~8.2 GB)")
    print("  python scripts/download_models.py --required")
    print("\n  # 只下载单个")
    print("  python scripts/download_models.py --model bge")
    print("\n  # 强制使用 HuggingFace 镜像")
    print("  python scripts/download_models.py --required --source hf")


def check_dependencies():
    """检查并提示安装下载依赖"""
    missing = []
    try:
        import modelscope  # noqa: F401
    except ImportError:
        missing.append("modelscope")
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        missing.append("huggingface_hub")

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
            "  python scripts/download_models.py --list          # 查看模型清单",
            "  python scripts/download_models.py --required      # 下载必需模型",
            "  python scripts/download_models.py --model 7b      # 只下载 7B",
            "  python scripts/download_models.py --all --source hf  # 全部用 HF 镜像",
        ]),
    )
    parser.add_argument("--model", choices=list(MODELS.keys()), help="下载指定模型")
    parser.add_argument("--required", action="store_true", help="只下载必需模型 (7B/1.5B/bge)")
    parser.add_argument("--all", action="store_true", help="下载所有模型（含 LayoutLMv3）")
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

    if args.model:
        keys = [args.model]
    elif args.required:
        keys = [k for k, v in MODELS.items() if v["required"]]
    elif args.all:
        keys = list(MODELS.keys())
    else:
        parser.print_help()
        sys.exit(1)

    # 检查依赖
    if not check_dependencies():
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
