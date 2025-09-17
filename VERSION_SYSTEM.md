# UPS Server Docker Version System

A versioning system inspired by SearXNG, automatically generating version information based on Git repository data.

## Features

- ✅ **Automatic versioning** in `YYYY.MM.DD+tag/hash` format
- ✅ **Dashboard display** similar to SearXNG
- ✅ **CLI tools** for version checking
- ✅ **API endpoint** for version information
- ✅ **Automatic freezing** during Docker builds
- ✅ **Fallback strategy** when Git is unavailable

## Version Format

The system generates versions in the format: `YYYY.MM.DD+identifier`

### Examples:
- `2025.09.17+v1.2.3` - when a Git tag exists
- `2025.09.17+a1b2c3d` - when no tag (uses commit hash)
- `2025.09.17+v1.2.3+dirty` - when there are uncommitted changes

## Usage

### 1. Web Interface

Version information is automatically displayed at the bottom of the Dashboard, similar to SearXNG:

```
Powered by UPS Server Docker - 2025.09.17+v1.2.3 | a1b2c3d | 2025-09-17
```

### 2. CLI Commands

#### Using local scripts:
```bash
# From project directory - Display detailed version information
./scripts/ups-version info

# Version string only
./scripts/ups-version string

# JSON format
./scripts/ups-version json

# Update the application
./scripts/update.sh
```

#### Using globally installed scripts:
```bash
# From anywhere on the system - Display detailed version information
ups-version info

# Version string only  
ups-version string

# JSON format
ups-version json

# Update the application (from project directory)
cd /opt/ups-server-docker
ups-update
```

#### Inside the container:
```bash
# Docker exec into container
docker exec -it ups-server bash

# Check version inside container
ups-version info

# The global command is automatically available inside container
ups-version string
```

### 3. API Endpoint

```bash
# Check version via API (no token required)
curl http://localhost:5000/version

# Web GUI API
curl http://localhost/version
```

Example response:
```json
{
  "version_string": "2025.09.17+v1.2.3",
  "commit_hash": "a1b2c3d",
  "commit_date": "2025-09-17 15:30:25 +0200",
  "commit_message": "Add version system",
  "branch": "main",
  "tag": "v1.2.3",
  "build_date": "2025-09-17T15:45:30.123456",
  "source": "git"
}
```

## Update Process

### Automated update with script:

#### Using local script:
```bash
# From project directory - Download latest changes with automatic handling
./scripts/update.sh

# With recent commits display
./scripts/update.sh --show-log

# With Docker rebuild
./scripts/update.sh --rebuild
```

#### Using globally installed script:
```bash
# From project directory - works from anywhere but runs in current dir
cd /opt/ups-server-docker
ups-update

# With options
ups-update --show-log
ups-update --rebuild
```

The script automatically:
- Checks for available updates
- Stashes local changes
- Fetches updates from Git
- Restores changes
- Freezes new version
- Optionally rebuilds Docker

### Manual update:

```bash
# Navigate to project directory
cd /opt/ups-server-docker

# Fetch latest changes
git fetch origin
git pull origin main

# Freeze version
python3 app/version_info.py freeze

# Rebuild container
docker-compose up --build -d
```

## Fallback Strategy

The system uses a fallback strategy in the following order:

1. **Frozen file** (`/app/version_info.json`) - version frozen during build
2. **Git repository** - direct Git calls (if available)
3. **Static fallback** - default version when other methods fail

## System Files

```
UPS_Server_Docker/
├── scripts/
│   ├── ups-version              # Global CLI tool for version management
│   └── update.sh                # Update script inspired by SearXNG
├── app/
│   ├── version_info.py          # Main versioning module
│   ├── version_info.json        # Frozen version (auto-generated)
│   └── version_cli.py           # CLI interface
├── .dockerignore.example        # Example Docker ignore file (Git-friendly)
└── ...
```

## Docker Integration

### Dockerfile automatically:
- Copies `.git` (if available in build context)
- Freezes version during build
- Installs CLI tools

**Important:** Ensure that `.git` directory is not excluded in your `.dockerignore` file if you want version information to be available during build. If `.git` is excluded or not available, the system will use fallback versioning at runtime.

### Common .dockerignore considerations:
```bash
# ❌ This will prevent Git versioning during build
.git

# ✅ Instead, exclude only specific Git files if needed
.git/hooks
.git/logs
# But keep .git/HEAD, .git/refs, etc. for versioning

# Or better yet, don't exclude .git at all for version info
```

### Entrypoint automatically:
- Sets up global CLI commands
- Checks version information availability
- Displays current version at startup

## Development Example

### Local development workflow:
```bash
# Check current version using local script
./scripts/ups-version info

# Output:
# 🚀 UPS Server Docker 2025.09.17+a1b2c3d+dirty
# 📦 Source: git
# 🌿 Branch: main
# 📝 Commit: a1b2c3d
# 📅 Date: 2025-09-17 15:30:25 +0200
# 💬 Message: Add version system
# 🔨 Build: 2025-09-17T15:45:30.123456

# Freeze before Docker build
./scripts/ups-version freeze

# Update with rebuild
./scripts/update.sh --rebuild
```

### Global installation workflow:
```bash
# Install scripts globally (one time setup)
sudo cp scripts/ups-version /usr/local/bin/ups-version
sudo cp scripts/update.sh /usr/local/bin/ups-update

# Now use from anywhere
cd /home/user
ups-version string
# Output: 2025.09.17+v1.2.3

# Update from project directory
cd /opt/ups-server-docker
ups-update --rebuild
```

## Troubleshooting

### Issue: "Version information unavailable"
- Check if you're in a Git repository: `git status`
- Ensure you have commits: `git log`
- Manually freeze version: `ups-version freeze`

