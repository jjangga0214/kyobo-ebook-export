---
name: kyobo-ebook-export
description: "교보문고 eBook 소장 도서를 찾아 일반 뷰어용 PDF 또는 EPUB 파일로 저장한다. Use when the user asks to find and export books already owned and downloaded in the local Kyobo app."
compatibility: "Apple Silicon macOS; Python 3.10+, Xcode command-line tools, Poppler; verified Kyobo 3.7.10 build 1 and 3.7.16 build 8 adapters."
---

# Kyobo eBook Export

Fulfill the user's book request by running the bundled script. Search the local
owned library, select the intended title, export its original format, and report
the actual validated file. Keep conversation in the user's language.

## Workflow

Use the absolute script path under this skill directory. The examples assume the
working directory is the project root:

```sh
python3 .agents/skills/kyobo-ebook-export/scripts/kyobo.py list --query "책 제목"
python3 .agents/skills/kyobo-ebook-export/scripts/kyobo.py export --book "RETURNED_BOOK_ID" --out "/chosen/output/directory"
```

1. Run `list --query` for the requested title. Prefer an exact returned `id` for
   export. If multiple titles/editions plausibly match, show the short candidate
   list and ask which one. Do not silently export every search hit.
2. Choose the destination from the user's explicit request, otherwise from the
   active project conventions and conversation context. State the chosen path;
   ask only if the destination is genuinely ambiguous. Do not impose `books/`
   or another fixed directory. Run `export --book ID --out DIRECTORY`; `--out`
   is required. Repeat `--book` for several selected books. Use `export --all`
   only when the user requested the entire library.
3. Let the script do discovery, compatibility checks, private backup, isolated
   execution, extraction, validation, publication, and cleanup. Do not reimplement
   these steps with ad hoc shell commands or reopen the original reader.
4. Report only `exported`/`reused` files, their paths and PDF page counts or EPUB
   spine-document counts. Surface actual validation warnings and any failed book.
   Check the final `cleanup` event before declaring the operation finished.
   `ok=true` can still carry a reported analytics-only timestamp change; inspect
   `all_source_state_unchanged` and `telemetry_changes` rather than claiming every
   original file was unchanged.

The CLI emits JSON events. `blocked` means no supported path was established;
`book_failed` identifies a failed book; `complete` summarizes a run. An initialized
hook, a PDF header, or a zero process exit alone is not export success.

## What the user should know

- PDF exports preserve the decrypted PDF bytes. No OCR, screenshots, or printing.
- EPUB exports preserve decrypted publication resources and create a new standard
  EPUB ZIP container. They are not PDF conversions or byte-identical copies of
  the original compressed archive. EPUB requires an EPUB reader.
- The default `--clock license-window` can replay a cached license's valid time
  inside the isolated process for permanently owned books. Explain this local
  behavior if relevant; it does not change the Mac's clock. Use `--clock current`
  when the user requests current-time license checking only.
- Replacement characters in extracted PDF text do not prove visible page damage
  or an author/publisher error. Report the observation, not an unverified cause.

## Concrete blockers

- `book_selection_required`: resolve the returned ambiguity with the user.
- `books_unavailable`: a book is not downloaded, not a supported owned edition,
  or not PDF/EPUB. Ask the user to download the selected owned title in the
  official app, then rerun `list`. Do not buy books or change login implicitly.
- `data_selection_required`: inspect the reported candidates and use `--data`
  only when the intended container is established.
- `unsupported_app_build` / `unsupported_framework_build`: stop that adapter.
  Do not reuse offsets on a different build. Retain the exact fingerprint and
  explain that the adapter needs updating.
- `legacy_repository_unavailable` / `extraction_failed`: return the exact local
  error and run path. Do not claim that logging in or reinstalling will solve it.
- `output_collision`: preserve the existing file; choose another destination or
  ask the user. The script never overwrites a different existing book file.
- `output_busy`: wait for the export already using that output directory.

`doctor` gives a read-only preflight. If a process was interrupted before cleanup:

```sh
python3 .agents/skills/kyobo-ebook-export/scripts/kyobo.py cleanup --run /absolute/path/to/run-HEX
```

Read [references/implementation.md](references/implementation.md) only when a
failure needs diagnosis or the user asks how the exporter works. Never upload
book files, private backups, credentials, or key values to external services.
