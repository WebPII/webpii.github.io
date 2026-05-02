#!/usr/bin/env python3
"""
YOLOv8-nano Training Pipeline for PII Field Detection

This script sets up a training pipeline for YOLOv8-nano to detect PII fields
in UI screenshots using the ground truth annotations from screenshots/.

Key features:
- Converts JSON annotations to YOLO format
- Handles train/val split
- Configures YOLOv8 training with appropriate hyperparameters
- Supports class-based PII detection (different PII types as different classes)
"""

import json
import os
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# PII class mapping - map PII keys to class indices
PII_CLASSES = {
    "PII_FIRSTNAME": 0,
    "PII_LASTNAME": 1,
    "PII_EMAIL": 2,
    "PII_PHONE": 3,
    "PII_STREET": 4,
    "PII_CITY": 5,
    "PII_STATE_ABBR": 6,
    "PII_POSTCODE": 7,
    "PII_ADDRESS": 8,
    "PII_CARD_NUMBER": 9,
    "PII_CARD_LAST4": 10,
    "PII_CARD_CVV": 11,
    "PII_CARD_EXPIRY_MONTH": 12,
    "PII_CARD_EXPIRY_YEAR": 13,
    "PII_CARD_EXPIRY": 14,
    "PII_ACCOUNT_ID": 15,
    "PII_FULLNAME": 16,
    "PII_AVATAR": 17,
}

# Simplified class mapping - group related PII types
PII_CLASSES_SIMPLIFIED = {
    "name": 0,       # firstname, lastname, username
    "contact": 1,    # email, phone
    "address": 2,    # street, city, state, postcode, full address
    "card": 3,       # card number, last4, cvv, expiry
    "account": 4,    # account_id, avatar
}

# Map detailed keys to simplified classes
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


def require_dependency(module: str, package: str | None = None):
    """Import an optional prediction dependency with a reviewer-friendly error."""
    try:
        return __import__(module)
    except ImportError as exc:
        install_name = package or module
        raise SystemExit(
            f"Missing dependency '{module}'. Install prediction dependencies with:\n"
            f"  pip install -r predict/requirements.txt\n"
            f"or install {install_name!r} directly."
        ) from exc


@dataclass
class YOLOAnnotation:
    class_id: int
    x_center: float  # normalized 0-1
    y_center: float  # normalized 0-1
    width: float     # normalized 0-1
    height: float    # normalized 0-1

    def to_line(self) -> str:
        return f"{self.class_id} {self.x_center:.6f} {self.y_center:.6f} {self.width:.6f} {self.height:.6f}"


def convert_bbox_to_yolo(
    bbox: dict,
    img_width: int,
    img_height: int
) -> tuple[float, float, float, float]:
    """
    Convert bbox dict (x, y, width, height) to YOLO format (x_center, y_center, w, h) normalized.
    """
    x = bbox["x"]
    y = bbox["y"]
    w = bbox["width"]
    h = bbox["height"]

    # Clamp to image bounds
    x = max(0, min(x, img_width))
    y = max(0, min(y, img_height))
    w = min(w, img_width - x)
    h = min(h, img_height - y)

    # Convert to center coordinates and normalize
    x_center = (x + w / 2) / img_width
    y_center = (y + h / 2) / img_height
    w_norm = w / img_width
    h_norm = h / img_height

    return x_center, y_center, w_norm, h_norm


def load_image_dimensions(image_path: Path) -> tuple[int, int]:
    """Load image and return (width, height)."""
    from PIL import Image

    try:
        with Image.open(image_path) as img:
            return img.width, img.height
    except Exception as exc:
        raise ValueError(f"Could not load image: {image_path}") from exc


def convert_annotation_to_yolo(
    json_path: Path,
    image_path: Path,
    use_simplified_classes: bool = True,
    visible_only: bool = True
) -> list[YOLOAnnotation]:
    """
    Convert a single annotation JSON to YOLO format annotations.

    Args:
        json_path: Path to annotation JSON
        image_path: Path to corresponding image
        use_simplified_classes: Use simplified 5-class mapping vs detailed 18-class
        visible_only: Only include visible PII elements
    """
    with open(json_path) as f:
        data = json.load(f)

    img_width, img_height = load_image_dimensions(image_path)

    annotations = []

    for elem in data.get("pii_elements", []):
        # Skip invisible elements if requested
        if visible_only and not elem.get("visible", False):
            continue

        pii_key = elem["key"]
        bbox = elem["bbox"]

        # Determine class ID
        if use_simplified_classes:
            simplified_name = PII_KEY_TO_SIMPLIFIED.get(pii_key)
            if simplified_name is None:
                continue
            class_id = PII_CLASSES_SIMPLIFIED[simplified_name]
        else:
            if pii_key not in PII_CLASSES:
                continue
            class_id = PII_CLASSES[pii_key]

        # Convert bbox
        x_center, y_center, w_norm, h_norm = convert_bbox_to_yolo(
            bbox, img_width, img_height
        )

        # Skip invalid boxes
        if w_norm <= 0 or h_norm <= 0:
            continue

        annotations.append(YOLOAnnotation(
            class_id=class_id,
            x_center=x_center,
            y_center=y_center,
            width=w_norm,
            height=h_norm
        ))

    return annotations


