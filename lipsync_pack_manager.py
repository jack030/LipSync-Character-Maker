"""200x200 talking-character video maker: character packs + viseme packs.

This version extends lipsync_mouth_editor.py with:
  * Character pack picker (including character/packs/<name>/expressions).
  * Viseme pack picker (one folder per pack, e.g. character/visemes/smile).
  * Separate mouth settings per character/viseme combination.
  * Timestamped output MP4, debug PNG and FFmpeg log; never overwrite videos.
  * Mouth-only overlay, draggable X/Y/scale settings, crop + target-center tuning.
  * Non-destructive audio splitting, Rhubarb A-H/X, opaque RGB FFmpeg frames.

Folder examples:
    character/expressions/neutral.png ...
    character/visemes/default/A.png ... X.png
    character/visemes/another_style/A.png ... X.png
    character/packs/politic/expressions/neutral.png ...
    character/packs/politic/visemes/default/A.png ... X.png

Both legacy and newly added folders appear in the editor's drop-down menus.
Alternative character roots: character/<name>/ and characters/<name>/.
A character pack can also use global character/visemes/<folder> packs.

Install: pip install pillow pydub
External dependencies: FFmpeg and Rhubarb Lip Sync (set RHUBARB_BIN below).
Run: python lipsync_pack_manager.py
Preview: python lipsync_pack_manager.py --preview-only --character-pack default --viseme-pack default --viseme A
List: python lipsync_pack_manager.py --list-packs
"""
from __future__ import annotations

import argparse
import bisect
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
from pydub import AudioSegment
from pydub.silence import detect_nonsilent

try:
    import pygame  # Optional: play audio in the emotion-selection window
except ImportError:
    pygame = None

ROOT = Path(__file__).resolve().parent
CHARACTER_DIR = ROOT / 'character'
CHARACTER_PACKS_DIR = CHARACTER_DIR / 'packs'
ALT_CHARACTERS_DIR = ROOT / 'characters'
SHARED_VISEMES_DIR = CHARACTER_DIR / 'visemes'
OUTPUT_DIR = ROOT / 'output'
SETTINGS_PATH = ROOT / 'mouth_settings.json'

WIDTH, HEIGHT = 200, 200
FPS = 24
BG_COLOR = (255, 255, 255, 255)
MIN_SILENCE_LEN = 500
SILENCE_THRESH_DB = -40
MIN_CHUNK_MS = 800
ENABLE_LIPSYNC = True
ENABLE_BLINK = True
ENABLE_EXPRESSION_BLEND = True
RHUBARB_BIN = r'D:\feelip\Rhubarb-Lip-Sync-1.14.0-Windows\Rhubarb-Lip-Sync-1.14.0-Windows\rhubarb.exe'
RHUBARB_RECOGNIZER = 'phonetic'
RHUBARB_EXTENDED = 'GH'  # Rhubarb optional shapes G and H

SHAPES = 'ABCDEFGHX'
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
DEFAULT_CROP = [60, 73, 140, 129]  # original 200x200 viseme mouth region
DEFAULT_TARGET = [100, 111]       # position of mouth in 200x200 expression
SOURCE_MOUTH_CENTER = (100, 100)   # used only for full_overlay mode


@dataclass(frozen=True)
class CharacterPack:
    key: str
    label: str
    root: Path

    @property
    def expressions_dir(self) -> Path:
        return self.root / 'expressions'

    @property
    def body_path(self) -> Path:
        return self.root / 'body.png'


@dataclass(frozen=True)
class VisemePack:
    key: str
    label: str
    directory: Path


def pack_key(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def image_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir()
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
                  key=lambda p: (p.suffix.lower() != '.png', p.name.lower()))


def discover_characters() -> dict[str, CharacterPack]:
    choices = {}
    if image_files(CHARACTER_DIR / 'expressions'):
        key = pack_key(CHARACTER_DIR)
        choices[key] = CharacterPack(key, 'default  (character/)', CHARACTER_DIR)
    parents = [CHARACTER_PACKS_DIR, CHARACTER_DIR, ALT_CHARACTERS_DIR]
    skip_direct = {'expressions', 'visemes', 'packs'}
    for parent in parents:
        if not parent.is_dir():
            continue
        for subdir in sorted(parent.iterdir(), key=lambda p: p.name.lower()):
            if not subdir.is_dir():
                continue
            if parent == CHARACTER_DIR and subdir.name.lower() in skip_direct:
                continue
            if not image_files(subdir / 'expressions'):
                continue
            key = pack_key(subdir)
            choices[key] = CharacterPack(key, f'{subdir.name}  ({key}/)', subdir)
    if not choices:
        raise FileNotFoundError(
            'No character expressions found. Add PNGs under '
            'character/expressions or character/packs/<name>/expressions.'
        )
    return choices


def discover_visemes(character: CharacterPack) -> dict[str, VisemePack]:
    result = {}
    candidates = [(character.root / 'visemes', 'character')]
    if character.root != CHARACTER_DIR:
        candidates.append((SHARED_VISEMES_DIR, 'shared'))
    for root, scope in candidates:
        if not root.is_dir():
            continue
        for subdir in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if subdir.is_dir() and image_files(subdir):
                key = pack_key(subdir)
                result[key] = VisemePack(key, f'{subdir.name}  ({scope})', subdir)
    return result


