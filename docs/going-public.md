# Going public (checklist)

Decisions (2026-11-15): the public repo is a fresh repo whose first commit is
the code at that point, with no history and no personal email. It keeps the
name `nealt9000/ytop`, so `REPO` in `src/ytop/product.py` and the ffmpeg URLs
in `stick/versions.toml` stay as they are. The private repo is renamed
`nealt9000/ytop-archive` and kept as the archive. Internal history (the P0
spike, agent plans, kickoff prompts) was removed from the tree before the
export and lives only in the archive. Licence is MIT. The name stays "YTOP"
for now.

`tools/make_public_repo.sh` never contacts GitHub. The owner runs every `gh`
and `git push` command below.

1. **Merge to master** in the private repo, or pass `--ref <ref>` in step 2.
2. **Run the script** from the private repo:
   `tools/make_public_repo.sh ../ytop-public --name "<display name>" --email "<id>+<login>@users.noreply.github.com"`
   It exports `master` (or `--ref <ref>`) with `git archive`, checks the tree,
   then makes one commit on `main` with that identity as author and committer.
   It never reads your git config, refuses a non-empty target directory, and
   refuses an email that is not a GitHub noreply address.
3. **Review the privacy output.** Hits for personal-provider emails,
   home-directory paths, private keys and tokens stop the script before any
   commit (matching is case-insensitive, binary files are scanned, and a read
   error fails the check); fix them in the private repo and rerun on a new
   directory. Other email-looking strings are only listed as warnings: review
   each (third-party licence files may contain authors' addresses, which is
   normally fine). Binary files (screenshots etc.) are listed under "check
   these by eye": open them and look for personal details.
4. **Rename the private repo** so the name is free:
   `gh repo rename ytop-archive -R nealt9000/ytop --yes`
   `git remote set-url origin https://github.com/nealt9000/ytop-archive.git`
   GitHub redirects the old name until a new repo takes it, which step 5 does.
5. **Create and push** the public repo:
   `gh repo create nealt9000/ytop --public --source ../ytop-public --remote origin`
   `git -C ../ytop-public push -u origin main`
6. **Mirror the ffmpeg-minimal release** byte for byte, so the checksums in
   `stick/versions.toml` still match:
   `gh release download ffmpeg-minimal-1 -R nealt9000/ytop-archive -D ../ffmpeg-minimal-1`
   `gh release create ffmpeg-minimal-1 -R nealt9000/ytop --title ffmpeg-minimal-1 --notes "Minimal ffmpeg + ffprobe for YTOP sticks (tools/ffmpeg-minimal), all 5 platforms verified." ../ffmpeg-minimal-1/*`
   The build uses a GitHub token if one is available and downloads without
   one otherwise, so a public release needs no token.
7. **Confirm the GPL point.** The bundled bgutil-ytdlp-pot-provider server is
   GPL-3.0 (unmodified, licence copy beside it, listed in
   `THIRD_PARTY_NOTICES`). Confirm you are comfortable with that before
   publishing.
8. **Check CI** on the public repo: the CI workflow runs on pushes to `main`.
   Run the **Stick** workflow by hand once, and the **Release** workflow with
   `dry_run` on.
9. **Smoke-test on a real Windows host**: a self-update plus revert from a
   stick. Locked-file renames during self-update are only simulated in tests.
10. **Tag the first public release** following README "Releasing" (bump
    version, annotated tag with release notes, push the tag). Sticks report
    "nothing published" until this exists.
11. **Archive the private repo** (`gh repo archive nealt9000/ytop-archive`)
    once the public one builds and releases correctly.
