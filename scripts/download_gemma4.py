#!/usr/bin/env python3
"""
download_gemma4.py — Télécharge les modèles Gemma4 depuis HuggingFace.
Appelé par le Makefile (target: download-gemma4).

Usage : python3 scripts/download_gemma4.py <dest_dir> <model_file> <mmproj_file>
"""
import os
import sys

from huggingface_hub import hf_hub_download

dest        = sys.argv[1]
model_file  = sys.argv[2]
mmproj_file = sys.argv[3]

token = os.environ.get("HF_TOKEN") or None
repo  = "bartowski/google_gemma-4-e2b-it-GGUF"
files = [model_file, mmproj_file]

for f in files:
    target = os.path.join(dest, f)
    if os.path.exists(target):
        print(f"  ✓ {f} dejà présent")
        continue
    print(f"  Téléchargement : {f} ...")
    try:
        hf_hub_download(
            repo_id=repo,
            filename=f,
            local_dir=dest,
            token=token,
            resume_download=True,
        )
        size = os.path.getsize(target) / 1024 ** 3
        print(f"  ✓ {f} ({size:.2f} GB)")
    except Exception as e:
        print(f"  ✗ Erreur huggingface_hub : {e}", file=sys.stderr)
        sys.exit(1)
