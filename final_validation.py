"""Compatibility entry point; validation behavior comes from --config."""
import sys
from converter import main

if __name__ == "__main__":
    main(["validate", *sys.argv[1:]])
