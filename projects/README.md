# Tracked projects

This directory contains shared project files that can be published and downloaded by
the workbench:

```text
projects/<game>/<campaign>/
├── campaign.json
└── scenarios/<scenario>/
    ├── project.json
    ├── source/
    ├── base/
    │   ├── extraction.json
    │   └── cards/<card-key>.json
    └── translations/<language>/
        ├── project.json
        ├── translation.json
        └── cards/<card-key>.json
```

The local working area uses the same structure under `library/projects/`. Running
`unspeakable-translations status --library .` from the repository root generates the static
catalog from this tracked directory. API keys, local logs, import snapshots, caches,
and temporary files do not belong here. Intentionally generated language
builds may be published with their translation.

`campaign.json` owns campaign-wide metadata such as title, author, source URL, and
source language. Card images, extraction data, translations, builds, and logs stay
inside their scenario. Legacy `projects/<game>/<scenario>/` directories remain readable.
`base/extraction.json` is a small manifest; each card's extracted source data and review
metadata live together in its referenced `base/cards/` file so independent card changes
can be reviewed and merged without rewriting one project-wide JSON document.
Translations use the same card-level layout, but remain sparse: a translation card file
exists only after that card has translated content or a language-specific reference.
