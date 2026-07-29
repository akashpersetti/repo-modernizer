# RepoModernizer Demo Script

Recording checklist for the demo GIF/video (~90 seconds).

1. Open the live dashboard: https://d12ztzghnd3aa9.cloudfront.net
2. Fill the form:
   - Repo URL: `https://github.com/akashpersetti/repomodernizer-demo-target`
   - Goal: `Migrate this Flask app to FastAPI with async route handlers.`
   - Test command: `pytest -q`
3. Click "Start migration" — page redirects to `/task?id=<task_id>`
4. Wait for the risk-gate interrupt to appear (yellow "Awaiting approval" panel with the real diff shown inline)
5. Click "Approve"
6. Wait for status to reach "Done" — green panel appears with a "View pull request" link
7. Click through to the real PR on GitHub, show the diff there too

## Capture

macOS: Cmd+Shift+5 (or QuickTime Player → New Screen Recording), trim to the steps above, convert to GIF:

```bash
ffmpeg -i recording.mov -vf "fps=12,scale=1000:-1" demo.gif
```

or, for better quality/smaller size:

```bash
gifski --fps 12 -o demo.gif recording.mov
```

Save the result as `docs/demo.gif` — the README embeds it from there.
