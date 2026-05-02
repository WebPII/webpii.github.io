#!/usr/bin/env python3
"""
PII Detection Benchmark Harness.

Runs multiple PII detection methods on ground truth screenshots and compares
accuracy (precision, recall, F1, IoU) and latency across methods.
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

from methods.base import (
    PIIDetector,
    PIIDetection,
    BBox,
    DetectionResult,
    compute_iou,
    compute_containment,
)


# Ground truth directory
SCREENSHOTS_DIR = Path(__file__).parent.parent / "ui_reproducer" / "screenshots"


@dataclass
class GroundTruth:
    """Ground truth PII elements from a screenshot."""
    image_path: Path
    image_id: str
    pii_elements: list[dict]  # {key, value, bbox, visible, element_type}
    pii_containers: list[dict]

    @classmethod
    def from_json(cls, json_path: Path) -> Optional["GroundTruth"]:
        """Load ground truth from JSON file."""
        try:
            with open(json_path) as f:
                data = json.load(f)

            # Get image path
            image_id = json_path.stem
            image_path = json_path.with_suffix(".png")

            if not image_path.exists():
                return None

            return cls(
                image_path=image_path,
                image_id=image_id,
                pii_elements=data.get("pii_elements", []),
                pii_containers=data.get("pii_containers", [])
            )
        except Exception as e:
            print(f"Error loading {json_path}: {e}")
            return None


@dataclass
class MatchResult:
    """Result of matching a detection to ground truth."""
    detection: PIIDetection
    gt_element: Optional[dict]
    iou: float
    containment: float
    matched: bool


@dataclass
class ImageMetrics:
    """Metrics for a single image."""
    image_id: str
    method: str
    num_gt: int
    num_detected: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    avg_iou: float
    latency_ms: float
    matches: list[MatchResult] = field(default_factory=list)


@dataclass
class AggregateMetrics:
    """Aggregate metrics across all images."""
    method: str
    total_gt: int
    total_detected: int
    total_tp: int
    total_fp: int
    total_fn: int
    precision: float
    recall: float
    f1: float
    avg_iou: float
    avg_latency_ms: float
    per_image: list[ImageMetrics] = field(default_factory=list)
    per_pii_type: dict = field(default_factory=dict)


def normalize_gt_type(key: str) -> str:
    """Convert ground truth key to normalized PII type."""
    type_map = {
        "PII_CARD_NUMBER": "CARD",
        "PII_CARD_CVV": "CVV",
        "PII_CARD_EXPIRY_MONTH": "DATE",
        "PII_CARD_EXPIRY_YEAR": "DATE",
        "PII_FIRSTNAME": "NAME",
        "PII_LASTNAME": "NAME",
        "PII_FULLNAME": "NAME",
        "PII_EMAIL": "EMAIL",
        "PII_PHONE": "PHONE",
        "PII_STREET": "ADDRESS",
        "PII_CITY": "ADDRESS",
        "PII_STATE": "ADDRESS",
        "PII_STATE_ABBR": "ADDRESS",
        "PII_POSTCODE": "ADDRESS",
        "PII_ADDRESS": "ADDRESS",
        "PII_COUNTRY": "ADDRESS",
        "PII_ACCOUNT_ID": "ID",
        "PII_SSN": "SSN",
    }
    return type_map.get(key, "OTHER")


def match_detections(
    detections: list[PIIDetection],
    gt_elements: list[dict],
    iou_threshold: float = 0.3,
    containment_threshold: float = 0.5
) -> tuple[list[MatchResult], int, int, int]:
    """
    Match detections to ground truth elements.

    Returns:
        matches: List of MatchResult
        true_positives: Number of GT elements matched
        false_positives: Number of detections not matching GT
        false_negatives: Number of GT elements not detected
    """
    matches = []
    matched_gt_indices = set()

    for detection in detections:
        best_match = None
        best_score = 0
        best_gt_idx = -1

        for i, gt in enumerate(gt_elements):
            if i in matched_gt_indices:
                continue

            # Only match visible elements
            if not gt.get("visible", True):
                continue

            gt_bbox = BBox(
                x=gt["bbox"]["x"],
                y=gt["bbox"]["y"],
                width=gt["bbox"]["width"],
                height=gt["bbox"]["height"]
            )

            iou = compute_iou(detection.bbox, gt_bbox)
            containment = compute_containment(detection.bbox, gt_bbox)

            # Consider match if either IoU or containment is high enough
            score = max(iou, containment)
            if score > best_score:
                best_score = score
                best_match = gt
                best_gt_idx = i

        is_matched = best_score >= min(iou_threshold, containment_threshold)
        if is_matched and best_gt_idx >= 0:
            matched_gt_indices.add(best_gt_idx)

        matches.append(MatchResult(
            detection=detection,
            gt_element=best_match if is_matched else None,
            iou=compute_iou(detection.bbox, BBox(**best_match["bbox"])) if best_match else 0,
            containment=compute_containment(detection.bbox, BBox(**best_match["bbox"])) if best_match else 0,
            matched=is_matched
        ))

    # Count visible GT elements
    visible_gt = [gt for gt in gt_elements if gt.get("visible", True)]

    true_positives = len(matched_gt_indices)
    false_positives = len([m for m in matches if not m.matched])
    false_negatives = len(visible_gt) - true_positives

    return matches, true_positives, false_positives, false_negatives


def compute_image_metrics(
    result: DetectionResult,
    gt: GroundTruth,
    iou_threshold: float = 0.3
) -> ImageMetrics:
    """Compute metrics for a single image."""
    matches, tp, fp, fn = match_detections(
        result.detections,
        gt.pii_elements,
        iou_threshold=iou_threshold
    )

    visible_gt = [e for e in gt.pii_elements if e.get("visible", True)]
    num_gt = len(visible_gt)
    num_detected = len(result.detections)

    precision = tp / num_detected if num_detected > 0 else 0
    recall = tp / num_gt if num_gt > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    # Average IoU of matched detections
    matched_ious = [m.iou for m in matches if m.matched]
    avg_iou = sum(matched_ious) / len(matched_ious) if matched_ious else 0

    return ImageMetrics(
        image_id=gt.image_id,
        method=result.method,
        num_gt=num_gt,
        num_detected=num_detected,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        avg_iou=avg_iou,
        latency_ms=result.latency_ms,
        matches=matches
    )


def aggregate_metrics(image_metrics: list[ImageMetrics], method: str) -> AggregateMetrics:
    """Aggregate metrics across multiple images."""
    total_gt = sum(m.num_gt for m in image_metrics)
    total_detected = sum(m.num_detected for m in image_metrics)
    total_tp = sum(m.true_positives for m in image_metrics)
    total_fp = sum(m.false_positives for m in image_metrics)
    total_fn = sum(m.false_negatives for m in image_metrics)

    precision = total_tp / total_detected if total_detected > 0 else 0
    recall = total_tp / total_gt if total_gt > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    # Average IoU across all matched detections
    all_ious = []
    for m in image_metrics:
        all_ious.extend([match.iou for match in m.matches if match.matched])
    avg_iou = sum(all_ious) / len(all_ious) if all_ious else 0

    avg_latency = sum(m.latency_ms for m in image_metrics) / len(image_metrics) if image_metrics else 0

    return AggregateMetrics(
        method=method,
        total_gt=total_gt,
        total_detected=total_detected,
        total_tp=total_tp,
        total_fp=total_fp,
        total_fn=total_fn,
        precision=precision,
        recall=recall,
        f1=f1,
        avg_iou=avg_iou,
        avg_latency_ms=avg_latency,
        per_image=image_metrics
    )


def load_ground_truths(screenshots_dir: Path, single_id: Optional[str] = None) -> list[GroundTruth]:
    """Load ground truth data from screenshots directory."""
    ground_truths = []

    for json_path in sorted(screenshots_dir.glob("*.json")):
        if json_path.name == "manifest.json":
            continue

        if single_id and json_path.stem != single_id:
            continue

        gt = GroundTruth.from_json(json_path)
        if gt:
            ground_truths.append(gt)

    return ground_truths


def get_detector(method: str) -> PIIDetector:
    """Get detector instance by method name."""
    try:
        if method == "presidio":
            from methods.presidio_detector import PresidioDetector
            return PresidioDetector()
        if method == "llm":
            from methods.llm_detector import LLMDetector
            return LLMDetector()
        if method == "paddle":
            from methods.paddle_detector import PaddleDetector
            return PaddleDetector()
        if method == "paddle-llm":
            from methods.paddle_detector import PaddleLLMDetector
            return PaddleLLMDetector()
        if method == "tesseract":
            from methods.tesseract_detector import TesseractDetector
            return TesseractDetector()
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency for method '{method}'. Install prediction dependencies with:\n"
            f"  pip install -r predict/requirements.txt"
        ) from exc

    available = ["presidio", "llm", "paddle", "paddle-llm", "tesseract"]
    raise ValueError(f"Unknown method: {method}. Available: {available}")


def run_benchmark(
    methods: list[str],
    ground_truths: list[GroundTruth],
    output_dir: Optional[Path] = None
) -> dict[str, AggregateMetrics]:
    """Run benchmark across all methods and images."""
    results = {}

    for method in methods:
        print(f"\n{'='*60}")
        print(f"Running {method}...")
        print(f"{'='*60}")

        try:
            detector = get_detector(method)
        except Exception as e:
            print(f"Failed to initialize {method}: {e}")
            continue

        image_metrics = []

        for gt in ground_truths:
            print(f"  Processing {gt.image_id}...", end=" ")
            sys.stdout.flush()

            try:
                result = detector.detect(gt.image_path)
                metrics = compute_image_metrics(result, gt)
                image_metrics.append(metrics)

                print(f"P={metrics.precision:.2f} R={metrics.recall:.2f} F1={metrics.f1:.2f} "
                      f"({metrics.true_positives}TP/{metrics.false_positives}FP/{metrics.false_negatives}FN) "
                      f"{metrics.latency_ms:.0f}ms")

            except Exception as e:
                print(f"ERROR: {e}")

        if image_metrics:
            results[method] = aggregate_metrics(image_metrics, method)

    return results


def print_results_table(results: dict[str, AggregateMetrics]):
    """Print a formatted results table."""
    print("\n")
    print("╔" + "═"*70 + "╗")
    print("║" + "PII Detection Benchmark Results".center(70) + "║")
    print("╠" + "═"*14 + "╦" + "═"*10 + "╦" + "═"*10 + "╦" + "═"*10 + "╦" + "═"*10 + "╦" + "═"*12 + "╣")
    print("║" + "Method".center(14) + "║" + "Precision".center(10) + "║" + "Recall".center(10) +
          "║" + "F1".center(10) + "║" + "IoU".center(10) + "║" + "Latency(ms)".center(12) + "║")
    print("╠" + "═"*14 + "╬" + "═"*10 + "╬" + "═"*10 + "╬" + "═"*10 + "╬" + "═"*10 + "╬" + "═"*12 + "╣")

    for method, metrics in sorted(results.items(), key=lambda x: x[1].f1, reverse=True):
        print(
            "║" + method.center(14) +
            "║" + f"{metrics.precision*100:.1f}%".center(10) +
            "║" + f"{metrics.recall*100:.1f}%".center(10) +
            "║" + f"{metrics.f1*100:.1f}%".center(10) +
            "║" + f"{metrics.avg_iou*100:.1f}%".center(10) +
            "║" + f"{metrics.avg_latency_ms:,.0f}".center(12) + "║"
        )

    print("╚" + "═"*14 + "╩" + "═"*10 + "╩" + "═"*10 + "╩" + "═"*10 + "╩" + "═"*10 + "╩" + "═"*12 + "╝")


def visualize_detections(
    image_path: Path,
    detections: list[PIIDetection],
    gt_elements: list[dict],
    output_path: Path
):
    """Create visualization of detections vs ground truth."""
    image = Image.open(image_path)
    draw = ImageDraw.Draw(image)

    # Draw ground truth boxes in green
    for gt in gt_elements:
        if not gt.get("visible", True):
            continue
        bbox = gt["bbox"]
        draw.rectangle(
            [bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"]],
            outline="green",
            width=2
        )

    # Draw detection boxes in red
    for det in detections:
        x1, y1, x2, y2 = det.bbox.to_xyxy()
        draw.rectangle([x1, y1, x2, y2], outline="red", width=2)
        draw.text((x1, y1 - 15), f"{det.pii_type}", fill="red")

    image.save(output_path)


def save_results(results: dict[str, AggregateMetrics], output_dir: Path):
    """Save results to JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)

    output = {}
    for method, metrics in results.items():
        output[method] = {
            "precision": metrics.precision,
            "recall": metrics.recall,
            "f1": metrics.f1,
            "avg_iou": metrics.avg_iou,
            "avg_latency_ms": metrics.avg_latency_ms,
            "total_gt": metrics.total_gt,
            "total_detected": metrics.total_detected,
            "total_tp": metrics.total_tp,
            "total_fp": metrics.total_fp,
            "total_fn": metrics.total_fn,
            "per_image": [
                {
                    "image_id": m.image_id,
                    "precision": m.precision,
                    "recall": m.recall,
                    "f1": m.f1,
                    "avg_iou": m.avg_iou,
                    "latency_ms": m.latency_ms,
                    "num_gt": m.num_gt,
                    "num_detected": m.num_detected,
                }
                for m in metrics.per_image
            ]
        }

    with open(output_dir / "benchmark_results.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to {output_dir / 'benchmark_results.json'}")


