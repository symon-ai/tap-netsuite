# Changelog

All notable changes to `tap-netsuite` are documented here. The released
version of this package is the value of `version` in `pyproject.toml`, and
each release is delivered to consumers as a git tag named `v<version>`
(e.g. pyproject `2.4.1` -> tag `v2.4.1`). `elt/app`'s import-activity image
build clones this tap by that tag
(`api/data/service/import-activity/docker_build.sh` -> `git clone --branch v<version>`),
so a tag matching the released `version` MUST exist for the consumer to build.

## 2.4.1

- Security: sanitize the config-supplied `error_file_path` against path
  traversal (CWE-73). `error_file_path` resolved from tap config is now
  constrained to a trusted output directory before `open()`; absolute and
  traversal paths that escape it are rejected. (WP-32503)

> RELEASE ACTION REQUIRED ON MERGE: cut git tag `v2.4.1` pointing at the
> merge commit so `elt/app`'s import-activity `docker_build.sh`
> `--branch v2.4.1` clone resolves. The remote currently maxes at `v2.1.0`
> (versions 2.2.0/2.3.0/2.4.0 were bumped in `pyproject.toml` for prior
> security dependency upgrades but were never tagged); `v2.4.1` is the tag
> that must be pushed to actually ship this remediated tap.
