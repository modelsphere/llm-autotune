#!/usr/bin/env bash
# Copy the SDK out to every policy repository.
#
# Policies vendor autotune_policy/ rather than depending on it: it is stdlib-only
# so that a policy image needs no wheels and no reachable package index, which is
# the difference between "works" and "does not work" on an air-gapped GPU host.
# The price is copies, and this script plus the `policy-sdk` CI job is how they
# are kept honest.
#
# Run it after changing policies/autotune_policy/, then commit inside each
# submodule and move the pointer here.
set -euo pipefail

cd "$(dirname "$0")/.."
src=policies/autotune_policy

for policy in policies/random-search policies/chaos; do
  if [ ! -e "$policy/.git" ]; then
    echo "$policy is not checked out — run: git submodule update --init" >&2
    exit 1
  fi
  rsync -a --delete --exclude='__pycache__' "$src/" "$policy/autotune_policy/"
  echo "synced -> $policy/autotune_policy/"
done

echo
echo "Now, in each submodule with changes: commit and push, then back here:"
echo "  git add policies/random-search policies/chaos && git commit"
