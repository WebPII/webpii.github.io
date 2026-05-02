# Versioning & Selective Processing

This document describes how to use timestamp-based filtering to selectively process reproductions.

## How It Works

Every reproduction creates a timestamped output directory:
```
output/{device}/{company}/{page_type}/{image_id}/{timestamp}/
                                                  └── 20260113_210000
```

The `test_workflow.py` script records each run in `.test_workflow_state.json`:
```json
{
  "processed": ["data/ui_images/..."],
  "runs": [
    {
      "timestamp": "20260113_210000",
      "image": "data/ui_images/account-dashboard/1234-amazon-desktop.png",
      "output_dir": "ui_reproducer/output/desktop/amazon/account-dashboard/1234/20260113_210000"
    }
  ],
  "last_run": "2026-01-13T21:00:00"
}
```

## Filtering by Timestamp

Use `--after` in `screenshot_pages.py` to only process pages created after a certain time:

```bash
# Only pages from today (Jan 13, 2026)
python screenshot_pages.py --after 20260113

# Only pages after 3pm today
python screenshot_pages.py --after 20260113_150000

# Combine with other filters
python screenshot_pages.py --after 20260113 --page-filter amazon --workers 4
```

## Typical Workflow

1. **Make changes** to prompts.py or other generation code

2. **Run test_workflow** to generate new reproductions:
   ```bash
   python test_workflow.py --iterations 3
   ```

3. **Note the timestamp** from the run (printed in output, or check `.test_workflow_state.json`)

4. **Screenshot only new outputs**:
   ```bash
   python screenshot_pages.py --after 20260113_210000
   ```

## Use Cases

### A/B Testing Prompt Changes
```bash
# Run batch with old prompts
python test_workflow.py  # Note: outputs at 20260113_100000

# Make prompt changes...

# Run batch with new prompts
python test_workflow.py  # Note: outputs at 20260113_140000

# Screenshot only the new batch
python screenshot_pages.py --after 20260113_140000
```

### Re-screenshotting After Detection Changes
```bash
# After modifying screenshot_pages.py detection logic,
# re-screenshot recent reproductions without re-running the expensive LLM step
python screenshot_pages.py --after 20260113
```

### Checking State File
```bash
# See recent runs
cat .test_workflow_state.json | jq '.runs[-5:]'

# Get timestamp of last run
cat .test_workflow_state.json | jq -r '.runs[-1].timestamp'
```

## Timestamp Format

Timestamps use `YYYYMMDD_HHMMSS` format which sorts lexicographically:
- `20260113` - matches all times on Jan 13, 2026
- `20260113_150000` - matches 3:00:00 PM and later on Jan 13
- `202601` - matches all of January 2026
