"""
Base classes for PII detection methods.

All detectors implement the same interface for fair benchmarking.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import time


@dataclass
class BBox:
    """Bounding box with x, y, width, height."""
    x: float
    y: float
    width: float
    height: float

    def to_xyxy(self) -> tuple[float, float, float, float]:
        """Convert to (x1, y1, x2, y2) format."""
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    def area(self) -> float:
        return self.width * self.height

    @classmethod
    def from_xyxy(cls, x1: float, y1: float, x2: float, y2: float) -> "BBox":
        return cls(x=x1, y=y1, width=x2 - x1, height=y2 - y1)


# Standardized PII type names (mapped from various sources)
PII_TYPES = {
    # Names
    "PERSON": "NAME",
    "FIRST_NAME": "NAME",
    "LAST_NAME": "NAME",
    "NAME": "NAME",

    # Contact
    "EMAIL": "EMAIL",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE": "PHONE",
    "PHONE_NUMBER": "PHONE",

    # Financial
    "CREDIT_CARD": "CARD",
    "CREDIT_CARD_NUMBER": "CARD",
    "CARD_NUMBER": "CARD",
    "CARD": "CARD",
    "CVV": "CVV",

    # Address
    "ADDRESS": "ADDRESS",
    "STREET": "ADDRESS",
    "STREET_ADDRESS": "ADDRESS",
    "LOCATION": "ADDRESS",
    "CITY": "ADDRESS",
    "STATE": "ADDRESS",
    "ZIP": "ADDRESS",
    "POSTCODE": "ADDRESS",
    "POSTAL_CODE": "ADDRESS",

    # IDs
    "SSN": "SSN",
    "SOCIAL_SECURITY_NUMBER": "SSN",
    "US_SSN": "SSN",
    "ACCOUNT_ID": "ID",
    "ID": "ID",

    # Other
    "DATE": "DATE",
    "DATE_TIME": "DATE",
    "IP_ADDRESS": "IP",
    "URL": "URL",
}


def normalize_pii_type(pii_type: str) -> str:
    """Normalize PII type to standard name."""
    upper = pii_type.upper().replace("-", "_").replace(" ", "_")
    return PII_TYPES.get(upper, upper)


@dataclass
class PIIDetection:
    """A single PII detection result."""
    pii_type: str           # Normalized PII type (NAME, EMAIL, PHONE, CARD, ADDRESS, etc.)
    text: str               # Detected text content
    bbox: BBox              # Bounding box location
    confidence: float       # 0-1 confidence score
    method: str             # Which detector produced this
    raw_type: str = ""      # Original type from detector before normalization

    def __post_init__(self):
        if not self.raw_type:
            self.raw_type = self.pii_type
        self.pii_type = normalize_pii_type(self.pii_type)


@dataclass
class DetectionResult:
    """Result from running a detector on an image."""
    detections: list[PIIDetection]
    latency_ms: float
    method: str
    image_path: str
    error: Optional[str] = None
    raw_ocr_text: Optional[str] = None  # Full OCR text if available


class PIIDetector(ABC):
    """Abstract base class for PII detectors."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Detector name for reporting."""
        pass

    @abstractmethod
    def _detect(self, image_path: Path) -> list[PIIDetection]:
        """
        Internal detection method. Subclasses implement this.

        Args:
            image_path: Path to image file

        Returns:
            List of PIIDetection objects
        """
        pass

    def detect(self, image_path: Path) -> DetectionResult:
        """
        Run detection with timing.

        Args:
            image_path: Path to image file

        Returns:
            DetectionResult with detections of latency
        """
        start = time.perf_counter()
        error = None
        detections = []

        try:
            detections = self._detect(image_path)
        except Exception as e:
            error = str(e)

        elapsed_ms = (time.perf_counter() - start) * 1000

        return DetectionResult(
            detections=detections,
            latency_ms=elapsed_ms,
            method=self.name,
            image_path=str(image_path),
            error=error
        )


def compute_iou(box1: BBox, box2: BBox) -> float:
    """Compute Intersection over Union between two bounding boxes."""
    x1_1, y1_1, x2_1, y2_1 = box1.to_xyxy()
    x1_2, y1_2, x2_2, y2_2 = box2.to_xyxy()

    xi1 = max(x1_1, x1_2)
    yi1 = max(y1_1, y1_2)
    xi2 = min(x2_1, x2_2)
    yi2 = min(y2_1, y2_2)

    if xi2 <= xi1 or yi2 <= yi1:
        return 0.0

    inter_area = (xi2 - xi1) * (yi2 - yi1)
    union_area = box1.area() + box2.area() - inter_area

    return inter_area / union_area if union_area > 0 else 0.0


def compute_containment(inner: BBox, outer: BBox) -> float:
    """
    Compute how much of inner box is contained within outer box.
    Useful when OCR text box is inside a larger field box.
    """
    x1_i, y1_i, x2_i, y2_i = inner.to_xyxy()
    x1_o, y1_o, x2_o, y2_o = outer.to_xyxy()

    xi1 = max(x1_i, x1_o)
    yi1 = max(y1_i, y1_o)
    xi2 = min(x2_i, x2_o)
    yi2 = min(y2_i, y2_o)

    if xi2 <= xi1 or yi2 <= yi1:
        return 0.0

    inter_area = (xi2 - xi1) * (yi2 - yi1)
    inner_area = inner.area()

    return inter_area / inner_area if inner_area > 0 else 0.0
