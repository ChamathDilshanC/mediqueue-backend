"""Validate configuration snapshots against the repository JSON Schema."""
import argparse, json
from pathlib import Path
from jsonschema import validate

def validate_file(path: str) -> None:
    """Raise a validation error when a configuration snapshot is invalid."""
    root = Path(__file__).parent
    validate(json.loads(Path(path).read_text(encoding="utf-8")), json.loads((root / "schema.json").read_text(encoding="utf-8")))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("path", nargs="?", default=str(Path(__file__).parent / "defaults.json"))
    validate_file(parser.parse_args().path); print("configuration valid")
