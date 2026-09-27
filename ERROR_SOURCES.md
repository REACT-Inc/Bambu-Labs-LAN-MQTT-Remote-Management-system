# Printer error descriptions: sources

When a printer reports an **HMS alert** or a **print error**, the dashboard and Discord show the official Bambu description, the code, and a link to Bambu's support page. The descriptions come from a bundled offline catalog, `bambu_error_catalog.json`, used by `printer_errors.py`.

## Source

The catalog is the English HMS and print-error resource files from the official **Bambu Studio** repository, unchanged:

- Repository: https://github.com/bambulab/BambuStudio/tree/f977235e6d736c4c0b650520ac5a5b72cbfe9244/resources/hms
- Commit: `f977235e6d736c4c0b650520ac5a5b72cbfe9244`
- Files: `hms_en_093.json`, `hms_en_094.json`, `hms_en_20P.json`, `hms_en_22E.json`, `hms_en_239.json`, `hms_en_26A.json`, `hms_en_31B.json`

Copyright belongs to the upstream authors. The upstream license is included as `BAMBU_RESOURCE_LICENSE.txt`.

## How codes are matched

- **Exact matches only.** A code is only described if that exact code exists in the catalog. Nothing is borrowed from a different module.
- **Choosing the model:**
  1. If the printer entry sets `error_model`, that catalog is used.
  2. Otherwise, the first 3 characters of the printer's **serial number** pick the catalog.
  3. If that doesn't match, the code is looked up across all catalogs, but only if it has a single, unambiguous description.
- **Unknown codes** are shown as unknown, with the code and a Bambu support link, and never guessed.
- **HMS alerts and print errors** are decoded separately.

## Updating the catalog

This is a snapshot, not a live lookup. `bambu_error_catalog.json` has three parts:
- `source`: the repository URL
- `commit`: the commit the files came from
- `catalogs`: one entry per model code (`093`, `094`, `20P`, `22E`, `239`, `26A`, `31B`). Each entry is the content of that `hms_en_<code>.json` file.

To pick up new codes:
1. Replace the entries with the newer `resources/hms/hms_en_*.json` content from Bambu Studio, and set `commit`.
2. Update the commit and file list above.
3. Run the tests.