def find_expressions(character: CharacterPack) -> dict[str, Path]:
    found = {}
    for path in image_files(character.expressions_dir):
        key = path.stem.lower()
        if key not in found:
            found[key] = path
    if not found:
        raise FileNotFoundError(f'No expression images in {character.expressions_dir}')
    return found


def choose_character(choices: dict[str, CharacterPack], request: str | None) -> CharacterPack:
    if request:
        if request.lower() == 'default' and 'character' in choices:
            return choices['character']
        if request in choices:
            return choices[request]
        found = [ch for ch in choices.values()
                 if ch.root.name.lower() == request.lower() or ch.label == request]
        if len(found) == 1:
            return found[0]
        raise ValueError(f'Unknown/ambiguous character pack: {request!r}. Use --list-packs.')
    return choices.get('character') or next(iter(choices.values()))


def choose_viseme(choices: dict[str, VisemePack], request: str | None) -> VisemePack:
    if not choices:
        raise FileNotFoundError('No viseme folders. Create <character>/visemes/default/A.png ... X.png '
                                'or character/visemes/default/A.png ... X.png')
    if request:
        if request in choices:
            return choices[request]
        found = [v for v in choices.values()
                 if v.directory.name.lower() == request.lower() or v.label == request]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return next((v for v in found if '(character)' in v.label), found[0])
        raise ValueError(f'Unknown viseme folder: {request!r}. Use --list-packs.')
    return next((v for v in choices.values() if v.directory.name.lower() == 'default'),
                next(iter(choices.values())))


def viseme_file(pack: VisemePack, shape: str) -> Path | None:
    requested = pack.directory / f'{shape.upper()}.png'
    if requested.is_file():
        return requested
    fallback = pack.directory / 'X.png'
    return fallback if fallback.is_file() else None


def validate_visemes(pack: VisemePack) -> None:
    if not (pack.directory / 'X.png').is_file():
        raise FileNotFoundError(f'Missing required X.png (silence) in {pack.directory}')
    missing = [s for s in 'ABCDEFGH' if not (pack.directory / f'{s}.png').is_file()]
    if missing:
        print(f'Warning: viseme pack {pack.directory.name} is missing {", ".join(missing)}; '
              'X.png will be used for these shapes.')


def default_setting() -> dict:
    return {'x': 0, 'y': 0, 'scale': 1.0}


def default_profile() -> dict:
    return {'mode': 'mouth_only', 'crop': list(DEFAULT_CROP),
            'target': list(DEFAULT_TARGET), 'expressions': {},
            'last_expression': 'neutral'}


def profile_key(char_key: str, viseme_key: str) -> str:
    return f'{char_key}::{viseme_key}'


def load_settings() -> dict:
    output = {'version': 2, 'selected_character': 'character', 'selected_viseme': '',
              'last_visemes': {}, 'profiles': {}}
    if not SETTINGS_PATH.is_file():
        return output
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
        if isinstance(raw.get('profiles'), dict):
            output.update({key: raw.get(key, output[key]) for key in output})
            if not isinstance(output['last_visemes'], dict):
                output['last_visemes'] = {}
        else:
            # Convert legacy mouth_settings.json without losing mouth tuning.
            legacy = default_profile()
            if raw.get('mode') in ('mouth_only', 'full_overlay'):
                legacy['mode'] = raw['mode']
            if isinstance(raw.get('expressions'), dict):
                legacy['expressions'] = raw['expressions']
            output['profiles'][profile_key('character', 'character/visemes/default')] = legacy
    except (OSError, ValueError, TypeError) as exc:
        print('Warning: could not load mouth settings:', exc)
    return output


