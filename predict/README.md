# WebPII Prediction Baselines

This directory contains baseline and model code for detecting PII bounding boxes
in WebPII screenshots:

- OCR plus text/PII classifiers;
- Presidio, Tesseract, PaddleOCR, and LLM-assisted baselines;
- YOLOv8 training, inference, export, and evaluation.

## Setup

```bash
pip install -r requirements.txt

# For OCR, also install tesseract system dependency:
# macOS: brew install tesseract
# Ubuntu: sudo apt-get install tesseract-ocr
```

## Ground Truth Data

By default, scripts expect released screenshot annotations in
`ui_reproducer/screenshots/`:
- `*.png` - Screenshot images
- `*.json` - Annotations with PII bounding boxes

## 1. OCR Validation

Test if naive OCR can detect PII fields by comparing OCR output against ground truth.

```bash
# Evaluate entire dataset
python ocr_validation.py

# Evaluate single image
python ocr_validation.py --single 0001 --verbose

# Save results to JSON
python ocr_validation.py --output ocr_results.json

# Generate visualization images
python ocr_validation.py --visualize viz_output/
```

## 2. YOLO Training Pipeline

Train YOLOv8-nano for PII detection.

### Step 1: Prepare Dataset

Convert annotations to YOLO format:

```bash
# Prepare dataset with default settings (simplified 5-class)
python yolo_train.py prepare

# Use detailed 18-class mapping
python yolo_train.py prepare --detailed-classes

# Custom train/val split
python yolo_train.py prepare --train-split 0.9
```

Output: `yolo_dataset/` with YOLO-formatted labels and `dataset.yaml`

### Step 2: Train Model

```bash
# Train YOLOv8-nano (default)
python yolo_train.py train

# Custom training options
python yolo_train.py train --model-size n --epochs 100 --batch 16 --imgsz 640

# Resume training
python yolo_train.py train --resume
```

### Step 3: Export Model (Optional)

```bash
python yolo_train.py export --weights runs/pii_detection_v8n/weights/best.pt --format onnx
```

### Visualize Dataset

Verify annotations are correct:

```bash
python yolo_train.py visualize --num-samples 10
```

## 3. YOLO Inference & Evaluation

Run inference and evaluate trained models.

```bash
# Single image inference
python yolo_inference.py infer --weights runs/pii_detection_v8n/weights/best.pt --image test.png

# Inference with ground truth comparison
python yolo_inference.py infer --weights best.pt --image 0001.png --annotation 0001.json --output viz.png

# Evaluate on full dataset
python yolo_inference.py evaluate --weights best.pt

# Batch inference on directory
python yolo_inference.py batch --weights best.pt --images-dir test_images/ --output-dir predictions/
```

## Class Mapping

### Simplified (5 classes, default):
| Class ID | Name    | PII Keys |
|----------|---------|----------|
| 0        | name    | FIRSTNAME, LASTNAME, USERNAME |
| 1        | contact | EMAIL, PHONE |
| 2        | address | STREET, CITY, STATE_ABBR, POSTCODE, ADDRESS, COUNTRY |
| 3        | card    | CARD_NUMBER, CARD_LAST4, CARD_CVV, CARD_EXPIRY_* |
| 4        | account | ACCOUNT_ID, AVATAR |

### Detailed (18 classes):
Each PII key gets its own class ID.

## Files

- `ocr_validation.py` - OCR-based PII detection and evaluation
- `yolo_train.py` - YOLOv8 training pipeline (prepare, train, export, visualize)
- `yolo_inference.py` - YOLO inference and evaluation
- `benchmark.py` - common benchmark runner across detector implementations
- `requirements.txt` - Python dependencies
