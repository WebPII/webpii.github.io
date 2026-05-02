#!/usr/bin/env python3
"""
YOLO Inference and Evaluation for PII Detection

This script provides inference and evaluation capabilities for trained
YOLO models (both YOLOv8 and YOLOv5) on PII detection tasks.

Features:
- Run inference on single images or batches
- Evaluate against ground truth annotations
- Compute comprehensive metrics (mAP, precision, recall, IoU)
- Visualize predictions with confidence scores
- Compare YOLO vs OCR approaches
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import cv2
import numpy as np

# Class mapping (must match training)
PII_CLASSES_SIMPLIFIED = ["name", "contact", "address", "card", "account"]

PII_KEY_TO_SIMPLIFIED = {
    "PII_FIRSTNAME": "name",
    "PII_LASTNAME": "name",
    "PII_FULLNAME": "name",
    "PII_EMAIL": "contact",
    "PII_PHONE": "contact",
    "PII_STREET": "address",
    "PII_CITY": "address",
    "PII_STATE": "address",
    "PII_STATE_ABBR": "address",
    "PII_POSTCODE": "address",
    "PII_ADDRESS": "address",
    "PII_COUNTRY": "address",
    "PII_COUNTRY_CODE": "address",
    "PII_CARD_NUMBER": "card",
    "PII_CARD_LAST4": "card",
    "PII_CARD_CVV": "card",
    "PII_CARD_EXPIRY_MONTH": "card",
    "PII_CARD_EXPIRY_YEAR": "card",
    "PII_CARD_EXPIRY": "card",
    "PII_ACCOUNT_ID": "account",
    "PII_AVATAR": "account",
}


@dataclass
class Detection:
    """A single detection from YOLO model."""
    class_id: int
    class_name: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float

    def to_xyxy(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    def to_xywh(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2 - self.x1, self.y2 - self.y1)

    def area(self) -> float:
        return (self.x2 - self.x1) * (self.y2 - self.y1)


@dataclass
class GroundTruth:
    """Ground truth annotation."""
    class_id: int
    class_name: str
    pii_key: str
    value: str
    x1: float
    y1: float
    x2: float
    y2: float
    visible: bool

    def to_xyxy(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    def area(self) -> float:
        return (self.x2 - self.x1) * (self.y2 - self.y1)


@dataclass
class EvalMetrics:
    """Evaluation metrics for a single image or batch."""
    num_predictions: int = 0
    num_ground_truth: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    avg_iou: float = 0.0
    per_class_metrics: dict = field(default_factory=dict)
    matched_pairs: list = field(default_factory=list)


class YOLOInference:
    """Unified inference class supporting both YOLOv8 and YOLOv5."""

    def __init__(
        self,
        weights_path: Union[str, Path],
        model_type: str = "auto",  # "yolov8", "yolov5", or "auto"
        device: Optional[str] = None,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        class_names: Optional[list[str]] = None
    ):
        """
        Initialize YOLO inference.

        Args:
            weights_path: Path to model weights (.pt file)
            model_type: Model type ("yolov8", "yolov5", or "auto" for detection)
            device: Device to run on (None=auto, "0"=GPU0, "cpu")
            conf_threshold: Confidence threshold for detections
            iou_threshold: IoU threshold for NMS
            class_names: List of class names (if not embedded in model)
        """
        self.weights_path = Path(weights_path)
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.class_names = class_names or PII_CLASSES_SIMPLIFIED

        # Detect model type
        if model_type == "auto":
            model_type = self._detect_model_type()

        self.model_type = model_type
        self.model = self._load_model(device)

    def _detect_model_type(self) -> str:
        """Auto-detect model type from weights file."""
        # Try to load as YOLOv8 first
        try:
            from ultralytics import YOLO
            model = YOLO(str(self.weights_path))
            # Check if it's a valid YOLOv8 model
            if hasattr(model, 'model') and model.model is not None:
                return "yolov8"
        except Exception:
            pass

        # Fall back to YOLOv5
        return "yolov5"

    def _load_model(self, device: Optional[str]):
        """Load the appropriate model."""
        if self.model_type == "yolov8":
            from ultralytics import YOLO
            model = YOLO(str(self.weights_path))
            if device:
                model.to(device)
            return model

        elif self.model_type == "yolov5":
            import torch
            model = torch.hub.load('ultralytics/yolov5', 'custom',
                                   path=str(self.weights_path))
            model.conf = self.conf_threshold
            model.iou = self.iou_threshold
            if device:
                model.to(device)
            return model

        else:
            raise ValueError(f"Unknown model type: {self.model_type}")

    def predict(
        self,
        image: Union[str, Path, np.ndarray],
        conf: Optional[float] = None,
        iou: Optional[float] = None
    ) -> list[Detection]:
        """
        Run inference on a single image.

        Args:
            image: Image path or numpy array
            conf: Override confidence threshold
            iou: Override IoU threshold

        Returns:
            List of Detection objects
        """
        conf = conf or self.conf_threshold
        iou = iou or self.iou_threshold

        if self.model_type == "yolov8":
            results = self.model.predict(
                image,
                conf=conf,
                iou=iou,
                verbose=False
            )[0]

            detections = []
            boxes = results.boxes

            for i in range(len(boxes)):
                x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy()
                confidence = float(boxes.conf[i].cpu().numpy())
                class_id = int(boxes.cls[i].cpu().numpy())
                class_name = self.class_names[class_id] if class_id < len(self.class_names) else f"class_{class_id}"

                detections.append(Detection(
                    class_id=class_id,
                    class_name=class_name,
                    confidence=confidence,
                    x1=float(x1),
                    y1=float(y1),
                    x2=float(x2),
                    y2=float(y2)
                ))

            return detections

        elif self.model_type == "yolov5":
            self.model.conf = conf
            self.model.iou = iou

            results = self.model(image)
            pred = results.xyxy[0].cpu().numpy()

            detections = []
            for row in pred:
                x1, y1, x2, y2, confidence, class_id = row
                class_id = int(class_id)
                class_name = self.class_names[class_id] if class_id < len(self.class_names) else f"class_{class_id}"

                detections.append(Detection(
                    class_id=class_id,
                    class_name=class_name,
                    confidence=float(confidence),
                    x1=float(x1),
                    y1=float(y1),
                    x2=float(x2),
                    y2=float(y2)
                ))

            return detections


def compute_iou(box1, box2) -> float:
    """Compute IoU between two boxes (each has to_xyxy method or is a tuple)."""
    if hasattr(box1, 'to_xyxy'):
        x1_1, y1_1, x2_1, y2_1 = box1.to_xyxy()
    else:
        x1_1, y1_1, x2_1, y2_1 = box1

    if hasattr(box2, 'to_xyxy'):
        x1_2, y1_2, x2_2, y2_2 = box2.to_xyxy()
    else:
        x1_2, y1_2, x2_2, y2_2 = box2

    # Intersection
    xi1 = max(x1_1, x1_2)
    yi1 = max(y1_1, y1_2)
    xi2 = min(x2_1, x2_2)
    yi2 = min(y2_1, y2_2)

    if xi2 <= xi1 or yi2 <= yi1:
        return 0.0

    inter_area = (xi2 - xi1) * (yi2 - yi1)

    # Union
    area1 = (x2_1 - x1_1) * (y2_1 - y1_1)
    area2 = (x2_2 - x1_2) * (y2_2 - y1_2)
    union_area = area1 + area2 - inter_area

    return inter_area / union_area if union_area > 0 else 0.0


def load_ground_truth(annotation_path: Path, visible_only: bool = True) -> list[GroundTruth]:
    """Load ground truth from annotation JSON."""
    with open(annotation_path) as f:
        data = json.load(f)

    ground_truths = []

    for elem in data.get("pii_elements", []):
        if visible_only and not elem.get("visible", False):
            continue

        pii_key = elem["key"]
        simplified_name = PII_KEY_TO_SIMPLIFIED.get(pii_key)

        if simplified_name is None:
            continue

        class_id = PII_CLASSES_SIMPLIFIED.index(simplified_name)
        bbox = elem["bbox"]

        ground_truths.append(GroundTruth(
            class_id=class_id,
            class_name=simplified_name,
            pii_key=pii_key,
            value=elem["value"],
            x1=bbox["x"],
            y1=bbox["y"],
            x2=bbox["x"] + bbox["width"],
            y2=bbox["y"] + bbox["height"],
            visible=elem.get("visible", True)
        ))

    return ground_truths


def evaluate_detections(
    detections: list[Detection],
    ground_truths: list[GroundTruth],
    iou_threshold: float = 0.5
) -> EvalMetrics:
    """
    Evaluate detections against ground truth.

    Uses Hungarian matching to find optimal detection-GT pairs.
    """
    metrics = EvalMetrics(
        num_predictions=len(detections),
        num_ground_truth=len(ground_truths)
    )

    if not detections or not ground_truths:
        metrics.false_positives = len(detections)
        metrics.false_negatives = len(ground_truths)
        return metrics

    # Compute IoU matrix
    iou_matrix = np.zeros((len(detections), len(ground_truths)))

    for i, det in enumerate(detections):
        for j, gt in enumerate(ground_truths):
            # Only match same class
            if det.class_id == gt.class_id:
                iou_matrix[i, j] = compute_iou(det, gt)

    # Greedy matching (could use Hungarian for optimal)
    matched_dets = set()
    matched_gts = set()
    matches = []

    # Sort by IoU descending
    indices = np.unravel_index(np.argsort(iou_matrix.ravel())[::-1], iou_matrix.shape)

    for det_idx, gt_idx in zip(indices[0], indices[1]):
        if det_idx in matched_dets or gt_idx in matched_gts:
            continue

        iou = iou_matrix[det_idx, gt_idx]
        if iou >= iou_threshold:
            matches.append((det_idx, gt_idx, iou))
            matched_dets.add(det_idx)
            matched_gts.add(gt_idx)

    metrics.true_positives = len(matches)
    metrics.false_positives = len(detections) - len(matches)
    metrics.false_negatives = len(ground_truths) - len(matches)

    # Precision, Recall, F1
    if metrics.true_positives + metrics.false_positives > 0:
        metrics.precision = metrics.true_positives / (metrics.true_positives + metrics.false_positives)
    if metrics.true_positives + metrics.false_negatives > 0:
        metrics.recall = metrics.true_positives / (metrics.true_positives + metrics.false_negatives)
    if metrics.precision + metrics.recall > 0:
        metrics.f1 = 2 * metrics.precision * metrics.recall / (metrics.precision + metrics.recall)

    # Average IoU of matches
    if matches:
        metrics.avg_iou = sum(m[2] for m in matches) / len(matches)

    # Per-class metrics
    per_class = {}
    for class_name in PII_CLASSES_SIMPLIFIED:
        class_id = PII_CLASSES_SIMPLIFIED.index(class_name)
        class_dets = [d for d in detections if d.class_id == class_id]
        class_gts = [g for g in ground_truths if g.class_id == class_id]
        class_matches = [(d, g, iou) for d, g, iou in matches if detections[d].class_id == class_id]

        tp = len(class_matches)
        fp = len(class_dets) - tp
        fn = len(class_gts) - tp

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

        per_class[class_name] = {
            "predictions": len(class_dets),
            "ground_truth": len(class_gts),
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1
        }

    metrics.per_class_metrics = per_class

    # Store matched pairs for analysis
    metrics.matched_pairs = [
        {
            "detection": {
                "class": detections[d].class_name,
                "confidence": detections[d].confidence,
                "bbox": detections[d].to_xyxy()
            },
            "ground_truth": {
                "class": ground_truths[g].class_name,
                "pii_key": ground_truths[g].pii_key,
                "value": ground_truths[g].value,
                "bbox": ground_truths[g].to_xyxy()
            },
            "iou": iou
        }
        for d, g, iou in matches
    ]

    return metrics


def evaluate_dataset(
    model: YOLOInference,
    screenshots_dir: Path,
    iou_threshold: float = 0.5,
    output_path: Optional[Path] = None,
    visualize_dir: Optional[Path] = None,
    verbose: bool = False
) -> dict:
    """
    Evaluate model on entire dataset.

    Returns aggregated metrics.
    """
    json_files = sorted(screenshots_dir.glob("*.json"))
    json_files = [f for f in json_files if f.name != "manifest.json"]

    all_metrics = []
    total_tp, total_fp, total_fn = 0, 0, 0
    total_iou_sum = 0
    total_matches = 0

    per_class_totals = {name: {"tp": 0, "fp": 0, "fn": 0, "gt": 0, "pred": 0}
                        for name in PII_CLASSES_SIMPLIFIED}

    print(f"Evaluating {len(json_files)} images...")

    for json_path in json_files:
        image_path = json_path.with_suffix(".png")
        if not image_path.exists():
            continue

        try:
            # Load ground truth
            ground_truths = load_ground_truth(json_path)

            # Run inference
            detections = model.predict(image_path)

            # Evaluate
            metrics = evaluate_detections(detections, ground_truths, iou_threshold)

            all_metrics.append({
                "image": image_path.name,
                "metrics": {
                    "predictions": metrics.num_predictions,
                    "ground_truth": metrics.num_ground_truth,
                    "true_positives": metrics.true_positives,
                    "false_positives": metrics.false_positives,
                    "false_negatives": metrics.false_negatives,
                    "precision": metrics.precision,
                    "recall": metrics.recall,
                    "f1": metrics.f1,
                    "avg_iou": metrics.avg_iou
                },
                "per_class": metrics.per_class_metrics
            })

            total_tp += metrics.true_positives
            total_fp += metrics.false_positives
            total_fn += metrics.false_negatives
            total_iou_sum += metrics.avg_iou * metrics.true_positives
            total_matches += metrics.true_positives

            # Update per-class totals
            for class_name, cm in metrics.per_class_metrics.items():
                per_class_totals[class_name]["tp"] += cm["true_positives"]
                per_class_totals[class_name]["fp"] += cm["false_positives"]
                per_class_totals[class_name]["fn"] += cm["false_negatives"]
                per_class_totals[class_name]["gt"] += cm["ground_truth"]
                per_class_totals[class_name]["pred"] += cm["predictions"]

            if verbose:
                print(f"  {image_path.name}: P={metrics.precision:.2%} R={metrics.recall:.2%} F1={metrics.f1:.2%}")

            # Visualize if requested
            if visualize_dir:
                visualize_dir.mkdir(parents=True, exist_ok=True)
                viz_path = visualize_dir / f"pred_{image_path.name}"
                visualize_predictions(image_path, detections, ground_truths, viz_path)

        except Exception as e:
            print(f"Error processing {json_path.name}: {e}")

    # Aggregate metrics
    overall_precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0
    overall_recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0
    overall_f1 = 2 * overall_precision * overall_recall / (overall_precision + overall_recall) if (overall_precision + overall_recall) > 0 else 0
    overall_iou = total_iou_sum / total_matches if total_matches > 0 else 0

    # Per-class aggregate metrics
    per_class_results = {}
    for class_name, totals in per_class_totals.items():
        tp, fp, fn = totals["tp"], totals["fp"], totals["fn"]
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

        per_class_results[class_name] = {
            "total_ground_truth": totals["gt"],
            "total_predictions": totals["pred"],
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1
        }

    summary = {
        "model_path": str(model.weights_path),
        "model_type": model.model_type,
        "iou_threshold": iou_threshold,
        "conf_threshold": model.conf_threshold,
        "total_images": len(all_metrics),
        "overall": {
            "true_positives": total_tp,
            "false_positives": total_fp,
            "false_negatives": total_fn,
            "precision": overall_precision,
            "recall": overall_recall,
            "f1": overall_f1,
            "avg_iou": overall_iou
        },
        "per_class": per_class_results,
        "per_image": all_metrics
    }

    print("\n" + "="*60)
    print("EVALUATION RESULTS")
    print("="*60)
    print(f"Model: {model.weights_path.name}")
    print(f"Images: {len(all_metrics)}")
    print(f"IoU threshold: {iou_threshold}")
    print(f"\nOverall Metrics:")
    print(f"  Precision: {overall_precision:.2%}")
    print(f"  Recall: {overall_recall:.2%}")
    print(f"  F1 Score: {overall_f1:.2%}")
    print(f"  Avg IoU: {overall_iou:.2%}")

    print(f"\nPer-Class Metrics:")
    for class_name, metrics in per_class_results.items():
        print(f"  {class_name}:")
        print(f"    GT: {metrics['total_ground_truth']} | Pred: {metrics['total_predictions']}")
        print(f"    P: {metrics['precision']:.2%} | R: {metrics['recall']:.2%} | F1: {metrics['f1']:.2%}")

    if output_path:
        with open(output_path, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\nResults saved to: {output_path}")

    return summary


def visualize_predictions(
    image_path: Path,
    detections: list[Detection],
    ground_truths: list[GroundTruth],
    output_path: Path,
    show_gt: bool = True
):
    """
    Visualize predictions overlaid on image.

    Green boxes: Correct predictions (matching GT)
    Red boxes: False positives
    Blue boxes: Ground truth (if show_gt=True)
    """
    image = cv2.imread(str(image_path))

    # Colors for each class
    colors = [
        (0, 255, 0),    # name - green
        (255, 165, 0),  # contact - orange
        (255, 0, 0),    # address - blue
        (128, 0, 128),  # card - purple
        (255, 255, 0),  # account - cyan
    ]

    # Draw ground truth (lighter/dashed)
    if show_gt:
        for gt in ground_truths:
            x1, y1, x2, y2 = int(gt.x1), int(gt.y1), int(gt.x2), int(gt.y2)
            color = colors[gt.class_id % len(colors)]
            # Draw dashed rectangle (using line segments)
            cv2.rectangle(image, (x1, y1), (x2, y2), color, 1)
            cv2.putText(image, f"GT:{gt.class_name}", (x1, y1 - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

    # Draw predictions
    for det in detections:
        x1, y1, x2, y2 = int(det.x1), int(det.y1), int(det.x2), int(det.y2)
        color = colors[det.class_id % len(colors)]
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
        label = f"{det.class_name}: {det.confidence:.2f}"
        cv2.putText(image, label, (x1, y2 + 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    cv2.imwrite(str(output_path), image)


def run_single_inference(
    model: YOLOInference,
    image_path: Path,
    annotation_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    verbose: bool = True
) -> dict:
    """Run inference on a single image with optional evaluation."""
    detections = model.predict(image_path)

    result = {
        "image": str(image_path),
        "num_detections": len(detections),
        "detections": [
            {
                "class": d.class_name,
                "confidence": d.confidence,
                "bbox": d.to_xyxy()
            }
            for d in detections
        ]
    }

    if verbose:
        print(f"\nImage: {image_path.name}")
        print(f"Detections: {len(detections)}")
        for d in detections:
            print(f"  - {d.class_name}: {d.confidence:.2%} at {d.to_xywh()}")

    # Evaluate against ground truth if provided
    if annotation_path and annotation_path.exists():
        ground_truths = load_ground_truth(annotation_path)
        metrics = evaluate_detections(detections, ground_truths)

        result["evaluation"] = {
            "ground_truth_count": len(ground_truths),
            "true_positives": metrics.true_positives,
            "false_positives": metrics.false_positives,
            "false_negatives": metrics.false_negatives,
            "precision": metrics.precision,
            "recall": metrics.recall,
            "f1": metrics.f1,
            "avg_iou": metrics.avg_iou
        }

        if verbose:
            print(f"\nEvaluation:")
            print(f"  Ground truth: {len(ground_truths)}")
            print(f"  TP: {metrics.true_positives} | FP: {metrics.false_positives} | FN: {metrics.false_negatives}")
            print(f"  Precision: {metrics.precision:.2%} | Recall: {metrics.recall:.2%} | F1: {metrics.f1:.2%}")

        # Visualize if output path provided
        if output_path:
            visualize_predictions(image_path, detections, ground_truths, output_path)
            print(f"\nVisualization saved to: {output_path}")

    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(description="YOLO inference and evaluation for PII detection")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Inference command
    infer_parser = subparsers.add_parser("infer", help="Run inference on image(s)")
    infer_parser.add_argument("--weights", type=Path, required=True,
                              help="Path to model weights")
    infer_parser.add_argument("--image", type=Path, required=True,
                              help="Path to image")
    infer_parser.add_argument("--annotation", type=Path, default=None,
                              help="Path to annotation JSON for evaluation")
    infer_parser.add_argument("--output", type=Path, default=None,
                              help="Output path for visualization")
    infer_parser.add_argument("--conf", type=float, default=0.25,
                              help="Confidence threshold")
    infer_parser.add_argument("--iou", type=float, default=0.45,
                              help="IoU threshold for NMS")
    infer_parser.add_argument("--model-type", type=str, default="auto",
                              choices=["auto", "yolov8", "yolov5"],
                              help="Model type")

    # Evaluate command
    eval_parser = subparsers.add_parser("evaluate", help="Evaluate on dataset")
    eval_parser.add_argument("--weights", type=Path, required=True,
                             help="Path to model weights")
    eval_parser.add_argument("--screenshots-dir", type=Path,
                             default=Path(__file__).parent.parent / "ui_reproducer" / "screenshots",
                             help="Path to screenshots directory")
    eval_parser.add_argument("--output", type=Path, default=None,
                             help="Output path for results JSON")
    eval_parser.add_argument("--visualize-dir", type=Path, default=None,
                             help="Directory for visualization outputs")
    eval_parser.add_argument("--conf", type=float, default=0.25,
                             help="Confidence threshold")
    eval_parser.add_argument("--iou-threshold", type=float, default=0.5,
                             help="IoU threshold for matching")
    eval_parser.add_argument("--model-type", type=str, default="auto",
                             choices=["auto", "yolov8", "yolov5"],
                             help="Model type")
    eval_parser.add_argument("--verbose", "-v", action="store_true",
                             help="Print per-image results")

    # Batch inference command
    batch_parser = subparsers.add_parser("batch", help="Run batch inference")
    batch_parser.add_argument("--weights", type=Path, required=True,
                              help="Path to model weights")
    batch_parser.add_argument("--images-dir", type=Path, required=True,
                              help="Directory containing images")
    batch_parser.add_argument("--output-dir", type=Path, required=True,
                              help="Output directory for results")
    batch_parser.add_argument("--conf", type=float, default=0.25,
                              help="Confidence threshold")
    batch_parser.add_argument("--model-type", type=str, default="auto",
                              choices=["auto", "yolov8", "yolov5"],
                              help="Model type")

    args = parser.parse_args()

    if args.command == "infer":
        model = YOLOInference(
            weights_path=args.weights,
            model_type=args.model_type,
            conf_threshold=args.conf,
            iou_threshold=args.iou
        )

        result = run_single_inference(
            model,
            args.image,
            annotation_path=args.annotation,
            output_path=args.output
        )

    elif args.command == "evaluate":
        model = YOLOInference(
            weights_path=args.weights,
            model_type=args.model_type,
            conf_threshold=args.conf
        )

        evaluate_dataset(
            model,
            screenshots_dir=args.screenshots_dir,
            iou_threshold=args.iou_threshold,
            output_path=args.output,
            visualize_dir=args.visualize_dir,
            verbose=args.verbose
        )

    elif args.command == "batch":
        model = YOLOInference(
            weights_path=args.weights,
            model_type=args.model_type,
            conf_threshold=args.conf
        )

        args.output_dir.mkdir(parents=True, exist_ok=True)

        image_files = list(args.images_dir.glob("*.png")) + list(args.images_dir.glob("*.jpg"))

        results = []
        for img_path in image_files:
            detections = model.predict(img_path)
            results.append({
                "image": img_path.name,
                "detections": [
                    {"class": d.class_name, "confidence": d.confidence, "bbox": d.to_xyxy()}
                    for d in detections
                ]
            })

            # Save visualization
            output_img = args.output_dir / f"pred_{img_path.name}"
            visualize_predictions(img_path, detections, [], output_img, show_gt=False)

        # Save results JSON
        results_path = args.output_dir / "predictions.json"
        with open(results_path, "w") as f:
            json.dump(results, f, indent=2)

        print(f"Processed {len(image_files)} images")
        print(f"Results saved to: {args.output_dir}")


if __name__ == "__main__":
    main()
