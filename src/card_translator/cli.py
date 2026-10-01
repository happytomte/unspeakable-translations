from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from card_translator.config import library_root
from card_translator.library import apply_card_image_batch_results
from card_translator.migration import migrate_project, migration_candidates
from card_translator.release_bundles import build_release_bundles
from card_translator.shared_projects import write_json
from card_translator.status_catalog import build_status_catalog


def main() -> None:
    parser = argparse.ArgumentParser(prog="unspeakable-translations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the local web workbench")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    serve.add_argument("--reload", action="store_true")

    migrate = subparsers.add_parser("migrate-library", help="Create shared projects from legacy language folders")
    migrate.add_argument("--library", type=Path, default=None)
    migrate.add_argument("--game", default=None)
    migrate.add_argument("--slug", default=None)
    migrate.add_argument("--dry-run", action="store_true")

    status = subparsers.add_parser("status", help="Generate the local project status catalog")
    status.add_argument("--library", type=Path, default=None)
    status.add_argument("--output", type=Path, default=Path("site/data/projects.json"))
    status.add_argument("--repository-url", default="")

    release = subparsers.add_parser("release-bundles", help="Package reviewed translations")
    release.add_argument("--library", type=Path, default=None)
    release.add_argument("--output", type=Path, default=Path("release"))
    release.add_argument("--project", action="append", default=[])
    release.add_argument("--languages", default="")
    release.add_argument("--version", default="")
    release.add_argument("--require-bundles", action="store_true")

    import_batch = subparsers.add_parser(
        "import-llm-batch", help="Validate and apply prepared LLM batch results"
    )
    import_batch.add_argument("batch", type=Path)
    import_batch.add_argument("--library", type=Path, default=None)

    args = parser.parse_args()

    if args.command == "serve":
        uvicorn.run(
            "card_translator.web.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
    elif args.command == "migrate-library":
        root = args.library or library_root()
        candidates = [
            item for item in migration_candidates(root)
            if (args.game is None or item["game"] == args.game)
            and (args.slug is None or item["slug"] == args.slug)
        ]
        failures = 0
        for item in candidates:
            label = f"{item['game']}/{item['slug']} ({', '.join(item['languages'])})"
            if args.dry_run:
                print(f"Would migrate {label}")
                continue
            try:
                migrated = migrate_project(root, game=item["game"], slug=item["slug"])
            except ValueError as exc:
                failures += 1
                print(f"Skipped {label}: {exc}")
            else:
                print(f"Migrated {label} → {migrated['path']}")
        if failures:
            raise SystemExit(1)
    elif args.command == "status":
        root = args.library or library_root()
        catalog = build_status_catalog(root)
        if args.repository_url:
            catalog["repository_url"] = args.repository_url.rstrip("/")
        write_json(args.output, catalog)
        print(f"Wrote {len(catalog['projects'])} projects to {args.output}")
    elif args.command == "release-bundles":
        root = args.library or library_root()
        manifest = build_release_bundles(
            root,
            args.output,
            projects=set(args.project),
            languages={item.strip() for item in args.languages.split(",") if item.strip()},
            version=args.version,
        )
        count = len(manifest["artifacts"])
        print(f"Wrote {count} release bundles to {args.output}")
        if args.require_bundles and not count:
            raise SystemExit("No translations are marked as fully reviewed")
    elif args.command == "import-llm-batch":
        try:
            result = apply_card_image_batch_results(
                args.library or library_root(),
                args.batch,
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(f"LLM batch import failed: {exc}") from exc
        print(
            f"Applied {result['applied']} LLM results to "
            f"{result['game']}/{result['scenario']} ({result['language']})"
        )
        if result.get("failed"):
            for error in result.get("errors") or []:
                print(
                    f"Skipped {error.get('card_key')} ({error.get('side')}): "
                    f"{error.get('error')}"
                )
            raise SystemExit(1)
