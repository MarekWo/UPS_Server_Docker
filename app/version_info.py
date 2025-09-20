#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UPS Server Version Information Module
Author: MarekWo
Description: Simple version management system inspired by SearXNG
"""

import os
import sys
import subprocess
import json
import logging
from datetime import datetime
from pathlib import Path

# Configuration
def get_version_file_paths():
    """Get appropriate paths for version files based on environment."""
    working_dir = get_working_directory()
    
    if working_dir == "/app":
        # Container environment
        return "/app/version_info.json", "/var/run/nut/version_info.json"
    else:
        # Host environment - store in app subdirectory or current directory
        primary_path = os.path.join(working_dir, "version_info.json")
        fallback_path = os.path.join(working_dir, "app", "version_info.json") if working_dir != "./app" else "version_info.json"
        return primary_path, fallback_path

logger = logging.getLogger(__name__)

def get_working_directory():
    """
    Auto-detect the correct working directory for git operations.
    Works both in container (/app) and on host (./app or current dir).
    """
    # Try different possible locations
    candidates = [
        "/app",  # Container environment
        "./app", # Host environment from project root
        ".",     # Current directory if it contains version_info.py
        os.path.dirname(os.path.abspath(__file__))  # Directory where this script is located
    ]
    
    for candidate in candidates:
        if os.path.exists(candidate) and os.path.exists(os.path.join(candidate, ".git")):
            return os.path.abspath(candidate)
        elif os.path.exists(candidate) and os.path.exists(os.path.join(os.path.dirname(candidate), ".git")):
            # If we're in app/ subdirectory, go up one level to find .git
            return os.path.abspath(os.path.dirname(candidate))
    
    # Fallback to current directory
    return os.getcwd()

def run_git_command(command, working_dir=None):
    """Execute git command safely and return output."""
    if working_dir is None:
        working_dir = get_working_directory()
    
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=working_dir
        )
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            # Don't log warnings for commands that are expected to fail as part of the logic,
            # like trying to find an exact tag.
            if "no tag" not in result.stderr.strip() and "No names found" not in result.stderr.strip():
                logger.warning(f"Git command failed: {command} - {result.stderr.strip()}")
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"Git command error: {command} - {str(e)}")
        return None

def check_git_dirty_status(working_dir=None):
    """
    Enhanced function to check if working directory has uncommitted changes.
    Uses multiple strategies to avoid false positives during Docker builds.
    """
    if working_dir is None:
        working_dir = get_working_directory()
        
    try:
        # Strategy 1: Use git status --porcelain (more reliable than git diff)
        status_output = run_git_command("git status --porcelain", working_dir)
        if status_output is None:
            # Git command failed, assume clean to avoid false dirty flag
            logger.warning("Could not determine git status, assuming clean")
            return False
        
        # If output is empty, working directory is clean
        if not status_output.strip():
            return False
        
        # Strategy 2: Check what specific changes exist
        # Split output into lines and analyze
        changes = [line.strip() for line in status_output.split('\n') if line.strip()]
        
        # Count only meaningful changes (ignore file mode changes in Docker context)
        meaningful_changes = []
        for change in changes:
            # Skip file mode only changes (common in Docker)
            if len(change) >= 2:
                status_code = change[:2]
                # 'M ' means modified content, ' M' means modified in working tree
                # Skip pure mode changes which might be '??' or similar non-content changes
                if status_code in ['M ', ' M', 'MM', 'A ', ' A', 'D ', ' D', 'R ', ' R', 'C ', ' C']:
                    meaningful_changes.append(change)
        
        if meaningful_changes:
            logger.info(f"Found meaningful changes: {meaningful_changes}")
            return True
        else:
            logger.info(f"Only non-meaningful changes found: {changes}")
            return False
    
    except Exception as e:
        logger.warning(f"Error checking git status: {e}")
        # On error, assume clean to avoid false dirty flag
        return False

def get_git_version_info():
    """Get version information from Git repository."""
    working_dir = get_working_directory()
    
    try:
        # Check if we're in a git repository
        if not run_git_command("git rev-parse --git-dir", working_dir):
            return None
        
        # IMPORTANT: Configure Git settings FIRST, before any checks
        # This prevents false dirty flags due to file mode changes during Docker build
        run_git_command("git config core.filemode false", working_dir)
        run_git_command("git config core.autocrlf false", working_dir)
        run_git_command("git config core.safecrlf false", working_dir)
        
        # Try to refresh the index to avoid stale state issues
        run_git_command("git update-index --refresh", working_dir)

        # Get commit hash (short)
        commit_hash = run_git_command("git rev-parse --short HEAD", working_dir)
        
        # Get commit date
        commit_date = run_git_command("git log -1 --format=%ci", working_dir)
        
        # Get commit message (first line only)
        commit_message = run_git_command("git log -1 --format=%s", working_dir)
        
        # Get branch name
        branch = run_git_command("git rev-parse --abbrev-ref HEAD", working_dir)
        
        # Get tag if exists
        tag = run_git_command("git describe --tags --exact-match HEAD", working_dir) or \
              run_git_command("git describe --tags --abbrev=0", working_dir)
        
        # Check for uncommitted changes using enhanced method
        has_changes = check_git_dirty_status(working_dir)
        dirty_suffix = "+dirty" if has_changes else ""
        
        if commit_hash and commit_date:
            # Parse date
            try:
                date_obj = datetime.fromisoformat(commit_date.replace(' +', '+'))
                formatted_date = date_obj.strftime('%Y.%m.%d')
            except:
                formatted_date = datetime.now().strftime('%Y.%m.%d')
            
            # Create version string in SearXNG format: YYYY.MM.DD+tag
            if tag:
                version_string = f"{formatted_date}+{tag}{dirty_suffix}"
            else:
                version_string = f"{formatted_date}+{commit_hash}{dirty_suffix}"
            
            return {
                "version_string": version_string,
                "commit_hash": commit_hash,
                "commit_date": commit_date,
                "commit_message": commit_message or "No commit message",
                "branch": branch or "unknown",
                "tag": tag,
                "build_date": datetime.now().isoformat(),
                "source": "git"
            }
    
    except Exception as e:
        logger.error(f"Error getting git version info: {e}")
    
    return None

def load_version_from_file(filepath):
    """Load version information from JSON file."""
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                data = json.load(f)
                # Add source indicator
                data["source"] = "file"
                return data
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Could not load version from {filepath}: {e}")
    return None

def save_version_to_file(version_info, filepath):
    """Save version information to JSON file."""
    try:
        # Ensure directory exists
        directory = os.path.dirname(filepath)
        if directory:  # Only create if there's a directory component
            os.makedirs(directory, exist_ok=True)
        
        with open(filepath, 'w') as f:
            json.dump(version_info, f, indent=2)
        logger.info(f"Version info saved to {filepath}")
        return True
    except IOError as e:
        logger.error(f"Could not save version to {filepath}: {e}")
        return False

def freeze_version():
    """Freeze current version information to file."""
    version_info = get_git_version_info()
    
    # If Git is not available, create fallback version
    if not version_info:
        logger.warning("Git repository not available, creating fallback version")
        version_info = {
            "version_string": f"{datetime.now().strftime('%Y.%m.%d')}+unknown",
            "commit_hash": "unknown",
            "commit_date": "unknown", 
            "commit_message": "Built without Git repository",
            "branch": "unknown",
            "tag": None,
            "build_date": datetime.now().isoformat(),
            "source": "fallback"
        }
    
    # Get dynamic paths
    primary_path, fallback_path = get_version_file_paths()
    
    # Try to save to primary location
    if save_version_to_file(version_info, primary_path):
        return version_info
    # Fallback to secondary location  
    elif save_version_to_file(version_info, fallback_path):
        return version_info
    
    logger.error("Failed to save version to any location")
    return None

def get_version_info():
    """
    Get version information with fallback strategy:
    1. Try to load from frozen file
    2. Fallback to Git repository
    3. Fallback to default version
    """
    # Get dynamic paths
    primary_path, fallback_path = get_version_file_paths()
    
    # Try primary version file
    version_info = load_version_from_file(primary_path)
    if version_info:
        return version_info
    
    # Try fallback version file
    version_info = load_version_from_file(fallback_path)
    if version_info:
        return version_info
    
    # Try to get from Git directly
    version_info = get_git_version_info()
    if version_info:
        return version_info
    
    # Final fallback - static version
    return {
        "version_string": f"{datetime.now().strftime('%Y.%m.%d')}+unknown",
        "commit_hash": "unknown",
        "commit_date": "unknown",
        "commit_message": "Version information unavailable",
        "branch": "unknown",
        "tag": None,
        "build_date": datetime.now().isoformat(),
        "source": "fallback"
    }

def get_version_string():
    """Get just the version string."""
    return get_version_info()["version_string"]

def print_version_info():
    """Print detailed version information to console."""
    info = get_version_info()
    
    print(f"🚀 UPS Server Docker {info['version_string']}")
    print(f"📦 Source: {info['source']}")
    print(f"🌿 Branch: {info['branch']}")
    print(f"📝 Commit: {info['commit_hash']}")
    if info.get('tag'):
        print(f"🏷️  Tag: {info['tag']}")
    print(f"📅 Date: {info['commit_date']}")
    print(f"💬 Message: {info['commit_message']}")
    print(f"🔨 Build: {info['build_date']}")

# Debug function to help troubleshoot dirty status
def debug_git_status():
    """Debug function to show detailed git status information."""
    working_dir = get_working_directory()
    primary_path, fallback_path = get_version_file_paths()
    
    print("🔍 Git Status Debug Information:")
    print("=" * 40)
    print(f"Working directory: {working_dir}")
    print(f"Primary version file: {primary_path}")
    print(f"Fallback version file: {fallback_path}")
    print(f"Git repository exists: {os.path.exists(os.path.join(working_dir, '.git'))}")
    print("")
    
    commands = [
        "git status --porcelain",
        "git diff --name-only",
        "git diff --cached --name-only", 
        "git ls-files --modified",
        "git ls-files --others --exclude-standard"
    ]
    
    for cmd in commands:
        print(f"📋 {cmd}:")
        result = run_git_command(cmd, working_dir)
        if result:
            print(result)
        else:
            print("(no output)")
        print("")
    
    # Check specific git configs
    print(f"⚙️  Git Configuration:")
    print(f"core.filemode: {run_git_command('git config core.filemode', working_dir)}")
    print(f"core.autocrlf: {run_git_command('git config core.autocrlf', working_dir)}")
    print(f"core.safecrlf: {run_git_command('git config core.safecrlf', working_dir)}")
    
    # Test our dirty check logic
    print(f"\n🧪 Dirty Status Check:")
    print(f"Has changes: {check_git_dirty_status(working_dir)}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "freeze":
            result = freeze_version()
            if result:
                print("✅ Version information frozen successfully")
                print_version_info()
            else:
                print("❌ Failed to freeze version information")
                sys.exit(1)
        elif sys.argv[1] == "info":
            print_version_info()
        elif sys.argv[1] == "debug":
            debug_git_status()
        else:
            print("Usage:")
            print("  python version_info.py freeze  # Freeze current version")
            print("  python version_info.py info    # Show version info")
            print("  python version_info.py debug   # Debug git status")
    else:
        print_version_info()