# Desktop Workspace Migration

Migration date: 2026-10-31.

## Active locations

- Repository: `C:/Users/wsdzk/Desktop/CryoWeave`
- Research state: `C:/Users/wsdzk/Desktop/CryoWeave-state`

The previous repository under `Documents/deepseek-harness/default-workspace` is retained as a backup and is no longer the active DSH plugin source.

## Changes

- Updated all 12 plugin `cordis.patch.yml` files to use the Desktop Python environments and state directories.
- Updated Host smoke-test paths.
- Reinstalled all 12 DSH plugins from the Desktop repository; every `@cryo-research/*` dependency in the Desktop profile now links to `C:/Users/wsdzk/Desktop/CryoWeave`.
- Copied the existing `.cryo-state` content to the new state directory.

## Verification

- All 12 plugin self-tests passed from the new location.
- Repository checks passed: 40 JSON files and 13 Python files.
- Physics Host smoke passed from the new location with the expected thermal and ensemble values.
- Git history and the GitHub remote were preserved.

Restart DSH or start a new session so the Host reloads bundles from the new paths.
