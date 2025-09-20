#!/bin/bash

# update.sh - UPS Server Docker Update Script
# Inspired by SearXNG update system
# 
# Usage: 
#   ./scripts/update.sh              # Basic update
#   ./scripts/update.sh --show-log   # Show recent commits  
#   ./scripts/update.sh --rebuild    # Force Docker rebuild
#
# If installed globally:
#   ups-update                       # Basic update
#   ups-update --show-log           # Show recent commits
#   ups-update --rebuild            # Force Docker rebuild

set -e

echo "🔄 UPS Server Docker - Update Script"
echo "===================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Function to freeze version using the best available method
freeze_version() {
    local force_clean=${1:-false}
    
    # Try to use the CLI tool first (preferred method)
    if [ -x "./scripts/ups-version" ]; then
        if [ "$force_clean" = true ]; then
            ./scripts/ups-version freeze --force-clean
        else
            ./scripts/ups-version freeze
        fi
        return $?
    # Fallback to direct Python call
    elif command -v python3 > /dev/null && [ -f app/version_info.py ]; then
        if [ "$force_clean" = true ]; then
            python3 app/version_info.py freeze --force-clean
        else
            python3 app/version_info.py freeze
        fi
        return $?
    else
        log_warning "Version tools not found, skipping version freeze"
        return 1
    fi
}

# Function to get version string using the best available method
get_version_string() {
    # Try to use the CLI tool first
    if [ -x "./scripts/ups-version" ]; then
        ./scripts/ups-version string 2>/dev/null
    # Fallback to direct Python call
    elif command -v python3 > /dev/null && [ -f app/version_info.py ]; then
        python3 app/version_info.py string 2>/dev/null
    else
        echo "unknown"
    fi
}

# Check if we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    log_error "Not in a git repository!"
    exit 1
fi

# Check for unstaged changes
if ! git diff-index --quiet HEAD --; then
    log_warning "You have uncommitted changes. Stashing them..."
    git stash push -m "Auto-stash before update $(date)"
    STASHED=1
else
    STASHED=0
fi

# Fetch updates
log_info "Fetching updates from remote repository..."
git fetch origin

# Check if updates are available
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main 2>/dev/null || git rev-parse origin/master 2>/dev/null)

if [ "$LOCAL" = "$REMOTE" ]; then
    log_info "Already up to date."
    
    # Restore stashed changes if any
    if [ $STASHED -eq 1 ]; then
        log_info "Restoring stashed changes..."
        git stash pop
    fi
    
    # Always refresh version (date might have changed)
    log_info "Refreshing version info..."
    if freeze_version; then
        VERSION=$(get_version_string)
        log_success "Current version: $VERSION"
    else
        log_warning "Failed to refresh version info"
    fi
    exit 0
fi

# Perform update
BRANCH=$(git rev-parse --abbrev-ref HEAD)
log_info "Updating branch: $BRANCH"
git pull origin $BRANCH

# Restore stashed changes if any
if [ $STASHED -eq 1 ]; then
    log_info "Restoring stashed changes..."
    if ! git stash pop; then
        log_warning "Conflicts during stash pop. Please resolve manually."
    fi
fi

# Freeze new version with force-clean for consistency
log_info "Freezing version information..."
if freeze_version true; then
    VERSION=$(get_version_string)
    log_success "Updated to version: $VERSION"
else
    log_warning "Failed to freeze version. Continuing anyway..."
    VERSION="unknown"
fi

# Parse command line arguments
FORCE_REBUILD=false
SHOW_LOG=false

for arg in "$@"; do
    case $arg in
        --rebuild)
            FORCE_REBUILD=true
            ;;
        --show-log)
            SHOW_LOG=true
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Usage: $0 [--rebuild] [--show-log]"
            exit 1
            ;;
    esac
done

# Check if we need to rebuild Docker
if [ -f Dockerfile ] || [ -f docker compose.yml ]; then
    echo
    log_info "Docker configuration detected."
    
    if [ "$FORCE_REBUILD" = true ]; then
        REPLY="y"
        log_info "Force rebuild requested via --rebuild flag"
    else
        read -p "Do you want to rebuild Docker image? [y/N]: " -n 1 -r
        echo
    fi
    
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if [ -f docker compose.yml ]; then
            log_info "Rebuilding with docker compose..."
            docker compose down
            docker compose up --build -d
        else
            log_info "Rebuilding Docker image..."
            # Only tag with version if we have a valid one
            if [ "$VERSION" != "unknown" ] && [ -n "$VERSION" ]; then
                docker build -t ups-server:"$VERSION" -t ups-server:latest .
            else
                docker build -t ups-server:latest .
            fi
        fi
        log_success "Docker image rebuilt successfully!"
    fi
fi

# Display summary
echo
log_success "Update completed successfully!"
echo "=================================="
log_info "Repository: $(git config --get remote.origin.url)"
log_info "Branch: $(git rev-parse --abbrev-ref HEAD)"
log_info "Commit: $(git rev-parse --short HEAD)"
if [ "$VERSION" != "unknown" ] && [ -n "$VERSION" ]; then
    log_info "Version: $VERSION"
fi
echo

# Show recent commits if requested
if [ "$SHOW_LOG" = true ] || [ "$1" = "--show-log" ]; then
    echo "📋 Recent changes:"
    git log --oneline -10
    echo
fi

log_info "🎉 Ready to go!"