"""
Tesseract-based PII Detector.

Uses Tesseract OCR for text extraction and Presidio analyzer for PII classification.
This provides a baseline using the same OCR engine as Presidio's ImageAnalyzerEngine.
"""

from pathlib import Path

import pytesseract
from PIL import Image
from presidio_analyzer import AnalyzerEngine

from .base import PIIDetector, PIIDetection, BBox


class TesseractDetector(PIIDetector):
    """PII detector using Tesseract OCR + Presidio analyzer."""

    def __init__(
        self,
        ocr_confidence_threshold: float = 30,
        score_threshold: float = 0.1
    ):
        """
        Initialize Tesseract detector.

        Args:
            ocr_confidence_threshold: Minimum OCR confidence to include text
            score_threshold: Minimum PII detection score to include
        """
        self.analyzer = AnalyzerEngine()
        self.ocr_confidence_threshold = ocr_confidence_threshold
        self.score_threshold = score_threshold

    @property
    def name(self) -> str:
        return "tesseract"

    def _run_ocr(self, image_path: Path) -> list[dict]:
        """Run Tesseract OCR and return text items with bounding boxes."""
        image = Image.open(image_path)
        ocr_data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

        items = []
        n_boxes = len(ocr_data["text"])

        for i in range(n_boxes):
            text = ocr_data["text"][i].strip()
            conf = float(ocr_data["conf"][i])

            if not text or conf < self.ocr_confidence_threshold:
                continue

            items.append({
                "text": text,
                "bbox": BBox(
                    x=ocr_data["left"][i],
                    y=ocr_data["top"][i],
                    width=ocr_data["width"][i],
                    height=ocr_data["height"][i]
                ),
                "confidence": conf
            })

        return items

    def _merge_lines(self, items: list[dict], y_tolerance: float = 10, x_gap: float = 50) -> list[dict]:
        """Merge OCR items that are on the same line."""
        if not items:
            return []

        # Sort by y, then x
        sorted_items = sorted(items, key=lambda x: (x["bbox"].y, x["bbox"].x))
        lines = []
        current_line = [sorted_items[0]]

        for item in sorted_items[1:]:
            last = current_line[-1]
            y_overlap = abs(item["bbox"].y - last["bbox"].y) < y_tolerance
            x_close = item["bbox"].x - (last["bbox"].x + last["bbox"].width) < x_gap

            if y_overlap and x_close:
                current_line.append(item)
            else:
                lines.append(current_line)
                current_line = [item]

        lines.append(current_line)

        # Merge each line into single item
        merged = []
        for line in lines:
            text = " ".join(item["text"] for item in line)
            x1 = min(item["bbox"].x for item in line)
            y1 = min(item["bbox"].y for item in line)
            x2 = max(item["bbox"].x + item["bbox"].width for item in line)
            y2 = max(item["bbox"].y + item["bbox"].height for item in line)
            avg_conf = sum(item["confidence"] for item in line) / len(line)

            merged.append({
                "text": text,
                "bbox": BBox(x=x1, y=y1, width=x2-x1, height=y2-y1),
                "confidence": avg_conf
            })

        return merged

    def _classify_with_presidio(self, ocr_items: list[dict]) -> list[PIIDetection]:
        """Classify OCR text using Presidio analyzer."""
        detections = []

        for item in ocr_items:
            text = item["text"]
            bbox = item["bbox"]

            # Run Presidio analyzer on this text
            results = self.analyzer.analyze(
                text=text,
                language="en"
            )

            for result in results:
                if result.score < self.score_threshold:
                    continue

                # Extract the detected PII text
                detected_text = text[result.start:result.end]

                # Calculate bbox for the detected portion
                text_len = len(text)
                if text_len > 0:
                    start_ratio = result.start / text_len
                    end_ratio = result.end / text_len
                    pii_x = bbox.x + bbox.width * start_ratio
                    pii_width = bbox.width * (end_ratio - start_ratio)
                    pii_bbox = BBox(
                        x=pii_x,
                        y=bbox.y,
                        width=max(pii_width, 10),
                        height=bbox.height
                    )
                else:
                    pii_bbox = bbox

                detections.append(PIIDetection(
                    pii_type=result.entity_type,
                    text=detected_text,
                    bbox=pii_bbox,
                    confidence=result.score,
                    method=self.name,
                    raw_type=result.entity_type
                ))

        return detections

    def _detect(self, image_path: Path) -> list[PIIDetection]:
        """Run Tesseract OCR + Presidio detection."""
        # Run OCR
        ocr_items = self._run_ocr(image_path)

        # Merge into lines
        merged_items = self._merge_lines(ocr_items)

        # Classify with Presidio
        detections = self._classify_with_presidio(merged_items)

        return detections
