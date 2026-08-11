import sys
import os

# Make the tests directory importable so "from helpers import *" works
# regardless of where pytest is invoked from.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))
# Make the project root importable for the source modules.
sys.path.insert(0, os.path.dirname(__file__))