def save_settings(config: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(SETTINGS_PATH)


def get_profile(config: dict, character: CharacterPack, viseme: VisemePack) -> dict:
    key = profile_key(character.key, viseme.key)
    profile = config.setdefault('profiles', {}).setdefault(key, default_profile())
    # Complete partial or migrated profiles.
    defaults = default_profile()
    for k, v in defaults.items():
        if k not in profile:
            profile[k] = v
    return profile


@lru_cache(maxsize=160)
def open_sprite(path: str) -> Image.Image:
    with Image.open(path) as raw:
        image = raw.convert('RGBA')
    if image.size != (WIDTH, HEIGHT):
        image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    return image


@lru_cache(maxsize=100)
def mouth_patch(path: str, crop: tuple[int, int, int, int]) -> Image.Image:
    left, top, right, bottom = crop
    if right <= left or bottom <= top:
        raise ValueError(f'Invalid mouth crop: {crop}; right/bottom must exceed left/top.')
    if not (0 <= left < right <= WIDTH and 0 <= top < bottom <= HEIGHT):
        raise ValueError(f'Mouth crop {crop} must be inside {WIDTH}x{HEIGHT}.')
    patch = open_sprite(path).crop(crop).copy()
    w, h = patch.size
    # Feather a small ellipse around the mouth; prevents rectangular seams.
    mask = Image.new('L', (w, h), 0)
    draw = ImageDraw.Draw(mask)
    pad_x, pad_y = min(5, w // 4), min(4, h // 4)
    draw.ellipse((pad_x, pad_y, max(pad_x+1, w-1-pad_x),
                  max(pad_y+1, h-1-pad_y)), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=min(5, w/8, h/8)))
    patch.putalpha(ImageChops.multiply(patch.getchannel('A'), mask))
    return patch


@lru_cache(maxsize=250)
def render_frame(expression_file: str, mouth_file: str | None, body_file: str | None,
                 mode: str, x: int, y: int, scale_percent: int,
                 crop: tuple[int, int, int, int], target: tuple[int, int]) -> Image.Image:
    """Render an opaque RGB frame; transparent visemes cannot black out the scene."""
    frame = Image.new('RGBA', (WIDTH, HEIGHT), BG_COLOR)
    if body_file:
        frame.alpha_composite(open_sprite(body_file))
    frame.alpha_composite(open_sprite(expression_file))

    if mouth_file:
        scale = max(.2, min(2.0, scale_percent / 100.0))
        if mode == 'full_overlay':
            source = open_sprite(mouth_file)
        else:
            source = mouth_patch(mouth_file, crop)
        new_w, new_h = max(1, round(source.width * scale)), max(1, round(source.height * scale))
        overlay = source.resize((new_w, new_h), Image.Resampling.LANCZOS)
        if mode == 'full_overlay':
            px = round(target[0] + x - SOURCE_MOUTH_CENTER[0] * scale)
            py = round(target[1] + y - SOURCE_MOUTH_CENTER[1] * scale)
        else:
            px = round(target[0] + x - new_w / 2)
            py = round(target[1] + y - new_h / 2)
        canvas = Image.new('RGBA', (WIDTH, HEIGHT), (0, 0, 0, 0))
        canvas.paste(overlay, (px, py))
        frame.alpha_composite(canvas)
    return frame.convert('RGB')


def frame_for(profile: dict, expression_file: Path, mouth_file: Path | None,
              feeling: str, character: CharacterPack) -> Image.Image:
    settings = profile.get('expressions', {}).get(feeling, default_setting())
    return render_frame(
        str(expression_file), str(mouth_file) if mouth_file else None,
        str(character.body_path) if character.body_path.is_file() else None,
        profile.get('mode', 'mouth_only'),
        int(settings.get('x', 0)), int(settings.get('y', 0)),
        round(100 * float(settings.get('scale', 1.0))),
        tuple(int(v) for v in profile.get('crop', DEFAULT_CROP)),
        tuple(int(v) for v in profile.get('target', DEFAULT_TARGET)),
    )


def stamp() -> str:
    return datetime.now().strftime('%Y%m%d_%H%M%S_%f')


def safe_filename(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_-]+', '_', name.strip())
    return name.strip('_') or 'character'


def unique_output(stem: str, suffix: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = OUTPUT_DIR / f'{stem}{suffix}'
    index = 2
    while result.exists():
        result = OUTPUT_DIR / f'{stem}_{index}{suffix}'
        index += 1
    return result


# ---------------------------------------------------------------------------
# Interactive: Character / viseme pack selection, mouth tuning and preview
# ---------------------------------------------------------------------------
class MouthEditor(tk.Toplevel):
    def __init__(self, master, config: dict, char_choices: dict[str, CharacterPack],
                 requested_character: str | None = None, requested_viseme: str | None = None):
        super().__init__(master)
        self.title('Character + viseme packs / mouth editor (200x200)')
        self.resizable(False, False)
        self.config_data = config
        self.char_choices = char_choices
        self.selected_character: CharacterPack | None = None
        self.selected_viseme: VisemePack | None = None
        self.viseme_choices: dict[str, VisemePack] = {}
        self.expressions: dict[str, Path] = {}
        self.current_expression = ''
        self._loading = False
        self._photo = None

        self.character_var = tk.StringVar()
        self.viseme_pack_var = tk.StringVar()
        self.expression_var = tk.StringVar()
        self.shape_var = tk.StringVar()
        self.mode_var = tk.StringVar(value='mouth_only')
        self.x = tk.IntVar(value=0)
        self.y = tk.IntVar(value=0)
        self.size = tk.IntVar(value=100)
        self.crop_vars = [tk.IntVar(value=v) for v in DEFAULT_CROP]
        self.target_vars = [tk.IntVar(value=v) for v in DEFAULT_TARGET]
        self.status_var = tk.StringVar(value='')

        left = ttk.Frame(self, padding=12)
        left.grid(row=0, column=0, sticky='n')
        ttk.Label(left, text='Character pack').pack(anchor='w')
        self.character_combo = ttk.Combobox(left, state='readonly', width=37,
                                            textvariable=self.character_var)
        self.character_combo.pack(fill='x', pady=(2, 5))
        self.character_combo.bind('<<ComboboxSelected>>', self.change_character)
        ttk.Label(left, text='Viseme pack (folder)').pack(anchor='w')
        self.viseme_combo = ttk.Combobox(left, state='readonly', width=37,
                                         textvariable=self.viseme_pack_var)
        self.viseme_combo.pack(fill='x', pady=(2, 5))
        self.viseme_combo.bind('<<ComboboxSelected>>', self.change_viseme_pack)
        ttk.Button(left, text='Refresh folders', command=self.refresh_packs).pack(fill='x', pady=(0, 9))

        ttk.Separator(left).pack(fill='x', pady=5)
        ttk.Label(left, text='Expression').pack(anchor='w')
        self.expression_combo = ttk.Combobox(left, state='readonly', width=37,
                                             textvariable=self.expression_var)
        self.expression_combo.pack(fill='x', pady=(0, 6))
        self.expression_combo.bind('<<ComboboxSelected>>', self.change_expression)
        ttk.Label(left, text='Viseme for preview').pack(anchor='w')
        self.shape_combo = ttk.Combobox(left, state='readonly', width=37,
                                        textvariable=self.shape_var)
        self.shape_combo.pack(fill='x', pady=(0, 6))
        self.shape_combo.bind('<<ComboboxSelected>>', lambda event: self.update_preview())
        ttk.Label(left, text='Overlay type').pack(anchor='w')
        self.mode_combo = ttk.Combobox(left, state='readonly', width=37,
                                       textvariable=self.mode_var,
                                       values=['mouth_only', 'full_overlay'])
        self.mode_combo.pack(fill='x', pady=(0, 6))
        self.mode_combo.bind('<<ComboboxSelected>>', lambda event: self.update_preview())

        self.make_slider(left, 'Mouth X (horizontal offset)', self.x, -80, 80)
        self.make_slider(left, 'Mouth Y (vertical offset)', self.y, -80, 80)
        self.make_slider(left, 'Mouth size (%)', self.size, 40, 180)

        advanced = ttk.LabelFrame(left, text='Advanced (per pack): crop / mouth center', padding=6)
        advanced.pack(fill='x', pady=(9, 0))
        crop_row = ttk.Frame(advanced)
        crop_row.pack(fill='x')
        for label, variable in zip(('L', 'T', 'R', 'B'), self.crop_vars):
            ttk.Label(crop_row, text=label).pack(side='left', padx=(3, 0))
            entry = ttk.Spinbox(crop_row, from_=0, to=200, width=4,
                                 textvariable=variable, command=self.update_preview)
            entry.pack(side='left', padx=(2, 4))
        target_row = ttk.Frame(advanced)
        target_row.pack(fill='x', pady=(3, 0))
        for label, variable in zip(('Target X', 'Target Y'), self.target_vars):
            ttk.Label(target_row, text=label).pack(side='left', padx=(3, 0))
            entry = ttk.Spinbox(target_row, from_=0, to=200, width=4,
                                 textvariable=variable, command=self.update_preview)
            entry.pack(side='left', padx=(3, 9))
        for var in [*self.crop_vars, *self.target_vars]:
            var.trace_add('write', lambda *_: self.update_preview())

        ttk.Button(left, text='Save preview + settings', command=self.save_current).pack(fill='x', pady=(11, 5))
        ttk.Button(left, text='Save and continue to audio', command=self.finish).pack(fill='x')
        ttk.Label(left, text='mouth_only changes only the mouth area.\n'
                  'Create new folders, then click Refresh folders.', foreground='#555').pack(
                      anchor='w', pady=(9, 0))

        right = ttk.Frame(self, padding=12)
        right.grid(row=0, column=1, sticky='n')
        ttk.Label(right, text='Preview at 400% (200x200 source)').pack()
        self.preview_label = ttk.Label(right)
        self.preview_label.pack(pady=12)
        ttk.Label(right, textvariable=self.status_var, width=48, wraplength=360,
                  justify='left').pack(anchor='w')

        try:
            initial_character = choose_character(
                char_choices, requested_character or config.get('selected_character'))
        except ValueError:
            if requested_character:
                raise  # Explicit user choice must not silently change.
            initial_character = choose_character(char_choices, None)
        self.character_combo['values'] = [item.label for item in char_choices.values()]
        self.character_var.set(initial_character.label)
        self.activate_character(initial_character, requested_viseme)
        self.protocol('WM_DELETE_WINDOW', self.finish)
        self.grab_set()

    def make_slider(self, parent, name: str, value, lowest: int, highest: int):
        ttk.Label(parent, text=name).pack(anchor='w', pady=(5, 0))
        tk.Scale(parent, from_=lowest, to=highest, orient='horizontal',
                 variable=value, length=280, command=lambda _: self.update_preview()).pack()

    def find_character_from_label(self) -> CharacterPack:
        return next(c for c in self.char_choices.values() if c.label == self.character_var.get())

    def find_viseme_from_label(self) -> VisemePack:
        return next(v for v in self.viseme_choices.values() if v.label == self.viseme_pack_var.get())

    def activate_character(self, character: CharacterPack, requested_viseme: str | None = None):
        self._loading = True
        self.selected_character = character
        self.expressions = find_expressions(character)
        self.expression_combo['values'] = list(self.expressions)
        self.viseme_choices = discover_visemes(character)
        if not self.viseme_choices:
            self._loading = False
            raise FileNotFoundError('No viseme packs for ' + character.label +
                                    '. Add an A.png ... X.png folder under '
                                    + str(character.root / 'visemes'))
        saved = self.config_data.get('last_visemes', {}).get(character.key)
        preferred = requested_viseme or saved
        try:
            viseme = choose_viseme(self.viseme_choices, preferred)
        except ValueError:
            viseme = choose_viseme(self.viseme_choices, None)
        self.viseme_combo['values'] = [item.label for item in self.viseme_choices.values()]
        self.viseme_pack_var.set(viseme.label)
        self.selected_viseme = viseme
        self._loading = False
        self.activate_profile()

    def activate_profile(self):
        self._loading = True
        assert self.selected_character is not None and self.selected_viseme is not None
        profile = get_profile(self.config_data, self.selected_character, self.selected_viseme)
        self.config_data['selected_character'] = self.selected_character.key
        self.config_data['selected_viseme'] = self.selected_viseme.key
        self.config_data['last_visemes'][self.selected_character.key] = self.selected_viseme.key
        self.mode_var.set(profile.get('mode', 'mouth_only'))
        for variable, value in zip(self.crop_vars, profile.get('crop', DEFAULT_CROP)):
            variable.set(int(value))
        for variable, value in zip(self.target_vars, profile.get('target', DEFAULT_TARGET)):
            variable.set(int(value))
        desired = profile.get('last_expression', 'neutral')
        if desired not in self.expressions:
            desired = 'neutral' if 'neutral' in self.expressions else next(iter(self.expressions))
        self.current_expression = desired
        self.expression_var.set(desired)
        self.shape_combo['values'] = [s for s in SHAPES if (self.selected_viseme.directory / f'{s}.png').is_file()]
        shapes = list(self.shape_combo['values'])
        self.shape_var.set('A' if 'A' in shapes else ('X' if 'X' in shapes else (shapes[0] if shapes else '')))
        self.read_expression()
        self._loading = False
        self.update_preview()

    def profile(self) -> dict:
        assert self.selected_character is not None and self.selected_viseme is not None
        return get_profile(self.config_data, self.selected_character, self.selected_viseme)

    def read_expression(self):
        settings = self.profile()['expressions'].get(self.current_expression, default_setting())
        self.x.set(int(settings.get('x', 0)))
        self.y.set(int(settings.get('y', 0)))
        self.size.set(round(float(settings.get('scale', 1.0)) * 100))

    def get_advanced_values(self) -> tuple[list[int], list[int]]:
        crop = [v.get() for v in self.crop_vars]
        target = [v.get() for v in self.target_vars]
        if not (0 <= crop[0] < crop[2] <= WIDTH and 0 <= crop[1] < crop[3] <= HEIGHT):
            raise ValueError('Crop must be inside 200x200 with L<R and T<B.')
        if not all(0 <= value <= WIDTH for value in target):
            raise ValueError('Target center must be between 0 and 200.')
        return crop, target

    def remember_current(self):
        if not self.selected_character or not self.selected_viseme or not self.current_expression:
            return
        crop, target = self.get_advanced_values()
        profile = self.profile()
        profile['mode'] = self.mode_var.get()
        profile['crop'] = crop
        profile['target'] = target
        profile['last_expression'] = self.current_expression
        profile['expressions'][self.current_expression] = {
            'x': self.x.get(), 'y': self.y.get(), 'scale': self.size.get() / 100.0}

    def change_character(self, event=None):
        if self._loading:
            return
        try:
            self.remember_current()
            self.activate_character(self.find_character_from_label())
        except Exception as exc:
            messagebox.showerror('Character pack', str(exc), parent=self)

    def change_viseme_pack(self, event=None):
        if self._loading:
            return
        try:
            self.remember_current()
            self.selected_viseme = self.find_viseme_from_label()
            self.activate_profile()
        except Exception as exc:
            messagebox.showerror('Viseme pack', str(exc), parent=self)

    def change_expression(self, event=None):
        if self._loading:
            return
        try:
            self.remember_current()
            self.current_expression = self.expression_var.get()
            self._loading = True
            self.read_expression()
            self._loading = False
            self.update_preview()
        except Exception as exc:
            self._loading = False
            messagebox.showerror('Expression', str(exc), parent=self)

    def refresh_packs(self):
        try:
            self.remember_current()
            selected_char_key = self.selected_character.key if self.selected_character else None
            selected_vis_key = self.selected_viseme.key if self.selected_viseme else None
            self.char_choices = discover_characters()
            self.character_combo['values'] = [item.label for item in self.char_choices.values()]
            chosen = self.char_choices.get(selected_char_key) or choose_character(self.char_choices, None)
            self.character_var.set(chosen.label)
            open_sprite.cache_clear()
            mouth_patch.cache_clear()
            render_frame.cache_clear()
            self.activate_character(chosen, selected_vis_key)
        except Exception as exc:
            messagebox.showerror('Refresh packs', str(exc), parent=self)

    def update_preview(self):
        if self._loading or not self.selected_character or not self.selected_viseme:
            return
        try:
            crop, target = self.get_advanced_values()
            chosen_viseme = viseme_file(self.selected_viseme, self.shape_var.get())
            expression = self.expressions[self.current_expression]
            picture = render_frame(
                str(expression), str(chosen_viseme) if chosen_viseme else None,
                str(self.selected_character.body_path) if self.selected_character.body_path.is_file() else None,
                self.mode_var.get(), self.x.get(), self.y.get(), self.size.get(),
                tuple(crop), tuple(target))
            self._photo = ImageTk.PhotoImage(picture.resize((400, 400), Image.Resampling.NEAREST))
            self.preview_label.configure(image=self._photo)
            note = f'Character: {self.selected_character.root.name} | Visemes: {self.selected_viseme.directory.name}'
            if not (self.selected_viseme.directory / 'X.png').is_file():
                note += '\nWARNING: X.png is missing! Add it before rendering.'
            self.status_var.set(note)
        except (ValueError, tk.TclError) as exc:
            self.status_var.set(str(exc))

    def save_current(self) -> Path | None:
        try:
            self.remember_current()
            save_settings(self.config_data)
            picture = frame_for(self.profile(), self.expressions[self.current_expression],
                                viseme_file(self.selected_viseme, self.shape_var.get()),
                                self.current_expression, self.selected_character)
            name = f'mouth_preview_{safe_filename(self.selected_character.root.name)}_{stamp()}'
            path = unique_output(name, '.png')
            picture.save(path)
            self.status_var.set(f'Settings saved; preview: {path.name}')
            print('Saved:', SETTINGS_PATH, '\nPreview:', path)
            return path
        except Exception as exc:
            messagebox.showerror('Save error', str(exc), parent=self)
            return None

    def finish(self):
        if self.save_current() is None:
            return
        self.destroy()


# ---------------------------------------------------------------------------
# Audio splitting + selection
# ---------------------------------------------------------------------------
def split_audio_without_losing_silence(audio_path: Path, temp_dir: Path) -> list[tuple[Path, int]]:
    audio = AudioSegment.from_file(str(audio_path))
    if len(audio) < 1:
        raise ValueError('Selected audio is empty.')
    regions = detect_nonsilent(audio, min_silence_len=MIN_SILENCE_LEN,
                               silence_thresh=SILENCE_THRESH_DB)
    cuts = [0]
    for a, b in zip(regions, regions[1:]):
        midpoint = (a[1] + b[0]) // 2
        if cuts[-1] < midpoint < len(audio):
            cuts.append(midpoint)
    cuts.append(len(audio))
    chunks = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        if chunks and (b-a < MIN_CHUNK_MS):
            chunks[-1] = (chunks[-1][0], b)
        else:
            chunks.append((a, b))
    if len(chunks) >= 2 and (chunks[0][1] - chunks[0][0] < MIN_CHUNK_MS):
        chunks[1] = (chunks[0][0], chunks[1][1])
        chunks.pop(0)
    temp_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for index, (start, end) in enumerate(chunks):
        target = temp_dir / f'chunk_{index:03d}.wav'
        audio[start:end].export(str(target), format='wav')
        results.append((target, end-start))
    print(f'Split audio into {len(results)} chunk(s), duration {len(audio)/1000:.2f}s')
    return results


class EmotionPicker(tk.Toplevel):
    def __init__(self, master, chunks: list[tuple[Path, int]], expressions: dict[str, Path]):
        super().__init__(master)
        self.title('Choose emotion for each audio segment')
        self.geometry('465x300')
        self.chunks = chunks
        self.emotions = list(expressions)
        default = 'neutral' if 'neutral' in expressions else self.emotions[0]
        self.feelings = [default] * len(chunks)
        self.intensities = [0.5] * len(chunks)
        self.index = 0
        self.emotion = tk.StringVar(value=default)
        self.intensity = tk.IntVar(value=5)
        wrap = ttk.Frame(self, padding=18)
        wrap.pack(fill='both', expand=True)
        self.title_label = ttk.Label(wrap, text='')
        self.title_label.pack(pady=(5, 12))
        ttk.Button(wrap, text='▶ Play current audio chunk', command=self.play).pack(pady=(0, 11))
        ttk.Combobox(wrap, textvariable=self.emotion, state='readonly',
                     values=self.emotions).pack(fill='x')
        ttk.Label(wrap, text='Emotion intensity').pack(pady=(11, 0))
        tk.Scale(wrap, from_=1, to=10, variable=self.intensity,
                 orient='horizontal').pack(fill='x')
        bar = ttk.Frame(wrap)
        bar.pack(pady=11)
        ttk.Button(bar, text='Previous', command=self.previous).pack(side='left', padx=5)
        ttk.Button(bar, text='Save / Next', command=self.next).pack(side='left', padx=5)
        self.update_label()
        self.grab_set()
        self.protocol('WM_DELETE_WINDOW', self.finish)

    def update_label(self):
        self.title_label.config(text=f'Segment {self.index+1}/{len(self.chunks)}  '
                                     f'({self.chunks[self.index][1] / 1000:.2f} s)')
        self.emotion.set(self.feelings[self.index])
        self.intensity.set(round(10 * self.intensities[self.index]))

    def save_selection(self):
        self.feelings[self.index] = self.emotion.get()
        self.intensities[self.index] = self.intensity.get() / 10.0

    def play(self):
        if pygame is None:
            messagebox.showinfo('Audio preview', 'Install pygame to preview audio: pip install pygame')
            return
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.load(str(self.chunks[self.index][0]))
            pygame.mixer.music.play()
        except Exception as exc:
            messagebox.showerror('Playback error', str(exc))

    def previous(self):
        self.save_selection()
        if self.index > 0:
            self.index -= 1
            self.update_label()

    def next(self):
        self.save_selection()
        if self.index == len(self.chunks)-1:
            self.finish()
        else:
            self.index += 1
            self.update_label()

    def finish(self):
        self.save_selection()
        if pygame is not None and pygame.mixer.get_init():
            pygame.mixer.music.stop()
        self.destroy()


# ---------------------------------------------------------------------------
# Rhubarb + FFmpeg: no black frames, no truncated timeline
# ---------------------------------------------------------------------------
def locate_rhubarb() -> str:
    if Path(RHUBARB_BIN).is_file():
        return RHUBARB_BIN
    found = shutil.which('rhubarb') or shutil.which('rhubarb.exe')
    if found:
        return found
    raise FileNotFoundError('Rhubarb not found. Set RHUBARB_BIN near the top of the script.')


def get_cues(chunk: Path, executable: str | None) -> tuple[list[float], list[tuple[float, float, str]]]:
    if not ENABLE_LIPSYNC:
        return [], []
    assert executable is not None
    target = chunk.with_suffix('.json')
    command = [executable, '-r', RHUBARB_RECOGNIZER, '--extendedShapes',
               RHUBARB_EXTENDED, '-f', 'json', '-o', str(target), str(chunk)]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise RuntimeError(f'Rhubarb failed on {chunk.name}: {proc.stderr}')
    cues = json.loads(target.read_text(encoding='utf-8')).get('mouthCues', [])
    ordered = sorted((float(c['start']), float(c['end']), str(c['value'])) for c in cues)
    return [row[0] for row in ordered], ordered


def cue_at(t: float, starts: list[float], cues: list[tuple[float, float, str]]) -> str:
    if not starts:
        return 'X'
    i = bisect.bisect_right(starts, t)-1
    return cues[i][2] if i >= 0 and t < cues[i][1] else 'X'


def locate_ffmpeg() -> str:
    exe = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise FileNotFoundError('FFmpeg not found: install FFmpeg and add it to PATH.') from exc


def render_video(audio_path: Path, chunks: list[tuple[Path, int]],
                 feelings: list[str], intensities: list[float],
                 expressions: dict[str, Path], character: CharacterPack,
                 viseme: VisemePack, profile: dict) -> Path:
    validate_visemes(viseme)
    ffmpeg = locate_ffmpeg()
    rhubarb = locate_rhubarb() if ENABLE_LIPSYNC else None
    specs, ends = [], []
    cursor = 0
    for (chunk, length_ms), feeling, intensity in zip(chunks, feelings, intensities):
        starts, cues = get_cues(chunk, rhubarb)
        cursor += length_ms
        ends.append(cursor)
        specs.append({'feeling': feeling, 'intensity': intensity,
                      'start_ms': cursor-length_ms, 'starts': starts, 'cues': cues})
    if not specs:
        raise ValueError('No audio chunks to render.')

    name = safe_filename(character.root.name)
    job = f'{name}_{stamp()}'
    video_path = unique_output(f'final_{job}', '.mp4')
    # A common stem keeps all diagnostic outputs grouped by render.
    debug_stem = video_path.stem
    first_frame_path = unique_output(f'{debug_stem}_first_frame', '.png')
    log_path = unique_output(f'{debug_stem}_ffmpeg', '.log')

    count = max(1, math.ceil(cursor*FPS/1000))
    blink_marks = []
    if ENABLE_BLINK and 'eyes_closed' in expressions:
        rng = random.Random(4321)
        t = rng.uniform(3,5)
        while t < cursor/1000:
            blink_marks.append(t)
            t += rng.uniform(3,5)

    command = [ffmpeg, '-n', '-hide_banner', '-loglevel', 'error',
               '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{WIDTH}x{HEIGHT}',
               '-framerate', str(FPS), '-i', '-', '-i', str(audio_path),
               '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264',
               '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(video_path)]
    print(f'Rendering {count} frames -> {video_path}')
    with log_path.open('wb') as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log)
        assert process.stdin is not None
        try:
            for index in range(count):
                moment = min(cursor-0.01, (index+0.5)*1000/FPS)
                segment = min(len(specs)-1, bisect.bisect_right(ends, moment))
                spec = specs[segment]
                feeling = spec['feeling']
                expression = expressions.get(feeling) or expressions.get('neutral') or next(iter(expressions.values()))
                local_t = (moment-spec['start_ms'])/1000
                shape = cue_at(local_t, spec['starts'], spec['cues'])
                mouth = viseme_file(viseme, shape)
                if blink_marks and any(0 <= moment/1000-b < .08 for b in blink_marks):
                    expression = expressions['eyes_closed']
                current = frame_for(profile, expression, mouth, feeling, character)
                if ENABLE_EXPRESSION_BLEND and segment > 0:
                    previous = specs[segment-1]
                    duration = .07 + .15*abs(previous['intensity']-spec['intensity'])
                    if local_t < duration:
                        prev_emotion = previous['feeling']
                        prev_expression = expressions.get(prev_emotion) or expression
                        before = frame_for(profile, prev_expression,
                                           viseme_file(viseme,'X'), prev_emotion, character)
                        current = Image.blend(before, current, min(1, max(0, local_t/duration)))
                if index == 0:
                    current.save(first_frame_path)
                process.stdin.write(current.tobytes())
                if (index+1) % (FPS*10) == 0:
                    print(f'  rendered {(index+1)/FPS:.0f} seconds')
        except Exception:
            process.stdin.close()
            process.wait()
            raise
        else:
            process.stdin.close()
            code = process.wait()
            if code:
                raise RuntimeError(f'FFmpeg exit {code}. See {log_path}:\n'
                                   + log_path.read_text(encoding='utf-8', errors='replace')[-2000:])
    print('Completed video:', video_path)
    print('First frame:', first_frame_path)
    print('Log:', log_path)
    return video_path


def list_all_packs(char_choices: dict[str, CharacterPack]):
    print('Character packs:')
    for ch in char_choices.values():
        print(f'  {ch.key}  ({len(find_expressions(ch))} expressions)')
        available = discover_visemes(ch)
        for v in available.values():
            present = ''.join(shape for shape in SHAPES if (v.directory/f'{shape}.png').is_file())
            print(f'      {v.key}  shapes: {present}')


def main():
    parser = argparse.ArgumentParser(description='200x200 lip-sync editor with character + viseme packs')
    parser.add_argument('--list-packs', action='store_true', help='List available character and viseme folders')
    parser.add_argument('--preview-only', action='store_true', help='Create only a mouth preview, no GUI or Rhubarb')
    parser.add_argument('--character-pack', default=None, help='Character name or path, e.g. politic')
    parser.add_argument('--viseme-pack', default=None, help='Viseme folder name or path, e.g. default')
    parser.add_argument('--emotion', default='neutral', help='Preview expression')
    parser.add_argument('--viseme', default='A', help='Preview mouth shape A-H or X')
    args = parser.parse_args()

    char_choices = discover_characters()
    if args.list_packs:
        list_all_packs(char_choices)
        return
    config = load_settings()
    if args.preview_only:
        try:
            ch = choose_character(char_choices, args.character_pack or config.get('selected_character'))
        except ValueError:
            if args.character_pack:
                raise
            ch = choose_character(char_choices, None)
        vis = choose_viseme(discover_visemes(ch), args.viseme_pack or
                            config.get('last_visemes',{}).get(ch.key))
        expressions = find_expressions(ch)
        emotion = args.emotion.lower()
        expression = expressions.get(emotion) or expressions.get('neutral') or next(iter(expressions.values()))
        image = frame_for(get_profile(config,ch,vis), expression,
                          viseme_file(vis,args.viseme), emotion, ch)
        path = unique_output(f'mouth_preview_{safe_filename(ch.root.name)}_{stamp()}', '.png')
        image.save(path)
        print('Preview:', path)
        return

    root = tk.Tk()
    root.withdraw()
    try:
        editor = MouthEditor(root, config, char_choices, args.character_pack, args.viseme_pack)
        root.wait_window(editor)
        ch = editor.selected_character
        vis = editor.selected_viseme
        if ch is None or vis is None:
            return
        validate_visemes(vis)
        expressions = find_expressions(ch)
        audio_file = filedialog.askopenfilename(parent=root, title='Choose a voice recording',
                                                filetypes=[('Audio', '*.wav *.mp3 *.ogg *.m4a *.flac'),
                                                           ('All files', '*.*')])
        if not audio_file:
            print('No audio chosen: settings and preview saved, no MP4 rendered.')
            return
        with tempfile.TemporaryDirectory(prefix='lipsync_') as temp:
            chunks = split_audio_without_losing_silence(Path(audio_file), Path(temp))
            picker = EmotionPicker(root, chunks, expressions)
            root.wait_window(picker)
            profile = get_profile(config, ch, vis)
            video = render_video(Path(audio_file), chunks, picker.feelings, picker.intensities,
                                 expressions, ch, vis, profile)
        messagebox.showinfo('Render finished', f'Video saved:\n{video}', parent=root)
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr)
        messagebox.showerror('Error', str(exc), parent=root)
        raise
    finally:
        root.destroy()


if __name__ == '__main__':
    main()
