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
VERSION_FILE = "/app/version_info.json"
FALLBACK_VERSION_FILE = "/var/run/nut/version_info.json"

logger = logging.getLogger(__name__)

def run_git_command(command):
    """Execute git command safely and return output."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
            cwd="/app"
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

def get_git_version_info():
    """Get version information from Git repository."""
    try:
        # Check if we're in a git repository
        if not run_git_command("git rev-parse --git-dir"):
            return None
            
        # Get commit hash (short)
        commit_hash = run_git_command("git rev-parse --short HEAD")
        
        # Get commit date
        commit_date = run_git_command("git log -1 --format=%ci")
        
        # Get commit message (first line only)
        commit_message = run_git_command("git log -1 --format=%s")
        
        # Get branch name
        branch = run_git_command("git rev-parse --abbrev-ref HEAD")
        
        # Get tag if exists
        tag = run_git_command("git describe --tags --exact-match HEAD") or \
              run_git_command("git describe --tags --abbrev=0")
        
        # Check for uncommitted changes
        has_changes = run_git_command("git diff --quiet --ignore-cr-at-eol") is None
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
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
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
    
    # Try to save to primary location
    if save_version_to_file(version_info, VERSION_FILE):
        return version_info
    # Fallback to secondary location  
    elif save_version_to_file(version_info, FALLBACK_VERSION_FILE):
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
    # Try primary version file
    version_info = load_version_from_file(VERSION_FILE)
    if version_info:
        return version_info
    
    # Try fallback version file
    version_info = load_version_from_file(FALLBACK_VERSION_FILE)
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
        else:
            print("Usage:")
            print("  python version_info.py freeze  # Freeze current version")
            print("  python version_info.py info    # Show version info")
    else:
        print_version_info()