def prepare_dataset(
    screenshots_dir: Path,
    output_dir: Path,
    train_split: float = 0.8,
    use_simplified_classes: bool = True,
    visible_only: bool = True,
    seed: int = 42
) -> dict:
    """
    Prepare YOLO dataset from screenshots directory.

    Creates:
        output_dir/
            images/
                train/
                val/
            labels/
                train/
                val/
            dataset.yaml
    """
    random.seed(seed)

    # Create directory structure
    for split in ["train", "val"]:
        (output_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (output_dir / "labels" / split).mkdir(parents=True, exist_ok=True)

    # Find all annotation files
    json_files = sorted(screenshots_dir.glob("*.json"))
    json_files = [f for f in json_files if f.name != "manifest.json"]

    # Shuffle and split
    random.shuffle(json_files)
    split_idx = int(len(json_files) * train_split)
    train_files = json_files[:split_idx]
    val_files = json_files[split_idx:]

    stats = {
        "total_images": len(json_files),
        "train_images": len(train_files),
        "val_images": len(val_files),
        "total_annotations": 0,
        "train_annotations": 0,
        "val_annotations": 0,
        "class_counts": {},
    }

    def process_files(files: list[Path], split: str):
        for json_path in files:
            image_path = json_path.with_suffix(".png")
            if not image_path.exists():
                print(f"Warning: Image not found for {json_path.name}")
                continue

            # Convert annotations
            annotations = convert_annotation_to_yolo(
                json_path, image_path,
                use_simplified_classes=use_simplified_classes,
                visible_only=visible_only
            )

            if not annotations:
                print(f"Warning: No valid annotations in {json_path.name}")
                continue

            # Copy image
            dest_image = output_dir / "images" / split / image_path.name
            shutil.copy2(image_path, dest_image)

            # Write labels
            dest_label = output_dir / "labels" / split / f"{json_path.stem}.txt"
            with open(dest_label, "w") as f:
                for ann in annotations:
                    f.write(ann.to_line() + "\n")

                    # Update stats
                    stats["total_annotations"] += 1
                    if split == "train":
                        stats["train_annotations"] += 1
                    else:
                        stats["val_annotations"] += 1

                    class_name = list(PII_CLASSES_SIMPLIFIED.keys())[ann.class_id] if use_simplified_classes else list(PII_CLASSES.keys())[ann.class_id]
                    stats["class_counts"][class_name] = stats["class_counts"].get(class_name, 0) + 1

    print("Processing training files...")
    process_files(train_files, "train")

    print("Processing validation files...")
    process_files(val_files, "val")

    # Create dataset.yaml
    class_names = list(PII_CLASSES_SIMPLIFIED.keys()) if use_simplified_classes else list(PII_CLASSES.keys())

    dataset_config = {
        "path": str(output_dir.absolute()),
        "train": "images/train",
        "val": "images/val",
        "nc": len(class_names),
        "names": class_names
    }

    yaml = require_dependency("yaml", "PyYAML")
    yaml_path = output_dir / "dataset.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(dataset_config, f, default_flow_style=False)

    stats["yaml_path"] = str(yaml_path)

    # Save stats
    stats_path = output_dir / "dataset_stats.json"
    with open(stats_path, "w") as f:
        json.dump(stats, f, indent=2)

    print(f"\nDataset prepared at: {output_dir}")
    print(f"Training images: {stats['train_images']}")
    print(f"Validation images: {stats['val_images']}")
    print(f"Total annotations: {stats['total_annotations']}")
    print(f"Class distribution: {stats['class_counts']}")

    return stats


def train_yolov8(
    dataset_yaml: Path,
    output_dir: Path,
    model_size: str = "n",  # n=nano, s=small, m=medium, l=large, x=xlarge
    epochs: int = 100,
    imgsz: int = 640,
    batch: int = 16,
    device: Optional[str] = None,
    resume: bool = False,
    pretrained: bool = True,
    augment: bool = True,
    **kwargs
) -> Path:
    """
    Train YOLOv8 model for PII detection.

    Args:
        dataset_yaml: Path to dataset.yaml file
        output_dir: Output directory for training results
        model_size: Model size variant (n, s, m, l, x)
        epochs: Number of training epochs
        imgsz: Input image size
        batch: Batch size
        device: Device to train on (None for auto, '0' for GPU 0, 'cpu' for CPU)
        resume: Resume from last checkpoint
        pretrained: Use pretrained weights
        augment: Enable data augmentation

    Returns:
        Path to best model weights
    """
    try:
        from ultralytics import YOLO
    except ImportError:
        raise ImportError("Please install ultralytics: pip install ultralytics")

    # Load model
    model_name = f"yolov8{model_size}.pt" if pretrained else f"yolov8{model_size}.yaml"
    model = YOLO(model_name)

    # Configure training arguments
    train_args = {
        "data": str(dataset_yaml),
        "epochs": epochs,
        "imgsz": imgsz,
        "batch": batch,
        "project": str(output_dir),
        "name": f"pii_detection_v8{model_size}",
        "exist_ok": True,
        "pretrained": pretrained,
        "augment": augment,
        "verbose": True,
        "save": True,
        "save_period": 10,
        "val": True,
        "plots": True,
    }

    if device is not None:
        train_args["device"] = device

    if resume:
        train_args["resume"] = True

    # Additional hyperparameters for small dataset
    train_args.update({
        "patience": 20,  # Early stopping patience
        "lr0": 0.01,     # Initial learning rate
        "lrf": 0.01,     # Final learning rate factor
        "warmup_epochs": 3,
        "warmup_momentum": 0.8,
        "warmup_bias_lr": 0.1,
        "box": 7.5,      # Box loss gain
        "cls": 0.5,      # Class loss gain
        "dfl": 1.5,      # DFL loss gain
        "mosaic": 0.5 if augment else 0.0,
        "mixup": 0.1 if augment else 0.0,
        "copy_paste": 0.1 if augment else 0.0,
    })

    # Override with any additional kwargs
    train_args.update(kwargs)

    print(f"\nStarting YOLOv8{model_size} training...")
    print(f"Dataset: {dataset_yaml}")
    print(f"Output: {output_dir}")
    print(f"Epochs: {epochs}")
    print(f"Image size: {imgsz}")
    print(f"Batch size: {batch}")

    # Train
    results = model.train(**train_args)

    # Return path to best weights
    best_weights = output_dir / f"pii_detection_v8{model_size}" / "weights" / "best.pt"
    print(f"\nTraining complete!")
    print(f"Best weights: {best_weights}")

    return best_weights


def export_model(
    weights_path: Path,
    export_format: str = "onnx",
    imgsz: int = 640,
    **kwargs
) -> Path:
    """
    Export trained model to different formats.

    Args:
        weights_path: Path to trained weights (.pt file)
        export_format: Export format (onnx, torchscript, coreml, etc.)
        imgsz: Export image size
    """
    try:
        from ultralytics import YOLO
    except ImportError:
        raise ImportError("Please install ultralytics: pip install ultralytics")

    model = YOLO(str(weights_path))

    print(f"Exporting model to {export_format}...")
    export_path = model.export(format=export_format, imgsz=imgsz, **kwargs)

    print(f"Model exported to: {export_path}")
    return Path(export_path)


def visualize_annotations(
    dataset_dir: Path,
    output_dir: Path,
    num_samples: int = 5,
    split: str = "train"
):
    """
    Visualize YOLO annotations to verify conversion is correct.
    """
    import random
    cv2 = require_dependency("cv2", "opencv-python")
    yaml = require_dependency("yaml", "PyYAML")

    images_dir = dataset_dir / "images" / split
    labels_dir = dataset_dir / "labels" / split

    # Load class names from yaml
    yaml_path = dataset_dir / "dataset.yaml"
    with open(yaml_path) as f:
        config = yaml.safe_load(f)
    class_names = config["names"]

    # Random color per class
    colors = [(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
              for _ in range(len(class_names))]

    output_dir.mkdir(parents=True, exist_ok=True)

    image_files = list(images_dir.glob("*.png"))
    samples = random.sample(image_files, min(num_samples, len(image_files)))

    for img_path in samples:
        label_path = labels_dir / (img_path.stem + ".txt")

        img = cv2.imread(str(img_path))
        h, w = img.shape[:2]

        if label_path.exists():
            with open(label_path) as f:
                for line in f:
                    parts = line.strip().split()
                    class_id = int(parts[0])
                    x_center, y_center, box_w, box_h = map(float, parts[1:5])

                    # Convert to pixel coordinates
                    x1 = int((x_center - box_w / 2) * w)
                    y1 = int((y_center - box_h / 2) * h)
                    x2 = int((x_center + box_w / 2) * w)
                    y2 = int((y_center + box_h / 2) * h)

                    color = colors[class_id]
                    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(img, class_names[class_id], (x1, y1 - 5),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        output_path = output_dir / f"viz_{img_path.name}"
        cv2.imwrite(str(output_path), img)
        print(f"Saved: {output_path}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="YOLOv8 training pipeline for PII detection")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Prepare dataset command
    prep_parser = subparsers.add_parser("prepare", help="Prepare YOLO dataset from screenshots")
    prep_parser.add_argument("--screenshots-dir", type=Path,
                             default=Path(__file__).parent.parent / "ui_reproducer" / "screenshots",
                             help="Path to screenshots directory")
    prep_parser.add_argument("--output-dir", type=Path,
                             default=Path(__file__).parent / "yolo_dataset",
                             help="Output directory for YOLO dataset")
    prep_parser.add_argument("--train-split", type=float, default=0.8,
                             help="Train/val split ratio")
    prep_parser.add_argument("--detailed-classes", action="store_true",
                             help="Use detailed 18-class mapping instead of simplified 5-class")
    prep_parser.add_argument("--include-invisible", action="store_true",
                             help="Include invisible PII elements in training")
    prep_parser.add_argument("--seed", type=int, default=42,
                             help="Random seed for reproducibility")

    # Train command
    train_parser = subparsers.add_parser("train", help="Train YOLOv8 model")
    train_parser.add_argument("--dataset", type=Path,
                              default=Path(__file__).parent / "yolo_dataset" / "dataset.yaml",
                              help="Path to dataset.yaml")
    train_parser.add_argument("--output-dir", type=Path,
                              default=Path(__file__).parent / "runs",
                              help="Output directory for training")
    train_parser.add_argument("--model-size", type=str, default="n",
                              choices=["n", "s", "m", "l", "x"],
                              help="Model size (n=nano, s=small, etc.)")
    train_parser.add_argument("--epochs", type=int, default=100,
                              help="Number of training epochs")
    train_parser.add_argument("--imgsz", type=int, default=640,
                              help="Input image size")
    train_parser.add_argument("--batch", type=int, default=16,
                              help="Batch size")
    train_parser.add_argument("--device", type=str, default=None,
                              help="Device (None=auto, '0'=GPU0, 'cpu')")
    train_parser.add_argument("--resume", action="store_true",
                              help="Resume from last checkpoint")
    train_parser.add_argument("--no-pretrained", action="store_true",
                              help="Train from scratch without pretrained weights")
    train_parser.add_argument("--no-augment", action="store_true",
                              help="Disable data augmentation")

    # Export command
    export_parser = subparsers.add_parser("export", help="Export trained model")
    export_parser.add_argument("--weights", type=Path, required=True,
                               help="Path to trained weights")
    export_parser.add_argument("--format", type=str, default="onnx",
                               choices=["onnx", "torchscript", "coreml", "tflite", "engine"],
                               help="Export format")
    export_parser.add_argument("--imgsz", type=int, default=640,
                               help="Export image size")

    # Visualize command
    viz_parser = subparsers.add_parser("visualize", help="Visualize dataset annotations")
    viz_parser.add_argument("--dataset-dir", type=Path,
                            default=Path(__file__).parent / "yolo_dataset",
                            help="Path to YOLO dataset directory")
    viz_parser.add_argument("--output-dir", type=Path,
                            default=Path(__file__).parent / "viz_output",
                            help="Output directory for visualizations")
    viz_parser.add_argument("--num-samples", type=int, default=5,
                            help="Number of samples to visualize")
    viz_parser.add_argument("--split", type=str, default="train",
                            choices=["train", "val"],
                            help="Dataset split to visualize")

    args = parser.parse_args()

    if args.command == "prepare":
        prepare_dataset(
            screenshots_dir=args.screenshots_dir,
            output_dir=args.output_dir,
            train_split=args.train_split,
            use_simplified_classes=not args.detailed_classes,
            visible_only=not args.include_invisible,
            seed=args.seed
        )

    elif args.command == "train":
        train_yolov8(
            dataset_yaml=args.dataset,
            output_dir=args.output_dir,
            model_size=args.model_size,
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            device=args.device,
            resume=args.resume,
            pretrained=not args.no_pretrained,
            augment=not args.no_augment
        )

    elif args.command == "export":
        export_model(
            weights_path=args.weights,
            export_format=args.format,
            imgsz=args.imgsz
        )

    elif args.command == "visualize":
        visualize_annotations(
            dataset_dir=args.dataset_dir,
            output_dir=args.output_dir,
            num_samples=args.num_samples,
            split=args.split
        )


if __name__ == "__main__":
    main()
