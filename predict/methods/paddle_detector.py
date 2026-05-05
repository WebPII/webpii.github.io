"""
PaddleOCR-based PII Detector.

Uses PaddleOCR for text extraction (often better than Tesseract for complex UIs)
then uses Presidio's analyzer for PII classification.
"""

from pathlib import Path
from typing import Optional

from PIL import Image
from paddleocr import PaddleOCR
from presidio_analyzer import AnalyzerEngine

from .base import PIIDetector, PIIDetection, BBox


class PaddleDetector(PIIDetector):
    """PII detector using PaddleOCR + Presidio analyzer."""

    def __init__(
        self,
        lang: str = "en",
        score_threshold: float = 0.1
    ):
        """
        Initialize PaddleOCR detector.

        Args:
            lang: Language for OCR
            score_threshold: Minimum PII detection score to include
        """
        self.ocr = PaddleOCR(lang=lang)
        self.analyzer = AnalyzerEngine()
        self.score_threshold = score_threshold

    @property
    def name(self) -> str:
        return "paddle"

    def _run_ocr(self, image_path: Path) -> list[dict]:
        """Run PaddleOCR and return text items with bounding boxes."""
        result = self.ocr.predict(str(image_path))

        items = []
        if result is None or len(result) == 0:
            return items

        # New PaddleOCR API returns list of dicts with rec_texts, rec_boxes, rec_scores
        for page_result in result:
            texts = page_result.get('rec_texts', [])
            boxes = page_result.get('rec_boxes', [])
            scores = page_result.get('rec_scores', [])

            for text, box, score in zip(texts, boxes, scores):
                # box is [x1, y1, x2, y2]
                x1, y1, x2, y2 = box.tolist()

                items.append({
                    "text": text,
                    "bbox": BBox(x=x1, y=y1, width=x2-x1, height=y2-y1),
                    "confidence": score
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
            # Check if items are on same line (similar y) and close together (x gap)
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
                # Approximate: scale bbox based on character position
                text_len = len(text)
                if text_len > 0:
                    start_ratio = result.start / text_len
                    end_ratio = result.end / text_len
                    pii_x = bbox.x + bbox.width * start_ratio
                    pii_width = bbox.width * (end_ratio - start_ratio)
                    pii_bbox = BBox(
                        x=pii_x,
                        y=bbox.y,
                        width=max(pii_width, 10),  # Minimum width
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
        """Run PaddleOCR + Presidio detection."""
        # Run OCR
        ocr_items = self._run_ocr(image_path)

        # Merge into lines
        merged_items = self._merge_lines(ocr_items)

        # Classify with Presidio
        detections = self._classify_with_presidio(merged_items)

        return detections


class PaddleLLMDetector(PIIDetector):
    """PII detector using PaddleOCR + LLM classification."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: Optional[str] = None,
        lang: str = "en"
    ):
        """
        Initialize PaddleOCR + LLM detector.

        Args:
            model: OpenAI model to use
            api_key: OpenAI API key (or uses OPENAI_API_KEY env var)
            lang: Language for OCR
        """
        import json
        import os
        from openai import OpenAI

        self._json = json
        self.ocr = PaddleOCR(lang=lang)
        self.model = model
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    @property
    def name(self) -> str:
        return f"paddle-llm-{self.model}"

    def _run_ocr(self, image_path: Path) -> list[dict]:
        """Run PaddleOCR and return text items with bounding boxes."""
        result = self.ocr.predict(str(image_path))

        items = []
        if result is None or len(result) == 0:
            return items

        for page_result in result:
            texts = page_result.get('rec_texts', [])
            boxes = page_result.get('rec_boxes', [])
            scores = page_result.get('rec_scores', [])

            for text, box, score in zip(texts, boxes, scores):
                x1, y1, x2, y2 = box.tolist()

                items.append({
                    "text": text,
                    "bbox": BBox(x=x1, y=y1, width=x2-x1, height=y2-y1),
                    "confidence": score
                })

        return items

    def _merge_lines(self, items: list[dict], y_tolerance: float = 10, x_gap: float = 50) -> list[dict]:
        """Merge OCR items that are on the same line."""
        if not items:
            return []

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

    def _classify_with_llm(self, ocr_items: list[dict]) -> list[dict]:
        """Send OCR text to LLM for PII classification."""
        if not ocr_items:
            return []

        SYSTEM_PROMPT = """You are a PII (Personally Identifiable Information) detector.
Given a list of text items extracted from a screenshot, identify which ones contain PII.

PII types to detect:
- NAME: Full names, first names, last names
- EMAIL: Email addresses
- PHONE: Phone numbers
- CARD: Credit/debit card numbers (full or partial)
- ADDRESS: Street addresses, cities, states, zip codes
- SSN: Social security numbers
- ID: Account IDs, user IDs
- DATE: Dates of birth, specific dates that could identify someone

For each PII item found, respond with the EXACT text as it appears in the input.
Only flag text that is clearly PII - not labels like "Email:" or "Phone Number:".

Respond with a JSON object in this exact format:
{
  "pii_items": [
    {"text": "exact text from input", "type": "NAME"},
    {"text": "mjohnson@gmail.com", "type": "EMAIL"}
  ]
}

If no PII is found, respond with: {"pii_items": []}"""

        text_list = [item["text"] for item in ocr_items]
        user_message = "Text items from screenshot:\n" + "\n".join(f"- {t}" for t in text_list)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message}
                ],
                response_format={"type": "json_object"},
                temperature=0
            )

            result = self._json.loads(response.choices[0].message.content)
            return result.get("pii_items", [])

        except Exception as e:
            print(f"LLM classification error: {e}")
            return []

    def _match_pii_to_bbox(
        self,
        pii_items: list[dict],
        ocr_items: list[dict]
    ) -> list[PIIDetection]:
        """Match LLM-identified PII back to OCR bounding boxes."""
        detections = []

        for pii in pii_items:
            pii_text = pii.get("text", "").lower().strip()
            pii_type = pii.get("type", "UNKNOWN")

            for ocr_item in ocr_items:
                ocr_text = ocr_item["text"].lower().strip()

                if pii_text == ocr_text or pii_text in ocr_text or ocr_text in pii_text:
                    detections.append(PIIDetection(
                        pii_type=pii_type,
                        text=ocr_item["text"],
                        bbox=ocr_item["bbox"],
                        confidence=0.9,
                        method=self.name,
                        raw_type=pii_type
                    ))
                    break

        return detections

    def _detect(self, image_path: Path) -> list[PIIDetection]:
        """Run PaddleOCR + LLM detection."""
        ocr_items = self._run_ocr(image_path)
        merged_items = self._merge_lines(ocr_items)
        pii_items = self._classify_with_llm(merged_items)
        detections = self._match_pii_to_bbox(pii_items, merged_items)
        return detections
