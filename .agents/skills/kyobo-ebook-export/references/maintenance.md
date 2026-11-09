# Adapter maintenance

## Runtime boundaries

The skill is self-contained. Its scripts read the user's installed app and local
reader sandbox, create a private compatibility copy, validate output, and clean
up the disposable runtime. No development archive, prior export, or personal
research directory is needed.

Output paths are selected by the caller through `--out`. The cache manifest and
private `.kyobo-export` run directories live under that chosen location. Account
state, certificates, licenses, books, and raw run logs must remain outside the
published skill and Git history.

## Reviewing a new app build

Do not add a new hash to an old adapter without inspecting the build. A version
number alone is not enough: main-image and framework fingerprints must match the
reviewed profile.

1. Inspect the fresh Mach-O load commands, architecture slices, available header
   padding, encrypted region, entry point, and function-start metadata.
2. Inspect the Objective-C names and type encodings of the reader methods used by
   `NativeExport.m`. The helper verifies those signatures again at runtime.
3. Derive compatibility edits from the new image's own structure. Do not reuse
   virtual addresses from a different build.
4. Keep edits constrained to the disposable copy. The native export constructor
   exits before the ordinary app entry/UI; any encrypted-page stubs are not
   recovered original code or a general-purpose reader replacement.
5. Add a helper load command only when verified empty header padding is available.
6. Preserve source code-signature and `__LINKEDIT` extents until `codesign`
   replaces the signature coherently. Copying shortened signing-size fields from
   another signed image while retaining the source bytes can fail validation.
7. Re-sign only disposable images. Test both PDF and EPUB exports, original-state
   preservation, failure handling, and cleanup before documenting support.

## Regression expectations

- Compare PDF byte hashes when the same source is exported by two supported
  reader builds.
- Compare EPUB member contents, not just whole-archive hashes: ZIP timestamps and
  compression can legitimately differ after packaging.
- Test duplicate titles, edition ambiguity, missing downloads, changed binary
  fingerprints, unsafe paths, output collisions, and interrupted runs.
- Confirm failures remain explicit and do not overwrite user output or restore a
  stale database over live app state.
- Keep console output bounded. Detailed diagnostics belong in local run reports;
  do not attach publication text or account-bearing backups to public issues.

## Validation claims

Successful file creation alone is not successful export. Preserve the
distinctions between native read success, format validation, full-page rendering,
text-extraction quality, and EPUB conformance. Refer to the current run's results
rather than generalizing from previous tests.

The adapters contain binary fingerprints and verified edit preconditions. The
repository must not include original app bundles, credentials, certificate or
license stores, or book payloads.
