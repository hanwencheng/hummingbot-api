#!/usr/bin/env python3
"""
Activate script to help Cursor find the correct Python interpreter.
Run this script and it will show the Python path to use in Cursor.
"""
import os
import sys

# Get the virtual environment python path
venv_python = os.path.join(os.getcwd(), "venv", "bin", "python")

print("=" * 60)
print("Hummingbot API Development Environment Setup")
print("=" * 60)
print(f"Python interpreter for Cursor: {venv_python}")
print(f"Current working directory: {os.getcwd()}")
print(f"Virtual environment location: {os.path.join(os.getcwd(), 'venv')}")
print()
print("To use in Cursor:")
print("1. Open Command Palette (Cmd+Shift+P)")
print("2. Search for 'Python: Select Interpreter'")
print(f"3. Select or enter: {venv_python}")
print()
print("Or create a .vscode/settings.json with:")
print(f'{{ "python.pythonPath": "{venv_python}" }}')
print("=" * 60)

# Test imports
try:
    import fastapi
    import uvicorn
    import sqlalchemy
    print("✅ Core dependencies available")
except ImportError as e:
    print(f"❌ Missing dependency: {e}")