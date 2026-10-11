"""Compatibility entry point; packaging behavior comes from --config."""
import sys
from converter import main

if __name__ == "__main__":
    main(["package", *sys.argv[1:]])