def main():
    parser = argparse.ArgumentParser(description="PII Detection Benchmark")
    parser.add_argument(
        "--methods",
        type=str,
        default="all",
        help="Comma-separated list of methods to run (presidio,llm,paddle,paddle-llm,tesseract) or 'all'"
    )
    parser.add_argument(
        "--single",
        type=str,
        help="Run on a single image ID (e.g., '0002')"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output directory for results"
    )
    parser.add_argument(
        "--visualize",
        action="store_true",
        help="Generate visualization images"
    )
    parser.add_argument(
        "--screenshots-dir",
        type=str,
        default=str(SCREENSHOTS_DIR),
        help="Path to screenshots directory"
    )

    args = parser.parse_args()

    # Parse methods
    all_methods = ["presidio", "llm", "paddle", "paddle-llm", "tesseract"]
    if args.methods == "all":
        methods = all_methods
    else:
        methods = [m.strip() for m in args.methods.split(",")]

    # Load ground truth
    screenshots_dir = Path(args.screenshots_dir)
    print(f"Loading ground truth from {screenshots_dir}...")
    ground_truths = load_ground_truths(screenshots_dir, args.single)

    if not ground_truths:
        print("No ground truth data found!")
        return

    print(f"Found {len(ground_truths)} images")

    # Run benchmark
    results = run_benchmark(methods, ground_truths)

    # Print results
    print_results_table(results)

    # Save results
    if args.output:
        save_results(results, Path(args.output))

    # Generate visualizations
    if args.visualize and args.output:
        output_dir = Path(args.output) / "visualizations"
        output_dir.mkdir(parents=True, exist_ok=True)

        for method, metrics in results.items():
            for img_metrics in metrics.per_image:
                gt = next((g for g in ground_truths if g.image_id == img_metrics.image_id), None)
                if gt:
                    detections = [m.detection for m in img_metrics.matches]
                    output_path = output_dir / f"{img_metrics.image_id}_{method}.png"
                    visualize_detections(gt.image_path, detections, gt.pii_elements, output_path)

        print(f"Visualizations saved to {output_dir}")


if __name__ == "__main__":
    main()
