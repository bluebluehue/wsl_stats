# WSL Fantasy stats

A WSL Fantasy player-data parser modelled on the NWSL fantasy stats project.

This project reads the public JSON feeds used by the WSL Fantasy create-team interface and writes a `transformed_data.json` file consumed by the included table viewer.

## What it creates

- `transformed_data.json` — normalized player table data
- `player_history.json` — daily snapshots of value and selected percentage
- `fixtures.json` — normalized fixture feed for debugging/reference
- `teams.json` — normalized team feed for debugging/reference

## Run locally

```bash
python -m pip install -e .
python get_data.py
python -m http.server 8000
```

Then open `http://localhost:8000/`.

## GitHub Pages setup

Use the included GitHub Action (`.github/workflows/refresh-data.yml`) to refresh the data and commit the generated JSON files back to the repository.

Recommended repo setup:

1. Create a new repository named something like `wsl_stats`.
2. Upload these files to the repository root.
3. Go to **Settings → Actions → General → Workflow permissions** and choose **Read and write permissions**.
4. Go to **Settings → Pages** and set the source to **Deploy from a branch**, branch `main`, folder `/root`.
5. Go to **Actions → Refresh WSL Fantasy data → Run workflow**.

## Notes

- This is intentionally a new repo rather than a fork so the WSL parser can evolve separately from the NWSL parser.
- The WSL feeds expose preseason player price, selected percentage, previous-season fantasy points, upcoming fixtures, Opta IDs, and current fixture ratings.
- Once matchweeks have been played, if the feed adds match-by-match scoring fields, `get_data.py` can be extended to populate the per-matchweek columns with richer scoring tooltips.
