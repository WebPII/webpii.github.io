# OpenReview Code Submission Notes

## Recommended Review Setup

Use an anonymized code URL for double-blind review. The current upstream
repository is private and owned by an author account, so the direct GitHub URL
is not reviewer-accessible and is not appropriate for the `Code URL` field.

Recommended path:

1. Push a clean review branch, for example `neurips2026-code-release`.
2. Create an anonymized mirror URL with a service such as Anonymous GitHub.
3. Paste the anonymized URL into OpenReview's `Code URL` field.
4. Keep the repository accessible and unchanged through review unless a
   permitted update is needed before the full-paper deadline.

If the submission is intentionally single-blind, the direct GitHub URL can be
used only after the repository is made accessible to reviewers.

## Suggested Code URL Field

Paste the anonymized mirror URL, not the private author-owned GitHub URL:

```text
https://anonymous.4open.science/r/<ANONYMIZED_REPOSITORY_ID>/
```

## Suggested Code Submission Justification

Leave this field blank when a code URL is supplied. WebPII includes executable
benchmark/data-generation and detector-evaluation artifacts, so code release is
needed for review.

## Pre-Submission Checklist

- The code URL is accessible in a private/incognito browser session.
- The visible repository contents do not contain author names, emails, local
  absolute paths, institution names, or personal GitHub handles.
- `README.md` documents setup, dependencies, smoke test, dataset workflow, and
  model training/evaluation.
- `example_data/` is sufficient for a quick executable smoke test.
- The full dataset URL and Croissant file are submitted separately in the
  dataset fields.
- The chosen license in OpenReview matches the license intended for the paper,
  dataset, and released code.

## Local Anonymity Check

Run:

```bash
python scripts/check_release.py
```

This scans tracked text files for common identity/path leaks before creating the
anonymous mirror.
