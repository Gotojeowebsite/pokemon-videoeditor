# pokemon-videoeditor

Python batch editor for Pokemon gameplay episodes.

Drop raw footage into one folder, run one command, and get:

- final edited videos in `data/final_cuts`
- automatic timeline overlay updates for team, catches, fallen, location, and PC box
- automatic team and graveyard icon rendering (auto-fetched when missing)
- RIP visual + sad music window when a death happens
- persistent `data/processed_log.json` with duplicate auto-skip behavior
- persistent `data/dead_pokemon_log.json` with all detected deaths
- persistent `data/state_timeline_log.json` with catches/fallen/location/pc-box timeline snapshots

No manual sidecar is required for base automation. OCR now attempts to infer death, catch, and location events directly from gameplay text.

## What this build does

- Scans `data/raw_footage` for supported video files.
- Checks each file against `data/processed_log.json` using SHA256 + perceptual frame hashes.
- Auto-skips likely duplicates and logs the decision.
- Loads per-video sidecar events from `data/events/<video_name>.events.json`.
- Runs OCR pass to infer death, catch, and location events from gameplay text.
- Merges sidecar and OCR events into a timeline.
- Renders final output with:
	- panel-style template overlay
	- timeline-accurate updates to catches, fallen, location, and pc box
	- graveyard icon strip updates while the video plays
	- RIP card for death windows
	- gameplay audio ducking during RIP windows
	- optional sad music mix during RIP windows if `assets/music/sad_theme.mp3` exists
- Writes final output to `data/final_cuts/<video_name>.final.mp4`.
- Appends deduplicated death history into `data/dead_pokemon_log.json`.
- Writes state timeline history into `data/state_timeline_log.json` for auditing precision.
- Restores graveyard continuity from `data/dead_pokemon_log.json` at startup so fallen history carries forever across episodes.

## Setup

1. Install FFmpeg and make sure `ffmpeg` and `ffprobe` are available in your PATH.
2. Create and activate a Python virtual environment.
3. Install Python dependencies:

```powershell
pip install -r requirements.txt
```

4. (OCR) Install Tesseract OCR and ensure `tesseract` is available in your PATH.
5. Keep internet access enabled if you want missing Pokemon icons to auto-download during processing.

## Folder layout

```
pokemon-videoeditor/
	assets/
		icons/
			pikachu.png              # lowercase sanitized name; used in graveyard icon panel
		music/
			sad_theme.mp3            # optional but recommended
		template/
			base.png                 # optional full-frame template base
			flowers.png              # reserved for future PNG flower overlay support
	data/
		_runtime/
			overlay_frames/          # generated per-run overlay frames
		raw_footage/               # input videos
		events/                    # per-video sidecar events
		final_cuts/                # output videos
		processed_log.json         # persistent run history and duplicate records
		dead_pokemon_log.json      # persistent dead-pokemon history
		state_timeline_log.json    # persistent per-video state timeline history
		team_state.json            # ongoing team state carried across episodes
```

## Event sidecar format

For `data/raw_footage/episode01.mp4`, use:

`data/events/episode01.events.json`

Example:

```json
{
	"video": "episode01.mp4",
	"events": [
		{
			"timestamp": 45.2,
			"type": "catch",
			"pokemon": "Pidgey",
			"location": "Route 1"
		},
		{
			"timestamp": 310.0,
			"type": "death",
			"pokemon": "Pidgey"
		},
		{
			"timestamp": 420.0,
			"type": "swap_to_team",
			"pokemon": "Geodude"
		},
		{
			"timestamp": 500.0,
			"type": "move_to_box",
			"pokemon": "Rattata"
		},
		{
			"timestamp": 650.0,
			"type": "location",
			"location": "Violet City"
		}
	]
}
```

Supported `type` values now:

- `catch`
- `death`
- `swap_to_team`
- `move_to_box`
- `location`

## Run

From the repo root:

```powershell
python -m src.main
```

Force processing even if duplicate confidence is high:

```powershell
python -m src.main --force
```

## Notes

- Duplicate behavior is currently auto-skip by default.
- Team state is persisted and updated in `data/team_state.json` after each processed video.
- Every death event is persisted to `data/dead_pokemon_log.json` (deduped by video + pokemon + timestamp).
- Timeline state transitions (catches/fallen/location/pc box) are persisted to `data/state_timeline_log.json`.
- Sidecar event files are optional overrides for precision; the bot works in OCR-only mode.
- If a video fails, a `failed` entry is still written to `data/processed_log.json`.
- Pokemon icons are auto-fetched and cached into `assets/icons` when missing.
- You can still pre-seed custom icons in `assets/icons` using sanitized names, e.g. `Mr Mime` -> `mr_mime.png`.
