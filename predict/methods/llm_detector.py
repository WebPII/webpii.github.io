"""
LLM-based PII Detector.

Uses OCR to extract text with bounding boxes, then sends to GPT-4o-mini
to classify which text regions contain PII.
"""

import json
import os
from pathlib import Path
from typing import Optional

import pytesseract
from PIL import Image
from openai import OpenAI

from .base import PIIDetector, PIIDetection, BBox


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


class LLMDetector(PIIDetector):
    """PII detector using OCR + LLM classification."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: Optional[str] = None,
        ocr_confidence_threshold: float = 30
    ):
        """
        Initialize LLM detector.

        Args:
            model: OpenAI model to use
            api_key: OpenAI API key (or uses OPENAI_API_KEY env var)
            ocr_confidence_threshold: Minimum OCR confidence to include text
        """
        self.model = model
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        self.ocr_confidence_threshold = ocr_confidence_threshold

    @property
    def name(self) -> str:
        return f"llm-{self.model}"

    def _run_ocr(self, image_path: Path) -> list[dict]:
        """Run OCR and return text items with bounding boxes."""
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
        """Merge OCR items into lines."""
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

        # Merge each line
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

        # Build text list for LLM
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

            result = json.loads(response.choices[0].message.content)
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

            # Find matching OCR item
            for ocr_item in ocr_items:
                ocr_text = ocr_item["text"].lower().strip()

                # Check for match (exact or substring)
                if pii_text == ocr_text or pii_text in ocr_text or ocr_text in pii_text:
                    detections.append(PIIDetection(
                        pii_type=pii_type,
                        text=ocr_item["text"],
                        bbox=ocr_item["bbox"],
                        confidence=0.9,  # LLM classification confidence
                        method=self.name,
                        raw_type=pii_type
                    ))
                    break

        return detections

    def _detect(self, image_path: Path) -> list[PIIDetection]:
        """Run OCR + LLM detection."""
        # Run OCR
        ocr_items = self._run_ocr(image_path)

        # Merge into lines
        merged_items = self._merge_lines(ocr_items)

        # Classify with LLM
        pii_items = self._classify_with_llm(merged_items)

        # Match back to bboxes
        detections = self._match_pii_to_bbox(pii_items, merged_items)

        return detections