### Issue: Docker build fails with "Failed to freeze version information"
This usually means Git is not available during Docker build:

**Solution 1: Ensure .git is available**
- Remove `.git` from `.dockerignore` if present
- Build from repository root: `docker build .` (not from subdirectory)

**Solution 2: Use the updated Dockerfile**
The fixed Dockerfile handles missing Git gracefully and will use runtime fallback.

**Solution 3: Manual verification**
```bash
# Check if .git is in build context
docker build --no-cache --progress=plain . 2>&1 | grep -E "git|Git"

# Test version system after container starts
docker run --rm your-image ups-version info
```

### Issue: CLI command not working
- Check if container is running
- Restart container: `docker-compose restart`

### Issue: Version not updating
- Remove old file: `rm app/version_info.json`
- Freeze again: `ups-version freeze`  
- Rebuild container

### Issue: Global commands not found after installation
```bash
# Verify installation
which ups-version
which ups-update

# Re-install if needed
sudo cp scripts/ups-version /usr/local/bin/
sudo cp scripts/update.sh /usr/local/bin/ups-update
sudo chmod +x /usr/local/bin/ups-version
sudo chmod +x /usr/local/bin/ups-update
```

## Installation

### For New Installations:

1. **Add the version system files** to your UPS Server Docker installation:
   - Copy `app/version_info.py`
   - Copy `app/version_cli.py` to the `app/` directory
   - Update `app/web_gui.py` and `app/api.py`
   - Update `app/templates/dashboard.html`
   - Update `Dockerfile` and `entrypoint.sh`

2. **Configure Docker build context** (important for Git versioning):
   ```bash
   # Ensure .git is not excluded (check your .dockerignore)
   # If you have .dockerignore, make sure it doesn't contain:
   # .git
   
   # Optional: Use the provided .dockerignore example
   cp .dockerignore.example .dockerignore
   ```

3. **Install system scripts** (optional but recommended):
   ```bash
   # Create scripts directory and copy scripts
   mkdir -p scripts
   cp scripts/update.sh scripts/
   cp scripts/ups-version scripts/
   
   # Make scripts executable
   chmod +x scripts/update.sh
   chmod +x scripts/ups-version
   
   # Install globally for system-wide access (optional)
   sudo cp scripts/update.sh /usr/local/bin/ups-update
   sudo cp scripts/ups-version /usr/local/bin/ups-version
   ```

4. **Rebuild container**:
   ```bash
   ./scripts/update.sh --rebuild
   # OR if installed globally:
   # ups-update --rebuild
   ```

### For Existing Installations:

1. **Update your installation**:
   ```bash
   git pull origin main
   ```

2. **Install system scripts**:
   ```bash
   chmod +x scripts/update.sh
   chmod +x scripts/ups-version
   
   # Optional: Install globally
   sudo cp scripts/update.sh /usr/local/bin/ups-update  
   sudo cp scripts/ups-version /usr/local/bin/ups-version
   ```

3. **Update with new script**:
   ```bash
   ./scripts/update.sh
   # OR if installed globally:
   # ups-update
   ```

### Global Installation Benefits

After installing scripts to `/usr/local/bin/`, you can use them from anywhere:

```bash
# Check version from anywhere on the system
ups-version info

# Update UPS Server from anywhere
ups-update

# Works from any directory
cd /home/user
ups-version string
# Output: 2025.09.17+v1.2.3
```

## Configuration

### Environment Variables

The version system respects the following environment variables:

- `TZ` - Timezone for date formatting (inherited from Docker Compose)
- No additional configuration required

### Customization

To customize version display format, edit the following files:
- `app/version_info.py` - Core versioning logic
- `app/templates/dashboard.html` - Web interface display
- `app/web_gui.py` - Version data passing to templates

## Comparison with SearXNG

| Feature | SearXNG | UPS Server |
|---------|---------|------------|
| Version format | ✅ YYYY.MM.DD+tag | ✅ YYYY.MM.DD+tag |
| Git integration | ✅ | ✅ |
| Frozen builds | ✅ | ✅ |
| CLI tools | ✅ | ✅ |
| Web display | ✅ | ✅ |
| API endpoint | ✅ | ✅ |
| Update script | ✅ | ✅ |

The system is fully compatible with SearXNG's approach but adapted for UPS Server Docker project specifics.

## API Reference

### GET /version

Returns version information in JSON format.

**Authentication:** None required

**Response:**
```json
{
  "version_string": "string",    // Human-readable version
  "commit_hash": "string",       // Git commit hash (short)
  "commit_date": "string",       // Commit date in ISO format
  "commit_message": "string",    // Commit message
  "branch": "string",           // Git branch name
  "tag": "string|null",         // Git tag (if available)
  "build_date": "string",       // Build timestamp
  "source": "string"            // Source of version info (git|file|fallback)
}
```

**Status Codes:**
- `200 OK` - Version information retrieved successfully

## Contributing

When contributing to the project:

1. **Always freeze version** before submitting Docker builds:
   ```bash
   ./scripts/ups-version freeze
   ```

2. **Use semantic Git tags** for releases (e.g., `v1.2.3`)

3. **Update version system** if you modify core versioning logic

4. **Test both local and global CLI commands** after making changes:
   ```bash
   # Test local scripts
   ./scripts/ups-version info
   ./scripts/update.sh --show-log
   
   # Test global installation (if installed)
   ups-version info
   ups-update --show-log
   ```

5. **Ensure scripts directory structure** is maintained:
   ```
   scripts/
   ├── ups-version     # Global CLI tool
   └── update.sh       # Update script
   ```

## License

This version system is part of UPS Server Docker project and follows the same MIT license terms.