#!/usr/bin/env python3
"""Download PII datasets from HuggingFace."""

from datasets import load_dataset
import os

DATASETS = [
    {
        "name": "PANORAMA",
        "hf_path": "srirxml/PANORAMA",
        "local_path": "data/text_pii/panorama",
        "gated": True,
    },
    {
        "name": "NVIDIA Nemotron-PII",
        "hf_path": "nvidia/Nemotron-PII",
        "local_path": "data/text_pii/nemotron-pii",
        "gated": False,
    },
    {
        "name": "Gretel Finance Multilingual",
        "hf_path": "gretelai/synthetic_pii_finance_multilingual",
        "local_path": "data/text_pii/gretel-finance",
        "gated": False,
    },
]


def download_all():
    for ds in DATASETS:
        print(f"\n{'='*60}")
        print(f"Downloading {ds['name']}...")
        print(f"{'='*60}")

        os.makedirs(ds["local_path"], exist_ok=True)

        try:
            dataset = load_dataset(ds["hf_path"])
            dataset.save_to_disk(ds["local_path"])
            print(f"Saved to {ds['local_path']}")
            print(f"Splits: {list(dataset.keys())}")
            for split, data in dataset.items():
                print(f"  {split}: {len(data):,} rows")
        except Exception as e:
            if "gated" in str(e).lower():
                print(f"ERROR: {ds['name']} is gated. Run: huggingface-cli login")
            else:
                print(f"ERROR: {e}")


if __name__ == "__main__":
    download_all()
