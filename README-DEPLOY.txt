# GogginsTube: corrected project files

## Files changed

- `pipeline.py`: tracks all search origins for each video, broadens longevity discovery, reports API errors clearly, handles Gemini responses safely, merges new discoveries into the saved library, and writes JSON files atomically.
- `.github/workflows/update.yml`: uses the real GitHub Actions location, persists `labels.json`, prevents overlapping runs, and runs a Python syntax check.
- `site/index.html`: requests fresh JSON data without reusing a stale browser HTTP cache.
- `site/sw.js`: bumps the service-worker cache version so clients refresh the updated site shell.

## Install these files

Copy the contents of this folder into the root of your GitHub repository, preserving the directory structure. Allow the files to overwrite the existing files.

The active workflow must be located at:

`.github/workflows/update.yml`

The old root-level `update.yml` was intentionally omitted because GitHub Actions does not run workflow files from the repository root.

## GitHub repository secrets

In Settings -> Secrets and variables -> Actions -> Repository secrets, make sure these exact secrets exist:

- `YOUTUBE_API_KEY`
- `GEMINI_API_KEY`

The workflow also accepts an optional repository variable named `GEMINI_MODEL`. If it is not set, the pipeline uses `gemini-3.5-flash-lite`.

Do not put API keys in the repository or in the website files.

## Run it

1. Commit the replaced files to the `main` branch.
2. Open Actions -> Update GogginsTube.
3. Select Run workflow.
4. Open the run and inspect the log if it fails.
5. On success, refresh the deployed site.

The pipeline will keep existing videos and papers if a run discovers no new items. It will stop before changing the website data if YouTube discovery fails completely.

## Important behaviour

- The YouTube searches return candidates. Gemini still decides which videos are relevant.
- Videos shorter than four minutes are excluded.
- The pipeline performs one all-time search per configured query plus two broad recent-only searches (24 `search.list` calls per full run with the current query list). YouTube currently documents a default limit of 100 `search.list` calls per day, so avoid repeatedly starting the workflow while testing.
- The Research tab contains recent papers accepted by Gemini. A successful paper search that finds no matching papers does not erase the existing paper library.
- `labels.json` stores Gemini's previous decisions. The workflow now commits this file so the same items do not need to be judged again on every run.

## Protect your current data

The three generated data files (`site/videos.json`, `site/papers.json`, `site/status.json`) are intentionally not included in this replacement package. Do not replace them with empty placeholder files. The pipeline creates or updates them during its next successful run and preserves existing entries.

If those files already exist in your repository, leave them in place.
