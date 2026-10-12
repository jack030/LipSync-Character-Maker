# Character + Viseme Pack Lip-Sync Editor (200×200)

This is an updated version of the previous lip-sync mouth editor. It supports choosing a character pack and a viseme folder from drop-down menus, then tuning the mouth size and position before rendering. It saves every output with a unique timestamp.

## Install and start

1. Install **Python 3.10+** and the Python packages:

   ```bash
   pip install pillow pydub
   ```

   `pygame` is optional for playing selected audio chunks (`pip install pygame`).

2. Install **FFmpeg** and **Rhubarb Lip Sync**. Update `RHUBARB_BIN` near the top of `lipsync_pack_manager.py` to point to your `rhubarb.exe`. FFmpeg must be on the system PATH (or `imageio_ffmpeg` installed).
3. Put `lipsync_pack_manager.py` next to your `character` folder.
4. Run:

   ```bash
   python lipsync_pack_manager.py
   ```

5. Choose a **Character pack** and **Viseme pack** in the mouth editor. You can change X/Y, scale, crop, and target center separately for each character/viseme combination. Click **Save and continue to audio**. Choose an audio file, then choose emotions for its segments.

## Folder layout

```text
project/
├── lipsync_pack_manager.py
├── character/
│   ├── expressions/                # existing legacy/default character
│   │   ├── neutral.png
│   │   ├── happy.png
│   │   ├── sad.png
│   │   ├── angry.png
│   │   └── eyes_closed.png
│   ├── body.png                    # optional for legacy character
│   ├── visemes/
│   │   ├── default/                # default/shared viseme folder
│   │   │   ├── A.png
│   │   │   ├── B.png
│   │   │   ├── C.png
│   │   │   ├── D.png
│   │   │   ├── E.png
│   │   │   ├── F.png
│   │   │   ├── G.png
│   │   │   ├── H.png
│   │   │   └── X.png                # required: silence
│   │   └── cartoon/                # your extra viseme pack
│   │       ├── A.png ... H.png
│   │       └── X.png
│   └── packs/
│       └── politic/                # example name: use any name you want
│           ├── expressions/
│           │   ├── neutral.png
│           │   ├── happy.png
│           │   ├── sad.png
│           │   └── angry.png
│           ├── body.png            # optional
│           └── visemes/
│               ├── default/
│               │   ├── A.png ... H.png
│               │   └── X.png
│               └── speaking/
│                   ├── A.png ... H.png
│                   └── X.png
└── output/
    ├── final_politic_20261012_220651_123456.mp4
    ├── final_politic_20261012_220651_123456_first_frame.png
    └── final_politic_20261012_220651_123456_ffmpeg.log
```

Each sprite should be **200×200 pixels**. Viseme sprites may use an alpha-transparent upper portion; the recommended **mouth_only** overlay mode extracts just the mouth patch, avoiding seams across the eyes. Every viseme folder needs `X.png`; missing other shapes fall back to X with a console warning.

Character packs also work under `character/<name>/expressions/` or `characters/<name>/expressions/`. If a custom character pack has no local visemes (or you want to reuse a set), the menu also offers shared packs from `character/visemes/`.

**To add packs:** Create a folder in `character/visemes/` for a shared viseme pack, or `character/packs/<name>/` for a new character. The folders appear automatically at startup, or click **Refresh folders** while the editor is open.

The program writes `mouth_settings.json`, keeping the position/size settings for every character-and-viseme pair separately. Existing `mouth_settings.json` files from the original editor are migrated automatically when saved.

## Timestamped outputs

Every render produces a uniquely named file, for example:

```text
output/final_politic_20261012_220651_123456.mp4
```

The original is never overwritten, even when multiple renders finish close together. Mouth previews, debug first frames, and FFmpeg logs also get distinct filenames.

## Optional command-line operations

```bash
python lipsync_pack_manager.py --list-packs
python lipsync_pack_manager.py --preview-only --character-pack default --viseme-pack default --emotion angry --viseme A
python lipsync_pack_manager.py --preview-only --character-pack politic --viseme-pack speaking --emotion happy --viseme E
```

No Rhubarb or audio file is needed for `--preview-only`; this is useful for inspecting mouth alignment.

## Troubleshooting

- Missing **X.png**: add it to the selected viseme folder before rendering.
- Mouth in wrong position: change **Mouth X/Y** and **Size**, then try **Advanced: crop / mouth center** for a different art style.
- Black frame: check the timestamped `*_first_frame.png` in `output/`; the renderer composites expression+viseme onto an opaque RGB frame before video encoding.
- Rhubarb executable not found: update `RHUBARB_BIN`.
- Additional viseme/character folder doesn't appear: ensure it has actual image files and click **Refresh folders**.
