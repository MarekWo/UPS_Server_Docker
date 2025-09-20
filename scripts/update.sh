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

# Check if we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    log_error "Not in a git repository!"
    exit 1
fi

# Check for unstaged changes
if ! git diff-index --quiet HEAD --; then
    log_warning "You have uncommitted changes. Stashing them..."
    if git stash push -m "Auto-stash before update $(date)"; then
        STASHED=1
        log_info "Changes stashed successfully"
    else
        log_error "Failed to stash changes"
        exit 1
    fi
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
        if git stash pop; then
            log_success "Stashed changes restored successfully"
        else
            log_warning "Conflicts during stash pop. Please resolve manually."
        fi
    fi
    
    log_success "Repository is current"
    exit 0
fi

# Perform update
BRANCH=$(git rev-parse --abbrev-ref HEAD)
log_info "Updating branch: $BRANCH"
git pull origin $BRANCH

# Restore stashed changes if any
if [ $STASHED -eq 1 ]; then
    log_info "Restoring stashed changes..."
    if git stash pop; then
        log_success "Stashed changes restored successfully"
    else
        log_warning "Conflicts during stash pop. Please resolve manually."
        log_info "You can resolve conflicts and run: git stash drop"
    fi
fi

# NOTE: We don't freeze version here anymore!
# The Dockerfile will handle version freezing during build with --force-clean
# This prevents version inconsistencies between host and container

log_info "Code updated successfully. Version will be determined during Docker build."

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
if [ -f Dockerfile ] || [ -f docker-compose.yml ]; then
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
        if [ -f docker-compose.yml ]; then
            log_info "Rebuilding with docker compose..."
            docker compose down
            docker compose up --build -d
        else
            log_info "Rebuilding Docker image..."
            docker build -t ups-server:latest .
        fi
        log_success "Docker image rebuilt successfully!"
        
        # Show version from container after rebuild
        echo
        log_info "Checking version in rebuilt container..."
        if command -v docker > /dev/null; then
            # Give container a moment to start
            sleep 2
            CONTAINER_VERSION=$(docker exec ups-server ups-version string 2>/dev/null || echo "unknown")
            if [ "$CONTAINER_VERSION" != "unknown" ]; then
                log_success "Container version: $CONTAINER_VERSION"
            fi
        fi
    fi
fi

# Display summary
echo
log_success "Update completed successfully!"
echo "=================================="
log_info "Repository: $(git config --get remote.origin.url)"
log_info "Branch: $(git rev-parse --abbrev-ref HEAD)"
log_info "Commit: $(git rev-parse --short HEAD)"
log_info "Note: Version is managed by Docker build process"
echo

# Show recent commits if requested
if [ "$SHOW_LOG" = true ] || [ "$1" = "--show-log" ]; then
    echo "📋 Recent changes:"
    git log --oneline -10
    echo
fi

log_info "🎉 Ready to go!"