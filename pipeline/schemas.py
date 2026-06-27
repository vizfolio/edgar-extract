"""Emit JSON Schemas for every published wire shape.

Schemas are derived from the pydantic models in `pipeline.models`, so
the models themselves stay the single source of truth. Run this after
any model change and commit the resulting files; CI also regenerates
them before publishing data so consumers always get schema + data in
lockstep.

    nix develop -c python -m pipeline.schemas
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import models

log = logging.getLogger(__name__)

_TARGETS = [
    ("fund_snapshot.json", models.FundSnapshot),
    ("funds_manifest.json", models.FundsManifest),
    ("security.json", models.Security),
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate JSON Schemas from pipeline.models.")
    p.add_argument("--out", default="schemas", help="Output directory")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for filename, model in _TARGETS:
        schema = model.model_json_schema(by_alias=True)
        path = out_dir / filename
        with open(path, "w") as f:
            json.dump(schema, f, indent=2, sort_keys=True)
            f.write("\n")
        log.info("wrote %s", path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
