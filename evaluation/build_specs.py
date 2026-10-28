"""Resolve official per-instance image names for a frozen legacy Lite dataset."""
import argparse
import json
from pathlib import Path


def main():
    from swebench.harness.test_spec.test_spec import make_test_spec
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    specs = {}
    for instance in json.loads(args.dataset.read_text()):
        spec = make_test_spec(instance, namespace="swebench", arch="x86_64")
        specs[instance["instance_id"]] = {"image": spec.instance_image_key}
    args.output.write_text(json.dumps(specs, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
