"""Project registry and active-project resolution.

LUMN supports multiple projects with isolated workspaces under output/projects/<slug>/.
A shared vault at output/vault/ holds reusable assets (characters, environments, costumes,
props, voices, master prompts) that can be imported into any project.

The 'active project' is persisted in output/active_project.json. Callers should read
the active slug via get_active_slug() and derive paths via get_project_root(slug).
"""
from __future__ import annotations
import os
import json
import shutil
import re
import time
from typing import Optional

# Absolute repo root — resolved from this file's location.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(_REPO_ROOT, "output")
PROJECTS_DIR = os.path.join(OUTPUT_DIR, "projects")
VAULT_DIR = os.path.join(OUTPUT_DIR, "vault")
ACTIVE_POINTER = os.path.join(OUTPUT_DIR, "active_project.json")
LEGACY_POS_DIR = os.path.join(OUTPUT_DIR, "prompt_os")  # pre-refactor path

DEFAULT_SLUG = "default"

# Subdirs scaffolded inside each project:
#   prompt_os/                  — all POS JSON + sheets
#   prompt_os/sheets/
#   prompt_os/char_photos/
#   prompt_os/env_photos/
#   prompt_os/costume_photos/
#   prompt_os/ref_photos/ (was prop_photos — 'references' motif library; legacy prop_photos still scaffolded)
#   prompt_os/voice_photos/
#   prompt_os/char_previews/
#   prompt_os/env_previews/
#   prompt_os/costume_previews/
#   prompt_os/ref_previews/ (was prop_previews)
#   shots/
#   renders/
#   cache/

_PROJECT_SUBDIRS = (
    "prompt_os",
    "prompt_os/sheets",
    "prompt_os/char_photos",
    "prompt_os/env_photos",
    "prompt_os/costume_photos",
    "prompt_os/ref_photos",
    "prompt_os/prop_photos",  # legacy — kept for existing projects
    "prompt_os/voice_photos",
    "prompt_os/char_previews",
    "prompt_os/env_previews",
    "prompt_os/costume_previews",
    "prompt_os/ref_previews",
    "prompt_os/prop_previews",  # legacy — kept for existing projects
    "shots",
    "renders",
    "cache",
)

_VAULT_SUBDIRS = (
    "characters",
    "environments",
    "costumes",
    "references",
    "voices",
    "master_prompts",
)

