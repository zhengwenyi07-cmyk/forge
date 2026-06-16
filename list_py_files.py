#!/usr/bin/env python3
"""Recursively list all .py files under /workspace."""

import os

root_dir = "/workspace"

for dirpath, dirnames, filenames in os.walk(root_dir):
    # Skip hidden directories like .git
    dirnames[:] = [d for d in dirnames if not d.startswith('.')]
    for filename in filenames:
        if filename.endswith('.py'):
            print(os.path.join(dirpath, filename))
