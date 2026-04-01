"""
Project Manager - Multi-project support for LUMN Studio.

Manages projects stored in output/projects/ as directories.
Each project has its own scene plan, clips, settings, and metadata.
"""

import json
import os
import shutil
import time
import uuid


class ProjectManager:
    def __init__(self, projects_dir: str, output_dir: str, uploads_dir: str,
                 references_dir: str):
        self.projects_dir = projects_dir
        self.output_dir = output_dir
        self.uploads_dir = uploads_dir
        self.references_dir = references_dir
        self.current_project_file = os.path.join(output_dir, "current_project.json")
        os.makedirs(projects_dir, exist_ok=True)

    def create_project(self, name: str) -> dict:
        """Create a new project directory with metadata."""
        project_id = str(uuid.uuid4())[:8]
        project_dir = os.path.join(self.projects_dir, project_id)
        os.makedirs(project_dir, exist_ok=True)
        os.makedirs(os.path.join(project_dir, "clips"), exist_ok=True)
        os.makedirs(os.path.join(project_dir, "photos"), exist_ok=True)
        os.makedirs(os.path.join(project_dir, "previews"), exist_ok=True)

        metadata = {
            "id": project_id,
            "name": name,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "modified": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scene_count": 0,
            "thumbnail": None,
        }
        meta_path = os.path.join(project_dir, "metadata.json")
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2)

        # Initialize empty scene plan
        plan = {"scenes": [], "song_path": None}
        with open(os.path.join(project_dir, "manual_scene_plan.json"), "w") as f:
            json.dump(plan, f, indent=2)

        # Initialize empty settings
        settings = {"default_engine": "grok", "character_references": {}}
        with open(os.path.join(project_dir, "settings.json"), "w") as f:
            json.dump(settings, f, indent=2)

        # Initialize cost tracker
        cost = {"total_cost": 0.0, "video_generations": 0,
                "image_generations": 0, "budget": 10.0, "scene_costs": {}}
        with open(os.path.join(project_dir, "cost_tracker.json"), "w") as f:
            json.dump(cost, f, indent=2)

        return metadata

    def list_projects(self) -> list:
        """Return list of all projects with metadata."""
        projects = []
        if not os.path.isdir(self.projects_dir):
            return projects
        for entry in os.listdir(self.projects_dir):
            project_dir = os.path.join(self.projects_dir, entry)
            if not os.path.isdir(project_dir):
                continue
            meta_path = os.path.join(project_dir, "metadata.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, "r") as f:
                    meta = json.load(f)
                # Count scenes
                plan_path = os.path.join(project_dir, "manual_scene_plan.json")
                if os.path.isfile(plan_path):
                    with open(plan_path, "r") as f:
                        plan = json.load(f)
                    meta["scene_count"] = len(plan.get("scenes", []))
                # Check for thumbnail
                thumb_path = os.path.join(project_dir, "thumbnail.jpg")
                if os.path.isfile(thumb_path):
                    meta["thumbnail"] = f"/api/projects/{meta['id']}/thumbnail"
                projects.append(meta)
            except (json.JSONDecodeError, IOError):
                continue
        projects.sort(key=lambda p: p.get("modified", ""), reverse=True)
        return projects

    def _save_current_workspace(self):
        """Save current workspace files into the currently loaded project."""
        current = self.get_current_project()
        if not current:
            return
        project_dir = os.path.join(self.projects_dir, current["id"])
        if not os.path.isdir(project_dir):
            return

        # Save scene plan
        src_plan = os.path.join(self.output_dir, "manual_scene_plan.json")
        if os.path.isfile(src_plan):
            shutil.copy2(src_plan, os.path.join(project_dir, "manual_scene_plan.json"))

        # Save settings
        src_settings = os.path.join(self.output_dir, "settings.json")
        if os.path.isfile(src_settings):
            shutil.copy2(src_settings, os.path.join(project_dir, "settings.json"))

        # Save cost tracker
        src_cost = os.path.join(self.output_dir, "cost_tracker.json")
        if os.path.isfile(src_cost):
            shutil.copy2(src_cost, os.path.join(project_dir, "cost_tracker.json"))

        # Update metadata
        meta_path = os.path.join(project_dir, "metadata.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r") as f:
                    meta = json.load(f)
                meta["modified"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                if os.path.isfile(src_plan):
                    with open(src_plan, "r") as f:
                        plan = json.load(f)
                    meta["scene_count"] = len(plan.get("scenes", []))
                with open(meta_path, "w") as f:
                    json.dump(meta, f, indent=2)
            except (json.JSONDecodeError, IOError):
                pass

    def load_project(self, project_id: str) -> dict:
        """Load a project: save current workspace, then restore project files."""
        project_dir = os.path.join(self.projects_dir, project_id)
        meta_path = os.path.join(project_dir, "metadata.json")
        if not os.path.isfile(meta_path):
            return None

        # Save current workspace first
        self._save_current_workspace()

        with open(meta_path, "r") as f:
            meta = json.load(f)

        # Restore scene plan
        src_plan = os.path.join(project_dir, "manual_scene_plan.json")
        if os.path.isfile(src_plan):
            shutil.copy2(src_plan, os.path.join(self.output_dir, "manual_scene_plan.json"))
        else:
            # Create empty plan
            with open(os.path.join(self.output_dir, "manual_scene_plan.json"), "w") as f:
                json.dump({"scenes": [], "song_path": None}, f, indent=2)

        # Restore settings
        src_settings = os.path.join(project_dir, "settings.json")
        if os.path.isfile(src_settings):
            shutil.copy2(src_settings, os.path.join(self.output_dir, "settings.json"))

        # Restore cost tracker
        src_cost = os.path.join(project_dir, "cost_tracker.json")
        if os.path.isfile(src_cost):
            shutil.copy2(src_cost, os.path.join(self.output_dir, "cost_tracker.json"))

        # Set current project pointer
        with open(self.current_project_file, "w") as f:
            json.dump({"id": project_id, "name": meta.get("name", "")}, f, indent=2)

        return meta

    def delete_project(self, project_id: str) -> bool:
        """Delete a project and all its files."""
        project_dir = os.path.join(self.projects_dir, project_id)
        if not os.path.isdir(project_dir):
            return False
        # Don't delete the currently loaded project
        current = self.get_current_project()
        if current and current.get("id") == project_id:
            # Unset current
            if os.path.isfile(self.current_project_file):
                os.remove(self.current_project_file)
        shutil.rmtree(project_dir, ignore_errors=True)
        return True

    def get_current_project(self) -> dict:
        """Get the currently loaded project info."""
        if not os.path.isfile(self.current_project_file):
            return None
        try:
            with open(self.current_project_file, "r") as f:
                data = json.load(f)
            # Verify the project still exists
            project_dir = os.path.join(self.projects_dir, data.get("id", ""))
            if os.path.isdir(project_dir):
                return data
            return None
        except (json.JSONDecodeError, IOError):
            return None

    def save_current(self):
        """Explicitly save current workspace to the current project."""
        self._save_current_workspace()

    def rename_project(self, project_id: str, new_name: str) -> dict:
        """Rename a project."""
        project_dir = os.path.join(self.projects_dir, project_id)
        meta_path = os.path.join(project_dir, "metadata.json")
        if not os.path.isfile(meta_path):
            return None
        with open(meta_path, "r") as f:
            meta = json.load(f)
        meta["name"] = new_name
        meta["modified"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)
        # Update current project pointer if this is the active project
        current = self.get_current_project()
        if current and current.get("id") == project_id:
            with open(self.current_project_file, "w") as f:
                json.dump({"id": project_id, "name": new_name}, f, indent=2)
        return meta
