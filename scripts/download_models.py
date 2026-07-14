#!/usr/bin/env python3
"""
下载项目所需的预训练模型。

用法:
    python scripts/download_models.py --model 7b      # 只下载 7B
    python scripts/download_models.py --all            # 下载全部
"""

import argparse
import os
import sys
from pathlib import Path

MODELS = {
    "7b": {
        "name": "Qwen2.5-7B-Instruct GGUF (4-bit)",
        "hf_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "file": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "size_gb": 4.7,
        "target_dir": "models/",
        "required": True,
    },
    "1.5b": {
        "name": "Qwen2.5-1.5B-Instruct",
        "hf_id": "Qwen/Qwen2.5-1.5B-Instruct",
        "size_gb": 3.1,
        "target_dir": "models/",
        "required": True,
    },
    "bge": {
        "name": "bge-small-zh-v1.5 (finetuned)",
        "hf_id": "BAAI/bge-small-zh-v1.5",
        "size_gb": 0.1,
        "target_dir": "models/",
        "required": True,
    },
    "layoutlm": {
        "name": "LayoutLMv3-base",
        "hf_id": "microsoft/layoutlmv3-base",
        "size_gb": 0.5,
        "target_dir": "models/",
        "required": False,  # M4 之前不需要
    },
}

TEST_PDFS = [
    {
        "name": "贵州茅台 2024 年报",
        "code": "600519",
        "url": "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=600519&announcementId=xxxxx",
        "filename": "moutai_2024.pdf",
    },
    {
        "name": "中国平安 2024 年报",
        "code": "601318",
        "url": "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=601318&announcementId=xxxxx",
        "filename": "pingan_2024.pdf",
    },
    {
        "name": "宁德时代 2024 年报",
        "code": "300750",
        "url": "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=300750&announcementId=xxxxx",
        "filename": "catl_2024.pdf",
    },
]


def download_from_hf(hf_id: str, target_dir: str, filename: str | None = None):
    """使用 huggingface_hub 或 snapshot_download 下载模型。"""
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("请先安装 huggingface_hub: pip install huggingface_hub")
        sys.exit(1)

    print(f"下载 {hf_id} -> {target_dir}")
    local = snapshot_download(
        repo_id=hf_id,
        local_dir=os.path.join(target_dir, hf_id.split("/")[-1]),
        local_dir_use_symlinks=False,
        resume_download=True,
    )
    print(f"  完成: {local}")


def main():
    parser = argparse.ArgumentParser(description="下载 FinReport Agent 依赖的模型")
    parser.add_argument("--model", choices=list(MODELS.keys()), help="下载指定模型")
    parser.add_argument("--all", action="store_true", help="下载所有模型")
    parser.add_argument("--required", action="store_true", help="只下载必需模型 (7B/1.5B/bge)")
    parser.add_argument("--list", action="store_true", help="列出所有模型和 PDF 信息")
    args = parser.parse_args()

    if args.list:
        print("\n=== 模型 ===\n")
        for key, m in MODELS.items():
            tag = "必需" if m["required"] else "可选(M4)"
            print(f"  [{key}] {m['name']}")
            print(f"       HuggingFace: {m['hf_id']}")
            print(f"       大小: ~{m['size_gb']} GB")
            print(f"       目标: {m['target_dir']}")
            print(f"       优先级: {tag}")
            print()
        print("=== 测试 PDF ===\n")
        for pdf in TEST_PDFS:
            print(f"  {pdf['name']} ({pdf['code']})")
            print(f"       文件名: data/benchmark/annual_reports/{pdf['filename']}")
            print(f"       来源: 巨潮资讯网 cninfo.com.cn")
            print()
        print("=== 下载命令 ===\n")
        print("  # 下载所有必需模型 (~8 GB)")
        print("  python scripts/download_models.py --required")
        print()
        print("  # 测试 PDF 需手动下载（见 data/benchmark/README.md）")
        return

    if args.model:
        models_to_dl = [args.model]
    elif args.required:
        models_to_dl = [k for k, v in MODELS.items() if v["required"]]
    elif args.all:
        models_to_dl = list(MODELS.keys())
    else:
        print("请指定 --model <name> / --required / --all / --list")
        sys.exit(1)

    total_gb = sum(MODELS[k]["size_gb"] for k in models_to_dl)
    print(f"\n将下载 {len(models_to_dl)} 个模型，约 {total_gb:.1f} GB\n")

    for key in models_to_dl:
        m = MODELS[key]
        print(f"\n{'='*60}")
        print(f"[{key}] {m['name']} (~{m['size_gb']} GB)")
        print(f"{'='*60}")
        download_from_hf(m["hf_id"], m["target_dir"])


if __name__ == "__main__":
    main()
