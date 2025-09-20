#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UPS Server Version CLI Tool
Author: MarekWo
Description: Command-line interface for version management
"""

import sys
import os
import argparse
import json

# Add app directory to path
sys.path.insert(0, '/app')

try:
    from version_info import get_version_info, freeze_version, print_version_info, debug_git_status
except ImportError:
    print("Error: version_info module not found", file=sys.stderr)
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description='UPS Server Version Management Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s info          Show version information
  %(prog)s freeze        Freeze current version to file
  %(prog)s json          Output version as JSON
  %(prog)s string        Output just the version string
  %(prog)s debug         Debug git status (troubleshooting)
        '''
    )
    
    parser.add_argument('command', 
                       choices=['info', 'freeze', 'json', 'string', 'debug'],
                       help='Command to execute')
    
    args = parser.parse_args()
    
    if args.command == 'info':
        print_version_info()
    
    elif args.command == 'freeze':
        result = freeze_version()
        if result:
            print("✅ Version information frozen successfully")
            print_version_info()
        else:
            print("❌ Failed to freeze version information", file=sys.stderr)
            sys.exit(1)
    
    elif args.command == 'json':
        version_info = get_version_info()
        print(json.dumps(version_info, indent=2))
    
    elif args.command == 'string':
        version_info = get_version_info()
        print(version_info['version_string'])
    
    elif args.command == 'debug':
        debug_git_status()

if __name__ == '__main__':
    main()