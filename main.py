"""Entry point — loads the world model + autofill and starts the command shell.

    python3 main.py

The library (containers.py, autofill.py) creates nothing on its own. This file
just launches the interactive shell in repl.py. See INSTRUCTIONS.md.
"""

from repl import run

if __name__ == "__main__":
    run()