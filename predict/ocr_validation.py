#!/usr/bin/env python3
"""
OCR-based PII Detection Validation

This script validates whether naive OCR can detect PII fields in screenshots
by comparing OCR output against ground truth bounding boxes from annotations.

Uses pytesseract for OCR and computes IoU between detected text regions
and ground truth PII bounding boxes.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytesseract
from PIL import Image

# PII field patterns for text matching
PII_PATTERNS = {
    "PII_EMAIL": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    "PII_PHONE": r"[\d\s\-\(\)]{10,}",
    "PII_CARD_NUMBER": r"\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}",
    "PII_CARD_LAST4": r"\*{4}\d{4}|\d{4}$",
    "PII_CVV": r"^\d{3,4}$",
    "PII_POSTCODE": r"\b\d{5}(?:-\d{4})?\b",
    "PII_STATE_ABBR": r"\b[A-Z]{2}\b",
}


@dataclass
class BBox:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_dict(cls, d: dict) -> "BBox":
        return cls(x=d["x"], y=d["y"], width=d["width"], height=d["height"])

    def to_xyxy(self) -> tuple[float, float, float, float]:
        """Convert to (x1, y1, x2, y2) format."""
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    def area(self) -> float:
        return self.width * self.height


@dataclass
class PIIElement:
    key: str
    value: str
    bbox: BBox
    visible: bool
    element_type: str


@dataclass
class OCRDetection:
    text: str
    bbox: BBox
    confidence: float
    matched_pii_key: Optional[str] = None


def compute_iou(box1: BBox, box2: BBox) -> float:
    """Compute Intersection over Union between two bounding boxes."""
    x1_1, y1_1, x2_1, y2_1 = box1.to_xyxy()
    x1_2, y1_2, x2_2, y2_2 = box2.to_xyxy()

    # Intersection
    xi1 = max(x1_1, x1_2)
    yi1 = max(y1_1, y1_2)
    xi2 = min(x2_1, x2_2)
    yi2 = min(y2_1, y2_2)

    if xi2 <= xi1 or yi2 <= yi1:
        return 0.0

    inter_area = (xi2 - xi1) * (yi2 - yi1)

    # Union
    union_area = box1.area() + box2.area() - inter_area

    return inter_area / union_area if union_area > 0 else 0.0


def compute_containment(inner: BBox, outer: BBox) -> float:
    """
    Compute how much of inner box is contained within outer box.
    Returns ratio of intersection to inner box area.

    This is useful when OCR text box is inside a larger field box.
    """
    x1_i, y1_i, x2_i, y2_i = inner.to_xyxy()
    x1_o, y1_o, x2_o, y2_o = outer.to_xyxy()

    # Intersection
    xi1 = max(x1_i, x1_o)
    yi1 = max(y1_i, y1_o)
    xi2 = min(x2_i, x2_o)
    yi2 = min(y2_i, y2_o)

    if xi2 <= xi1 or yi2 <= yi1:
        return 0.0

    inter_area = (xi2 - xi1) * (yi2 - yi1)
    inner_area = inner.area()

    return inter_area / inner_area if inner_area > 0 else 0.0


def load_annotation(json_path: Path) -> tuple[list[PIIElement], dict]:
    """Load PII elements from annotation JSON."""
    with open(json_path) as f:
        data = json.load(f)

    pii_elements = []
    for elem in data.get("pii_elements", []):
        pii_elements.append(PIIElement(
            key=elem["key"],
            value=elem["value"],
            bbox=BBox.from_dict(elem["bbox"]),
            visible=elem["visible"],
            element_type=elem.get("element_type", "unknown")
        ))

    return pii_elements, data


def run_ocr(image_path: Path, preprocess: str = "none") -> list[OCRDetection]:
    """
    Run OCR on image and return detected text with bounding boxes.

    Args:
        image_path: Path to image file
        preprocess: Preprocessing method - 'thresh', 'blur', or 'none'
    """
    # Load image
    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError(f"Could not load image: {image_path}")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Preprocess
    if preprocess == "thresh":
        gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    elif preprocess == "blur":
        gray = cv2.medianBlur(gray, 3)

    # Run OCR with detailed output
    ocr_data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT)

    detections = []
    n_boxes = len(ocr_data["text"])

    for i in range(n_boxes):
        text = ocr_data["text"][i].strip()
        conf = float(ocr_data["conf"][i])

        # Skip empty or low-confidence detections
        if not text or conf < 30:
            continue

        bbox = BBox(
            x=ocr_data["left"][i],
            y=ocr_data["top"][i],
            width=ocr_data["width"][i],
            height=ocr_data["height"][i]
        )

        detections.append(OCRDetection(
            text=text,
            bbox=bbox,
            confidence=conf
        ))

    return detections


def merge_into_lines(
    detections: list[OCRDetection],
    y_tolerance: float = 10,
    x_gap_tolerance: float = 50
) -> list[OCRDetection]:
    """
    Merge word-level OCR detections into line-level detections.

    Words are merged if they're on the same horizontal line (within y_tolerance)
    and close together (within x_gap_tolerance).
    """
    if not detections:
        return []

    # Sort by y position, then x position
    sorted_dets = sorted(detections, key=lambda d: (d.bbox.y, d.bbox.x))

    lines = []
    current_line = [sorted_dets[0]]

    for det in sorted_dets[1:]:
        last = current_line[-1]

        # Check if on same line (y overlap)
        y_overlap = (
            abs(det.bbox.y - last.bbox.y) < y_tolerance or
            abs((det.bbox.y + det.bbox.height/2) - (last.bbox.y + last.bbox.height/2)) < y_tolerance
        )

        # Check if horizontally close
        last_right = last.bbox.x + last.bbox.width
        x_gap = det.bbox.x - last_right
        x_close = x_gap < x_gap_tolerance and x_gap > -last.bbox.width  # Allow some overlap

        if y_overlap and x_close:
            current_line.append(det)
        else:
            # Finish current line and start new one
            lines.append(current_line)
            current_line = [det]

    # Don't forget the last line
    lines.append(current_line)

    # Merge each line into a single detection
    merged = []
    for line in lines:
        if not line:
            continue

        # Combine text
        text = " ".join(d.text for d in line)

        # Compute bounding box that encompasses all words
        x1 = min(d.bbox.x for d in line)
        y1 = min(d.bbox.y for d in line)
        x2 = max(d.bbox.x + d.bbox.width for d in line)
        y2 = max(d.bbox.y + d.bbox.height for d in line)

        # Average confidence
        avg_conf = sum(d.confidence for d in line) / len(line)

        merged.append(OCRDetection(
            text=text,
            bbox=BBox(x=x1, y=y1, width=x2-x1, height=y2-y1),
            confidence=avg_conf
        ))

    return merged


def merge_vertically_adjacent(
    lines: list[OCRDetection],
    y_gap_tolerance: float = 20,
    x_overlap_ratio: float = 0.5
) -> list[OCRDetection]:
    """
    Merge vertically adjacent lines that likely form a single field
    (e.g., multi-line address).

    Lines are merged if they're vertically close and horizontally aligned.
    """
    if not lines:
        return []

    # Sort by y position
    sorted_lines = sorted(lines, key=lambda d: d.bbox.y)

    merged = []
    current_group = [sorted_lines[0]]

    for line in sorted_lines[1:]:
        last = current_group[-1]

        # Check vertical gap
        last_bottom = last.bbox.y + last.bbox.height
        y_gap = line.bbox.y - last_bottom

        # Check horizontal overlap
        last_x1, last_x2 = last.bbox.x, last.bbox.x + last.bbox.width
        line_x1, line_x2 = line.bbox.x, line.bbox.x + line.bbox.width

        overlap_start = max(last_x1, line_x1)
        overlap_end = min(last_x2, line_x2)
        overlap_width = max(0, overlap_end - overlap_start)

        min_width = min(last.bbox.width, line.bbox.width)
        overlap_ratio = overlap_width / min_width if min_width > 0 else 0

        if y_gap < y_gap_tolerance and y_gap > -10 and overlap_ratio > x_overlap_ratio:
            current_group.append(line)
        else:
            # Merge current group
            merged.append(_merge_group(current_group))
            current_group = [line]

    # Merge last group
    merged.append(_merge_group(current_group))

    return merged


def _merge_group(group: list[OCRDetection]) -> OCRDetection:
    """Merge a group of detections into one."""
    if len(group) == 1:
        return group[0]

    text = " ".join(d.text for d in group)
    x1 = min(d.bbox.x for d in group)
    y1 = min(d.bbox.y for d in group)
    x2 = max(d.bbox.x + d.bbox.width for d in group)
    y2 = max(d.bbox.y + d.bbox.height for d in group)
    avg_conf = sum(d.confidence for d in group) / len(group)

    return OCRDetection(
        text=text,
        bbox=BBox(x=x1, y=y1, width=x2-x1, height=y2-y1),
        confidence=avg_conf
    )


def run_ocr_with_merging(
    image_path: Path,
    preprocess: str = "none",
    merge_lines: bool = True,
    merge_vertical: bool = True,
    y_tolerance: float = 10,
    x_gap_tolerance: float = 50,
    y_gap_tolerance: float = 20
) -> tuple[list[OCRDetection], list[OCRDetection]]:
    """
    Run OCR with intelligent line merging.

    Returns:
        (raw_detections, merged_detections)
    """
    raw = run_ocr(image_path, preprocess)

    if not merge_lines:
        return raw, raw

    merged = merge_into_lines(raw, y_tolerance, x_gap_tolerance)

    if merge_vertical:
        merged = merge_vertically_adjacent(merged, y_gap_tolerance)

    return raw, merged


def match_ocr_to_pii(
    ocr_detections: list[OCRDetection],
    pii_elements: list[PIIElement],
    iou_threshold: float = 0.2,
    containment_threshold: float = 0.7,
    text_similarity_threshold: float = 0.5
) -> tuple[list[tuple[OCRDetection, PIIElement, float]], list[PIIElement], list[OCRDetection]]:
    """
    Match OCR detections to ground truth PII elements.

    Uses both IoU and containment metrics - OCR text boxes are often
    much smaller than PII field boxes, so we check if the OCR box
    is contained within the PII box.

    Returns:
        - matched: List of (detection, ground_truth, score) tuples
        - missed: PII elements not detected by OCR
        - false_positives: OCR detections that don't match any PII
    """
    matched = []
    matched_pii_indices = set()
    matched_ocr_indices = set()

    # Only consider visible PII elements
    visible_pii = [(i, elem) for i, elem in enumerate(pii_elements) if elem.visible]

    # Try to match each OCR detection to a PII element
    for ocr_idx, det in enumerate(ocr_detections):
        best_match = None
        best_score = 0

        for pii_idx, pii in visible_pii:
            if pii_idx in matched_pii_indices:
                continue

            iou = compute_iou(det.bbox, pii.bbox)
            containment = compute_containment(det.bbox, pii.bbox)

            # Also check text similarity
            det_text = det.text.lower().strip()
            pii_value = str(pii.value).lower().strip()

            # Exact match or substring match
            text_match = (
                det_text == pii_value or
                det_text in pii_value or
                pii_value in det_text
            )

            # Score combines IoU and containment
            # High containment means OCR box is inside PII field
            spatial_score = max(iou, containment * 0.5)

            # Boost score significantly if text matches
            if text_match:
                spatial_score = max(spatial_score, 0.5)  # Minimum 0.5 for text match
                spatial_score *= 1.5

            # Check if meets threshold (either IoU or containment)
            meets_threshold = (
                iou >= iou_threshold or
                (containment >= containment_threshold and text_match)
            )

            if spatial_score > best_score and meets_threshold:
                best_score = spatial_score
                best_match = (pii_idx, pii, spatial_score)

        if best_match:
            pii_idx, pii, score = best_match
            matched.append((det, pii, score))
            matched_pii_indices.add(pii_idx)
            matched_ocr_indices.add(ocr_idx)
            det.matched_pii_key = pii.key

    # Find missed PII (visible but not detected)
    missed = [pii for i, pii in visible_pii if i not in matched_pii_indices]

    # Find false positives (detections that look like PII but weren't matched)
    false_positives = []
    for ocr_idx, det in enumerate(ocr_detections):
        if ocr_idx in matched_ocr_indices:
            continue
        # Check if detection matches any PII pattern
        for pattern_name, pattern in PII_PATTERNS.items():
            if re.match(pattern, det.text):
                false_positives.append(det)
                break

    return matched, missed, false_positives


def evaluate_screenshot(
    image_path: Path,
    annotation_path: Path,
    verbose: bool = False,
    log_file=None,
    use_merging: bool = True
) -> tuple[dict, list[OCRDetection], list[OCRDetection]]:
    """
    Evaluate OCR-based PII detection on a single screenshot.

    Returns metrics dict, raw OCR detections, and merged OCR detections.
    """
    pii_elements, annotation_data = load_annotation(annotation_path)

    # Run OCR with merging
    raw_detections, merged_detections = run_ocr_with_merging(
        image_path,
        merge_lines=use_merging,
        merge_vertical=use_merging
    )

    # Use merged detections for matching
    ocr_detections = merged_detections if use_merging else raw_detections

    # Log all OCR detections
    if log_file:
        log_file.write(f"\n{'='*80}\n")
        log_file.write(f"IMAGE: {image_path.name}\n")
        log_file.write(f"Company: {annotation_data.get('company')} | Page: {annotation_data.get('page_type')}\n")
        log_file.write(f"{'='*80}\n\n")

        log_file.write(f"RAW OCR DETECTIONS ({len(raw_detections)} words):\n")
        log_file.write("-" * 60 + "\n")
        for i, det in enumerate(raw_detections[:20]):  # First 20 only
            log_file.write(f"  [{i:3d}] '{det.text}' @ ({det.bbox.x:.0f}, {det.bbox.y:.0f}, {det.bbox.width:.0f}x{det.bbox.height:.0f})\n")
        if len(raw_detections) > 20:
            log_file.write(f"  ... and {len(raw_detections) - 20} more\n")
        log_file.write("\n")

        log_file.write(f"MERGED DETECTIONS ({len(merged_detections)} lines):\n")
        log_file.write("-" * 60 + "\n")
        for i, det in enumerate(merged_detections):
            log_file.write(f"  [{i:3d}] '{det.text[:60]}{'...' if len(det.text) > 60 else ''}'\n")
            log_file.write(f"        @ ({det.bbox.x:.0f}, {det.bbox.y:.0f}, {det.bbox.width:.0f}x{det.bbox.height:.0f}) conf={det.confidence:.0f}%\n")
        log_file.write("\n")

        visible_pii = [p for p in pii_elements if p.visible]
        log_file.write(f"GROUND TRUTH PII ({len(visible_pii)} visible):\n")
        log_file.write("-" * 60 + "\n")
        for pii in visible_pii:
            log_file.write(f"  - {pii.key}: '{pii.value}'\n")
            log_file.write(f"    @ ({pii.bbox.x:.0f}, {pii.bbox.y:.0f}, {pii.bbox.width:.0f}x{pii.bbox.height:.0f})\n")
        log_file.write("\n")

    matched, missed, false_positives = match_ocr_to_pii(ocr_detections, pii_elements)

    # Calculate metrics
    visible_pii = [p for p in pii_elements if p.visible]
    n_visible = len(visible_pii)
    n_matched = len(matched)
    n_missed = len(missed)
    n_fp = len(false_positives)

    precision = n_matched / (n_matched + n_fp) if (n_matched + n_fp) > 0 else 0
    recall = n_matched / n_visible if n_visible > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    avg_iou = sum(m[2] for m in matched) / len(matched) if matched else 0

    result = {
        "image_path": str(image_path),
        "company": annotation_data.get("company"),
        "page_type": annotation_data.get("page_type"),
        "total_pii_elements": len(pii_elements),
        "visible_pii_elements": n_visible,
        "ocr_detections": len(ocr_detections),
        "matched": n_matched,
        "missed": n_missed,
        "false_positives": n_fp,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "avg_iou": avg_iou,
        "matched_details": [
            {
                "pii_key": m[1].key,
                "pii_value": m[1].value,
                "ocr_text": m[0].text,
                "iou": m[2]
            }
            for m in matched
        ],
        "missed_details": [
            {"key": p.key, "value": p.value}
            for p in missed
        ]
    }

    if verbose:
        print(f"\n{'='*60}")
        print(f"Image: {image_path.name}")
        print(f"Company: {annotation_data.get('company')} | Page: {annotation_data.get('page_type')}")
        print(f"{'='*60}")
        print(f"Visible PII elements: {n_visible}")
        print(f"OCR detections: {len(ocr_detections)}")
        print(f"Matched: {n_matched} | Missed: {n_missed} | False Positives: {n_fp}")
        print(f"Precision: {precision:.2%} | Recall: {recall:.2%} | F1: {f1:.2%}")
        print(f"Average IoU: {avg_iou:.2%}")

        if matched:
            print("\nMatched PII:")
            for det, pii, iou in matched:
                print(f"  - {pii.key}: '{pii.value}' <- OCR: '{det.text}' (IoU: {iou:.2%})")

        if missed:
            print("\nMissed PII:")
            for pii in missed:
                print(f"  - {pii.key}: '{pii.value}'")

    # Log matching results
    if log_file:
        log_file.write(f"MATCHING RESULTS:\n")
        log_file.write("-" * 60 + "\n")
        log_file.write(f"  Matched: {n_matched} | Missed: {n_missed} | False Positives: {n_fp}\n")
        log_file.write(f"  Precision: {precision:.2%} | Recall: {recall:.2%} | F1: {f1:.2%}\n\n")
        if matched:
            log_file.write("  Matched:\n")
            for det, pii, iou in matched:
                log_file.write(f"    - {pii.key}: '{pii.value}' <- OCR: '{det.text[:40]}' (IoU: {iou:.2%})\n")
        if missed:
            log_file.write("  Missed:\n")
            for pii in missed:
                log_file.write(f"    - {pii.key}: '{pii.value}'\n")
        log_file.write("\n")

    return result, raw_detections, merged_detections


def stitch_images(image_paths: list[Path], output_path: Path, cols: int = 4):
    """Stitch multiple images into a grid."""
    images = [cv2.imread(str(p)) for p in image_paths if p.exists()]
    if not images:
        return

    # Get max dimensions
    max_h = max(img.shape[0] for img in images)
    max_w = max(img.shape[1] for img in images)

    # Pad images to same size
    padded = []
    for img in images:
        h, w = img.shape[:2]
        pad_h = max_h - h
        pad_w = max_w - w
        padded_img = cv2.copyMakeBorder(img, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        padded.append(padded_img)

    # Arrange in grid
    rows = (len(padded) + cols - 1) // cols

    # Pad list to fill grid
    while len(padded) < rows * cols:
        padded.append(np.ones((max_h, max_w, 3), dtype=np.uint8) * 255)

    # Create grid
    grid_rows = []
    for r in range(rows):
        row_imgs = padded[r * cols:(r + 1) * cols]
        grid_rows.append(np.hstack(row_imgs))

    grid = np.vstack(grid_rows)
    cv2.imwrite(str(output_path), grid)
    print(f"Stitched grid saved to: {output_path} ({grid.shape[1]}x{grid.shape[0]})")


def evaluate_dataset(
    screenshots_dir: Path,
    output_path: Optional[Path] = None,
    log_path: Optional[Path] = None,
    visualize_dir: Optional[Path] = None,
    verbose: bool = False
) -> dict:
    """
    Evaluate OCR-based PII detection on entire dataset.
    """
    results = []

    # Find all annotation files
    json_files = sorted(screenshots_dir.glob("*.json"))
    json_files = [f for f in json_files if f.name != "manifest.json"]

    print(f"Found {len(json_files)} annotation files")

    # Open log file if requested
    log_file = None
    if log_path:
        log_file = open(log_path, "w")
        log_file.write("OCR VALIDATION LOG\n")
        log_file.write(f"Generated: {Path(__file__).name}\n")
        log_file.write(f"Screenshots dir: {screenshots_dir}\n")
        log_file.write("=" * 80 + "\n")

    viz_paths = []

    for json_path in json_files:
        image_path = json_path.with_suffix(".png")
        if not image_path.exists():
            print(f"Warning: Image not found for {json_path.name}")
            continue

        try:
            result, raw_dets, merged_dets = evaluate_screenshot(image_path, json_path, verbose=verbose, log_file=log_file)
            results.append(result)

            # Generate visualization if requested
            if visualize_dir:
                visualize_dir.mkdir(parents=True, exist_ok=True)
                viz_path = visualize_dir / f"{json_path.stem}_ocr_viz.png"
                visualize_detections(image_path, json_path, viz_path, use_merging=True)
                viz_paths.append(viz_path)

        except Exception as e:
            print(f"Error processing {json_path.name}: {e}")
            import traceback
            traceback.print_exc()

    # Aggregate metrics
    total_visible = sum(r["visible_pii_elements"] for r in results)
    total_matched = sum(r["matched"] for r in results)
    total_missed = sum(r["missed"] for r in results)
    total_fp = sum(r["false_positives"] for r in results)

    overall_precision = total_matched / (total_matched + total_fp) if (total_matched + total_fp) > 0 else 0
    overall_recall = total_matched / total_visible if total_visible > 0 else 0
    overall_f1 = 2 * overall_precision * overall_recall / (overall_precision + overall_recall) if (overall_precision + overall_recall) > 0 else 0

    summary = {
        "total_images": len(results),
        "total_visible_pii": total_visible,
        "total_matched": total_matched,
        "total_missed": total_missed,
        "total_false_positives": total_fp,
        "overall_precision": overall_precision,
        "overall_recall": overall_recall,
        "overall_f1": overall_f1,
        "avg_iou": sum(r["avg_iou"] for r in results) / len(results) if results else 0,
        "per_image_results": results
    }

    print("\n" + "="*60)
    print("OVERALL RESULTS")
    print("="*60)
    print(f"Total images: {len(results)}")
    print(f"Total visible PII elements: {total_visible}")
    print(f"Total matched: {total_matched}")
    print(f"Total missed: {total_missed}")
    print(f"Total false positives: {total_fp}")
    print(f"Overall Precision: {overall_precision:.2%}")
    print(f"Overall Recall: {overall_recall:.2%}")
    print(f"Overall F1: {overall_f1:.2%}")

    if output_path:
        with open(output_path, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\nResults saved to {output_path}")

    # Close log file
    if log_file:
        log_file.write("\n" + "=" * 80 + "\n")
        log_file.write("SUMMARY\n")
        log_file.write("=" * 80 + "\n")
        log_file.write(f"Total images: {len(results)}\n")
        log_file.write(f"Total visible PII: {total_visible}\n")
        log_file.write(f"Total matched: {total_matched}\n")
        log_file.write(f"Total missed: {total_missed}\n")
        log_file.write(f"Total false positives: {total_fp}\n")
        log_file.write(f"Overall Precision: {overall_precision:.2%}\n")
        log_file.write(f"Overall Recall: {overall_recall:.2%}\n")
        log_file.write(f"Overall F1: {overall_f1:.2%}\n")
        log_file.close()
        print(f"Log saved to {log_path}")

    # Stitch visualizations into grid
    if visualize_dir and viz_paths:
        grid_path = visualize_dir / "grid.png"
        stitch_images(viz_paths, grid_path)

    return summary


def visualize_detections(
    image_path: Path,
    annotation_path: Path,
    output_path: Optional[Path] = None,
    use_merging: bool = True
) -> np.ndarray:
    """
    Visualize OCR detections vs ground truth PII boxes.

    Green: Matched OCR detection
    Red: Missed PII (ground truth)
    Yellow: False positive OCR detection
    Blue: Ground truth PII box
    """
    image = cv2.imread(str(image_path))
    pii_elements, _ = load_annotation(annotation_path)

    raw_detections, merged_detections = run_ocr_with_merging(
        image_path, merge_lines=use_merging, merge_vertical=use_merging
    )
    ocr_detections = merged_detections if use_merging else raw_detections

    matched, missed, false_positives = match_ocr_to_pii(ocr_detections, pii_elements)

    # Draw ground truth PII boxes (blue)
    for pii in pii_elements:
        if pii.visible:
            x1, y1, x2, y2 = pii.bbox.to_xyxy()
            cv2.rectangle(image, (int(x1), int(y1)), (int(x2), int(y2)), (255, 0, 0), 2)

    # Draw matched detections (green)
    for det, pii, iou in matched:
        x1, y1, x2, y2 = det.bbox.to_xyxy()
        cv2.rectangle(image, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
        cv2.putText(image, f"{pii.key} ({iou:.0%})", (int(x1), int(y1)-5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)

    # Draw missed PII (red)
    for pii in missed:
        x1, y1, x2, y2 = pii.bbox.to_xyxy()
        cv2.rectangle(image, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 2)
        cv2.putText(image, f"MISSED: {pii.key}", (int(x1), int(y1)-5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)

    # Draw false positives (yellow)
    for det in false_positives:
        x1, y1, x2, y2 = det.bbox.to_xyxy()
        cv2.rectangle(image, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 255), 2)
        cv2.putText(image, f"FP: {det.text[:15]}", (int(x1), int(y1)-5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)

    if output_path:
        cv2.imwrite(str(output_path), image)
        print(f"Visualization saved to {output_path}")

    return image


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Validate OCR-based PII detection")
    parser.add_argument("--screenshots-dir", type=Path,
                        default=Path(__file__).parent.parent / "ui_reproducer" / "screenshots",
                        help="Path to screenshots directory")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output path for results JSON")
    parser.add_argument("--log", type=Path, default=None,
                        help="Output path for detailed OCR log file")
    parser.add_argument("--visualize", type=Path, default=None,
                        help="Output directory for visualization images (also creates grid.png)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print detailed per-image results")
    parser.add_argument("--single", type=str, default=None,
                        help="Evaluate single image (e.g., '0001')")

    args = parser.parse_args()

    if args.single:
        # Evaluate single image
        json_path = args.screenshots_dir / f"{args.single}.json"
        image_path = args.screenshots_dir / f"{args.single}.png"

        if not json_path.exists() or not image_path.exists():
            print(f"Error: Files not found for {args.single}")
            return

        # Open log file for single image
        log_file = None
        if args.log:
            log_file = open(args.log, "w")
            log_file.write(f"OCR VALIDATION LOG - Single image: {args.single}\n")
            log_file.write("=" * 80 + "\n")

        result, raw_dets, merged_dets = evaluate_screenshot(image_path, json_path, verbose=True, log_file=log_file)

        if log_file:
            log_file.close()
            print(f"Log saved to {args.log}")

        if args.visualize:
            args.visualize.mkdir(parents=True, exist_ok=True)
            output_viz = args.visualize / f"{args.single}_ocr_viz.png"
            visualize_detections(image_path, json_path, output_viz)
    else:
        # Evaluate entire dataset
        summary = evaluate_dataset(
            args.screenshots_dir,
            output_path=args.output,
            log_path=args.log,
            visualize_dir=args.visualize,
            verbose=args.verbose
        )


if __name__ == "__main__":
    main()
