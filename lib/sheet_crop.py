"""Deterministic crops from an approved character sheet.

The canonical character sheet layout (see server._build_pos_sheet_prompt):
  Row 1: FRONT, FRONT 3Q, RIGHT SIDE PROFILE   (three equal full-body tiles)
  Row 2: BACK 3Q, BACK, LEFT SIDE PROFILE      (three equal full-body tiles)
  Row 3: LARGE FACE CLOSEUP                     (spans full width)

Crops are pixel-deterministic — same sheet always yields the same tile.
Used to build per-shot anchors without re-generating (and re-drifting) identity.
"""

from __future__ import annotations
import os
from typing import Optional
from PIL import Image

# Relative tile geometry. The generator renders a 3-row sheet where the top two
# rows are equal-height body-angle rows and the bottom row is the face closeup.
# Observed empirically that Gemini tends to give the face row ~40% of the height
# when asked for a "large" closeup. We tolerate a range and pick a safe middle.
BODY_ROW_HEIGHT_FRACTION = 0.30  # each of the two body rows
FACE_ROW_HEIGHT_FRACTION = 0.40  # bottom face row

# Body row column splits
FRONT_COL = (0.00, 0.333)
FRONT_3Q_COL = (0.333, 0.666)
RIGHT_PROFILE_COL = (0.666, 1.00)
BACK_3Q_COL = (0.00, 0.333)
BACK_COL = (0.333, 0.666)
LEFT_PROFILE_COL = (0.666, 1.00)


def _safe_crop(img: Image.Image, left: float, top: float, right: float, bottom: float) -> Image.Image:
    w, h = img.size
    box = (int(left * w), int(top * h), int(right * w), int(bottom * h))
    return img.crop(box)


def crop_face(sheet_path: str, out_path: Optional[str] = None) -> str:
    """Crop the bottom face-closeup row from a character sheet.

    Returns the path to the cropped image.
    """
    img = Image.open(sheet_path).convert("RGB")
    top = 1.0 - FACE_ROW_HEIGHT_FRACTION
    face = _safe_crop(img, 0.0, top, 1.0, 1.0)
    if out_path is None:
        base, ext = os.path.splitext(sheet_path)
        out_path = f"{base}__face{ext}"
    face.save(out_path, "PNG")
    return out_path


def crop_front(sheet_path: str, out_path: Optional[str] = None) -> str:
    """Crop the FRONT full-body view (top-left tile)."""
    img = Image.open(sheet_path).convert("RGB")
    face = _safe_crop(img, FRONT_COL[0], 0.0, FRONT_COL[1], BODY_ROW_HEIGHT_FRACTION)
    if out_path is None:
        base, ext = os.path.splitext(sheet_path)
        out_path = f"{base}__front{ext}"
    face.save(out_path, "PNG")
    return out_path


def crop_side(sheet_path: str, out_path: Optional[str] = None) -> str:
    """Crop the RIGHT SIDE PROFILE view (top-right tile)."""
    img = Image.open(sheet_path).convert("RGB")
    face = _safe_crop(img, RIGHT_PROFILE_COL[0], 0.0, RIGHT_PROFILE_COL[1], BODY_ROW_HEIGHT_FRACTION)
    if out_path is None:
        base, ext = os.path.splitext(sheet_path)
        out_path = f"{base}__side{ext}"
    face.save(out_path, "PNG")
    return out_path


def crop_back(sheet_path: str, out_path: Optional[str] = None) -> str:
    """Crop the BACK full-body view (middle-middle tile)."""
    img = Image.open(sheet_path).convert("RGB")
    top = BODY_ROW_HEIGHT_FRACTION
    bottom = 2 * BODY_ROW_HEIGHT_FRACTION
    face = _safe_crop(img, BACK_COL[0], top, BACK_COL[1], bottom)
    if out_path is None:
        base, ext = os.path.splitext(sheet_path)
        out_path = f"{base}__back{ext}"
    face.save(out_path, "PNG")
    return out_path


def crop_tile(sheet_path: str, tile: str, out_path: Optional[str] = None) -> str:
    """Dispatch: tile in {'face','front','side','back'}."""
    fn = {"face": crop_face, "front": crop_front, "side": crop_side, "back": crop_back}.get(tile)
    if fn is None:
        raise ValueError(f"Unknown tile '{tile}' — expected one of face, front, side, back")
    return fn(sheet_path, out_path)
