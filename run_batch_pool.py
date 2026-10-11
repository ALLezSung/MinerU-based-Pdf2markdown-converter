"""Compatibility entry point; concurrency comes from the task config."""
import sys
from converter import main

if __name__ == "__main__":
    main(["batch", *sys.argv[1:]])
