#!/usr/bin/env python3
"""Demo: Ingest a plain English SOP into a structured YAML procedure.

This script demonstrates the LLM-powered ingestion pipeline that converts
natural language procedure documents into the fine-grained YAML format
used by the BT compiler.

Usage:
    # Ingest the sample SOP
    python examples/ingest_demo.py

    # Ingest a custom text file
    python examples/ingest_demo.py path/to/your_sop.txt

    # Ingest with custom output path
    python examples/ingest_demo.py path/to/sop.txt --output procedures/my_proc.yaml

Requirements:
    - GOOGLE_API_KEY set in environment or .env file
    - pip install -r requirements.txt
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bt_engine.compiler.ingestion import ProcedureIngester
from bt_engine.compiler.tool_registry import create_default_registry


DEFAULT_SOP = Path(__file__).parent / "sample_sop.txt"


async def main():
    # Parse arguments
    sop_path = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else DEFAULT_SOP
    output_path = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = sys.argv[idx + 1]

    # Read the SOP text
    if not sop_path.exists():
        print(f"Error: File not found: {sop_path}")
        sys.exit(1)

    plain_text = sop_path.read_text()
    print(f"Input SOP ({sop_path}):")
    print("-" * 60)
    print(plain_text[:500] + ("..." if len(plain_text) > 500 else ""))
    print("-" * 60)
    print()

    # Create the ingester with the default tool registry
    registry = create_default_registry()
    ingester = ProcedureIngester(registry=registry, max_refinement_rounds=3)

    # Run the ingestion pipeline
    if output_path:
        out = output_path
    else:
        out = f"procedures/{sop_path.stem}.yaml"

    print(f"Ingesting SOP into structured YAML...")
    print(f"Output: {out}")
    print()

    try:
        result_path = await ingester.ingest_to_yaml(plain_text, out)
        print(f"Success! Procedure written to: {result_path}")
        print()
        print("Generated YAML:")
        print("=" * 60)
        print(result_path.read_text())
    except Exception as e:
        print(f"Error during ingestion: {e}")
        # Fall back to showing the procedure object
        try:
            procedure = await ingester.ingest(plain_text)
            print(f"\nProcedure created (but YAML write failed):")
            print(f"  ID: {procedure.id}")
            print(f"  Name: {procedure.name}")
            print(f"  Steps: {len(procedure.steps)}")
            for step in procedure.steps:
                print(f"    - {step.id} ({step.action.value})")
        except Exception as e2:
            print(f"Pipeline also failed: {e2}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
