"""
Presidio-based PII Detector.

Uses Microsoft Presidio's ImageAnalyzerEngine which combines:
- Tesseract OCR for text extraction
- spaCy NER for entity recognition
- Custom recognizers for patterns (emails, phones, credit cards, etc.)
"""

from pathlib import Path
from PIL import Image

from presidio_image_redactor import ImageAnalyzerEngine

from .base import PIIDetector, PIIDetection, BBox


class PresidioDetector(PIIDetector):
    """PII detector using Microsoft Presidio."""

    def __init__(self, ocr_threshold: float = 0.3, score_threshold: float = 0.1):
        """
        Initialize Presidio detector.

        Args:
            ocr_threshold: Minimum OCR confidence to include text
            score_threshold: Minimum PII detection score to include
        """
        self.engine = ImageAnalyzerEngine()
        self.ocr_threshold = ocr_threshold
        self.score_threshold = score_threshold

    @property
    def name(self) -> str:
        return "presidio"

    def _detect(self, image_path: Path) -> list[PIIDetection]:
        """Run Presidio analysis on image."""
        image = Image.open(image_path)

        # Run OCR first to get text
        ocr_result = self.engine.ocr.perform_ocr(image)
        ocr_text = self.engine.ocr.get_text_from_ocr_dict(ocr_result)

        # Run analysis
        # ocr_kwargs controls OCR, text_analyzer_kwargs control NER
        results = self.engine.analyze(
            image,
            ocr_kwargs={"ocr_threshold": self.ocr_threshold},
            score_threshold=self.score_threshold
        )

        detections = []
        for result in results:
            # Extract bbox from result
            bbox = BBox(
                x=result.left,
                y=result.top,
                width=result.width,
                height=result.height
            )

            # Get detected text using start/end positions in OCR text
            text = ""
            if hasattr(result, 'start') and hasattr(result, 'end'):
                try:
                    text = ocr_text[result.start:result.end]
                except (IndexError, TypeError):
                    text = ""

            detections.append(PIIDetection(
                pii_type=result.entity_type,
                text=text,
                bbox=bbox,
                confidence=result.score,
                method=self.name,
                raw_type=result.entity_type
            ))

        return detections

    def analyze_with_ocr_text(self, image_path: Path) -> tuple[list[PIIDetection], str]:
        """
        Run analysis and also return full OCR text.

        Useful for debugging/logging.
        """
        image = Image.open(image_path)

        # Get OCR results first
        ocr_result = self.engine.ocr.perform_ocr(image)
        ocr_text = self.engine.ocr.get_text_from_ocr_dict(ocr_result)

        # Run analysis
        results = self.engine.analyze(
            image,
            ocr_threshold=self.ocr_threshold,
            score_threshold=self.score_threshold
        )

        detections = []
        for result in results:
            bbox = BBox(
                x=result.left,
                y=result.top,
                width=result.width,
                height=result.height
            )

            # Try to get text from OCR text using start/end positions
            text = ""
            if hasattr(result, 'start') and hasattr(result, 'end'):
                try:
                    text = ocr_text[result.start:result.end]
                except (IndexError, TypeError):
                    text = f"[{result.entity_type}]"

            detections.append(PIIDetection(
                pii_type=result.entity_type,
                text=text,
                bbox=bbox,
                confidence=result.score,
                method=self.name,
                raw_type=result.entity_type
            ))

        return detections, ocr_text