# Asset-type -> source-json filename + vault subdir. Used by snapshot_project_to_vault.
_VAULT_ASSET_TYPES = (
    ("characters", "characters.json"),
    ("environments", "environments.json"),
    ("costumes", "costumes.json"),
    ("references", "references.json"),
    ("voices", "voices.json"),
    ("master_prompts", "master_prompts.json"),
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _slugify(name: str) -> str:
    """Turn a display name into a filesystem-safe slug (lowercase, hyphens)."""
    s = re.sub(r"[^a-zA-Z0-9\-_ ]+", "", name).strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    return s or "project"


def _read_json(path: str, default=None):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _project_meta_path(slug: str) -> str:
    return os.path.join(PROJECTS_DIR, slug, "meta.json")


def _read_project_meta(slug: str) -> dict:
    """Read meta.json for a project. If missing, synthesize from slug."""
    meta = _read_json(_project_meta_path(slug), default=None)
    if meta and isinstance(meta, dict):
        # Ensure required fields present; fill defaults if not.
        meta.setdefault("slug", slug)
        meta.setdefault("name", slug.replace("-", " ").title())
        meta.setdefault("created_at", _now_iso())
        meta.setdefault("updated_at", meta.get("created_at", _now_iso()))
        return meta
    # Synthesize
    return {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def _write_project_meta(slug: str, meta: dict) -> None:
    meta = dict(meta)
    meta["slug"] = slug
    meta["updated_at"] = _now_iso()
    _write_json(_project_meta_path(slug), meta)


# ─────────────────────────── Active pointer ───────────────────────────

def get_active_slug() -> str:
    """Return the currently active project slug. Creates default pointer on first call."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    pointer = _read_json(ACTIVE_POINTER, default=None)
    if not pointer or not isinstance(pointer, dict) or not pointer.get("slug"):
        # First call — scaffold default project so callers never see a dangling pointer.
        ensure_project_scaffold(DEFAULT_SLUG)
        # Write meta.json if it doesn't exist yet
        if not os.path.isfile(_project_meta_path(DEFAULT_SLUG)):
            _write_project_meta(DEFAULT_SLUG, {
                "slug": DEFAULT_SLUG,
                "name": "Default",
                "created_at": _now_iso(),
            })
        _write_json(ACTIVE_POINTER, {"slug": DEFAULT_SLUG, "updated_at": _now_iso()})
        return DEFAULT_SLUG
    return pointer["slug"]


def set_active_slug(slug: str) -> None:
    """Update the active-project pointer. Caller must ensure slug exists."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    _write_json(ACTIVE_POINTER, {"slug": slug, "updated_at": _now_iso()})


# ─────────────────────────── Path helpers ───────────────────────────

def get_project_root(slug: Optional[str] = None) -> str:
    """Absolute path to output/projects/<slug>/. Defaults to active slug."""
    if slug is None:
        slug = get_active_slug()
    return os.path.join(PROJECTS_DIR, slug)


def ensure_project_scaffold(slug: str) -> str:
    """Create all required subdirs for a project. Idempotent. Returns project root."""
    root = os.path.join(PROJECTS_DIR, slug)
    os.makedirs(root, exist_ok=True)
    for sub in _PROJECT_SUBDIRS:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def ensure_vault_scaffold() -> str:
    """Create vault root and its subdirs. Idempotent. Returns vault root."""
    os.makedirs(VAULT_DIR, exist_ok=True)
    for sub in _VAULT_SUBDIRS:
        os.makedirs(os.path.join(VAULT_DIR, sub), exist_ok=True)
    return VAULT_DIR


# ─────────────────────────── Registry ───────────────────────────

def list_projects() -> list[dict]:
    """Return [{slug, name, created_at, updated_at, is_active}, ...] by scanning PROJECTS_DIR.

    Each project has a meta.json file at its root; if missing, synthesize from slug.
    Only directory entries are considered projects — stray .json files from legacy tooling
    are ignored.
    """
    if not os.path.isdir(PROJECTS_DIR):
        return []

    active_slug = None
    # Don't create a scaffold just to check — read the pointer directly.
    pointer = _read_json(ACTIVE_POINTER, default=None)
    if pointer and isinstance(pointer, dict):
        active_slug = pointer.get("slug")

    projects: list[dict] = []
    try:
        entries = sorted(os.listdir(PROJECTS_DIR))
    except OSError:
        return []

    for entry in entries:
        entry_path = os.path.join(PROJECTS_DIR, entry)
        if not os.path.isdir(entry_path):
            continue
        meta = _read_project_meta(entry)
        projects.append({
            "slug": meta["slug"],
            "name": meta["name"],
            "created_at": meta["created_at"],
            "updated_at": meta.get("updated_at", meta["created_at"]),
            "is_active": (meta["slug"] == active_slug),
        })
    return projects


def create_project(name: str, slug: Optional[str] = None) -> dict:
    """Create a new project. Returns its meta dict. Raises ValueError on duplicate slug.

    If slug is None, derived from name via _slugify. Writes meta.json with name/created_at.
    """
    if not name or not name.strip():
        raise ValueError("Project name is required")
    name = name.strip()
    if slug is None:
        slug = _slugify(name)
    else:
        slug = _slugify(slug)

    if not slug:
        raise ValueError("Unable to derive a valid slug")

    # Ensure PROJECTS_DIR exists before checking for duplicates
    os.makedirs(PROJECTS_DIR, exist_ok=True)

    project_root = os.path.join(PROJECTS_DIR, slug)
    if os.path.isdir(project_root) and os.path.isfile(_project_meta_path(slug)):
        raise ValueError(f"Project with slug '{slug}' already exists")

    ensure_project_scaffold(slug)
    meta = {
        "slug": slug,
        "name": name,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _write_project_meta(slug, meta)
    return meta


def rename_project(slug: str, new_name: str) -> dict:
    """Update display name in meta.json (slug does NOT change — that would break URLs).

    Returns updated meta.
    """
    if not new_name or not new_name.strip():
        raise ValueError("New name is required")
    project_root = os.path.join(PROJECTS_DIR, slug)
    if not os.path.isdir(project_root):
        raise ValueError(f"Project '{slug}' does not exist")
    meta = _read_project_meta(slug)
    meta["name"] = new_name.strip()
    _write_project_meta(slug, meta)
    return _read_project_meta(slug)


def delete_project(slug: str) -> None:
    """Permanently delete output/projects/<slug>/. Raises if slug is active or is DEFAULT_SLUG.

    Caller is responsible for confirming with user before invoking.
    """
    if slug == DEFAULT_SLUG:
        raise ValueError(f"Cannot delete the default project '{DEFAULT_SLUG}'")
    pointer = _read_json(ACTIVE_POINTER, default=None)
    if pointer and isinstance(pointer, dict) and pointer.get("slug") == slug:
        raise ValueError(f"Cannot delete the active project '{slug}' — switch first")
    project_root = os.path.join(PROJECTS_DIR, slug)
    if not os.path.isdir(project_root):
        raise ValueError(f"Project '{slug}' does not exist")
    shutil.rmtree(project_root)


# ─────────────────────────── Vault snapshot ───────────────────────────

def snapshot_project_to_vault(slug: str) -> dict:
    """Export a project's reusable assets into the shared vault.

    Copies characters.json entries, environments.json entries, etc., into vault subdirs
    with filename {project-slug}__{asset-slug}.json. Returns {count_per_type}.
    """
    project_root = os.path.join(PROJECTS_DIR, slug)
    if not os.path.isdir(project_root):
        raise ValueError(f"Project '{slug}' does not exist")

    ensure_vault_scaffold()
    pos_dir = os.path.join(project_root, "prompt_os")

    counts: dict = {}
    for vault_subdir, filename in _VAULT_ASSET_TYPES:
        src = os.path.join(pos_dir, filename)
        data = _read_json(src, default=None)
        if not data or not isinstance(data, list):
            counts[vault_subdir] = 0
            continue

        dest_dir = os.path.join(VAULT_DIR, vault_subdir)
        os.makedirs(dest_dir, exist_ok=True)

        written = 0
        for entry in data:
            if not isinstance(entry, dict):
                continue
            asset_id = entry.get("id") or _slugify(entry.get("name", "asset"))
            asset_slug = _slugify(str(asset_id))
            dest = os.path.join(dest_dir, f"{slug}__{asset_slug}.json")
            try:
                payload = {
                    "project_slug": slug,
                    "asset_type": vault_subdir,
                    "exported_at": _now_iso(),
                    "entry": entry,
                }
                with open(dest, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                written += 1
            except (IOError, OSError):
                continue
        counts[vault_subdir] = written

    return counts


# ─────────────────────────── Legacy migration ───────────────────────────

def _has_any_content(path: str) -> bool:
    """Return True if path exists and contains at least one file (recursive)."""
    if not os.path.isdir(path):
        return False
    for _root, _dirs, files in os.walk(path):
        if files:
            return True
    return False


def _projects_dir_has_any_project() -> bool:
    """True if PROJECTS_DIR has at least one subdirectory (legacy json files don't count)."""
    if not os.path.isdir(PROJECTS_DIR):
        return False
    try:
        for entry in os.listdir(PROJECTS_DIR):
            if os.path.isdir(os.path.join(PROJECTS_DIR, entry)):
                return True
    except OSError:
        return False
    return False


def _merge_move_tree(src_root: str, dest_root: str, moved_log: list) -> None:
    """Recursively move files from src_root into dest_root, preserving structure.

    - Creates dest subdirs as needed.
    - Does NOT overwrite existing files (skips if destination already exists).
    - Removes empty source directories after moving.
    """
    if not os.path.isdir(src_root):
        return
    for dirpath, _dirnames, filenames in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        target_dir = dest_root if rel == "." else os.path.join(dest_root, rel)
        os.makedirs(target_dir, exist_ok=True)
        for fname in filenames:
            src = os.path.join(dirpath, fname)
            dest = os.path.join(target_dir, fname)
            if os.path.exists(dest):
                # Don't overwrite
                continue
            try:
                shutil.move(src, dest)
                moved_log.append(dest)
            except (IOError, OSError, shutil.Error):
                continue
    # Best-effort: remove now-empty source tree (bottom-up)
    for dirpath, dirnames, filenames in os.walk(src_root, topdown=False):
        if not filenames and not dirnames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


def migrate_legacy_workspace() -> Optional[str]:
    """One-time migration: if output/prompt_os/ exists with content and no projects exist
    yet, move it to output/projects/default/prompt_os/ and set active=default.

    Returns the slug migrated to, or None if migration was not needed.
    Must be IDEMPOTENT — safe to call on every server boot.
    """
    # 1. If any project directory already exists, skip.
    if _projects_dir_has_any_project():
        return None

    # 2. If legacy dir is missing or empty, skip.
    if not _has_any_content(LEGACY_POS_DIR):
        return None

    # 3. Ensure default scaffold.
    ensure_project_scaffold(DEFAULT_SLUG)
    default_pos = os.path.join(PROJECTS_DIR, DEFAULT_SLUG, "prompt_os")
    os.makedirs(default_pos, exist_ok=True)

    # 4. Move legacy contents into the scaffolded prompt_os/, merging subdirs.
    moved: list = []

    # Top-level JSON files + known subdirs all land inside prompt_os/.
    # _merge_move_tree handles arbitrary structure, so one call covers it.
    _merge_move_tree(LEGACY_POS_DIR, default_pos, moved)

    # Attempt to remove the now-empty legacy root.
    if os.path.isdir(LEGACY_POS_DIR):
        try:
            # Only remove if truly empty
            if not os.listdir(LEGACY_POS_DIR):
                os.rmdir(LEGACY_POS_DIR)
        except OSError:
            pass

    # 5. Write meta.json for default if missing.
    if not os.path.isfile(_project_meta_path(DEFAULT_SLUG)):
        _write_project_meta(DEFAULT_SLUG, {
            "slug": DEFAULT_SLUG,
            "name": "Default",
            "created_at": _now_iso(),
        })

    # 6. Set active pointer to default.
    set_active_slug(DEFAULT_SLUG)

    # Log summary (stdout is fine — server will pick this up)
    try:
        print(f"[active_project] migrated {len(moved)} legacy files from "
              f"{LEGACY_POS_DIR} -> {default_pos}")
    except Exception:
        pass

    return DEFAULT_SLUG
