# UI Reproducer Notes

`prompts.py` and `screenshot_pages.py` need to stay aligned: the generated React
markup should expose the attributes that the screenshot/annotation pipeline
expects.

Keep `template/` minimal and stable. Avoid broad prompt rewrites unless they are
needed for a measured reproduction or annotation issue.

Generated screenshots, output projects, logs, and review state are local
artifacts and should stay out of the review branch unless intentionally packaged
as a small fixture.
