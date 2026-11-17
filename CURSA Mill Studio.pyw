# Double-click to start CURSA Mill Studio without installing (needs: py -m pip install numpy pyserial).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cursa_studio.app import main  # noqa: E402

main()
