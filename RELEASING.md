# Releasing

Releases are cut by pushing a `v*` tag. `.github/workflows/release.yml` builds all five
targets and attaches the archives to a GitHub Release. Nothing is published from a laptop.

## Cut a release

```bash
# 1. Bump the version and commit it.
sed -i '' 's/^version = .*/version = "0.1.1"/' pyproject.toml
git add pyproject.toml
git commit -m "chore: release v0.1.1"

# 2. Push the commit first, then the tag.
git push
git tag v0.1.1
git push origin v0.1.1

# 3. Watch the build (~5-10 min; macos-13 runners are usually the slow one).
gh run watch "$(gh run list --workflow release --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Push the commit before the tag. A tag pointing at an unpushed commit builds whatever the
remote has, which silently ships the previous code.

## Verify

```bash
gh release view "v0.1.1" --json assets --jq '.assets[].name'
```

Expect ten assets, an archive plus a `.sha256` for each target:

```
sumologic-darwin-arm64.tar.gz     sumologic-linux-arm64.tar.gz
sumologic-darwin-x86_64.tar.gz    sumologic-linux-x86_64.tar.gz
sumologic-windows-x86_64.zip
```

Then install it the way a user would, from a clean shell:

```bash
curl -fsSL https://raw.githubusercontent.com/rushi/sumologic-cli/main/install.sh | bash
sumologic --list
```

`install.sh` resolves `releases/latest`, so a release that is still building, or one marked
prerelease, will not be picked up.

## If the build fails

Fix forward. Delete the tag, push a corrected commit, retag:

```bash
git tag -d v0.1.1
git push origin :refs/tags/v0.1.1
gh release delete v0.1.1 --yes   # only if a release was already created
```

To rebuild without cutting a tag, run the workflow manually. It builds and uploads
artifacts but skips the release job, which is gated on `refs/tags/`:

```bash
gh workflow run release
```

## Things that break releases

- **A missing runner.** `ubuntu-24.04-arm` and `macos-13` queue longer than the rest. The
  release job needs every build job to finish, so one stuck runner holds the whole release.
- **A new mcp2cli version.** `pyproject.toml` pins `mcp2cli==3.6.0`. Bumping it can pull in
  imports PyInstaller does not trace. Build locally first (see README) and run
  `./dist/sumologic --list` before tagging.
- **Deleting `docs/client.json` or turning off GitHub Pages.** SumoLogic fetches that URL
  server-side during authorization. Every installed binary, including already-released
  ones, loses the ability to log in. Check it survives any docs reshuffle:
  `curl -sI https://rushi.github.io/sumologic-cli/client.json`
