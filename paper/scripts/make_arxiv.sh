#!/usr/bin/env bash
# Build the arXiv submission package: comment-free main.tex, the pre-built
# bibliography (arXiv does not run BibTeX) and the figures. The package is
# then compiled in a scratch directory exactly as arXiv will, with pdflatex
# only, and rejected if any reference or citation is left undefined.
#
#   bash paper/scripts/make_arxiv.sh   ->   paper/build/teleforce-arxiv.tar.gz
set -euo pipefail
cd "$(dirname "$0")/.."

latexmk -pdf -interaction=nonstopmode -halt-on-error -outdir=build main.tex >/dev/null

stage=build/arxiv
rm -rf "$stage"
mkdir -p "$stage/figures"
# arXiv publishes the source: drop full-line comments, keep everything else.
grep -v '^[[:space:]]*%' main.tex >"$stage/main.tex"
cp build/main.bbl "$stage/main.bbl"
cp figures/*.jpg "$stage/figures/"

check=$(mktemp -d)
cp -r "$stage"/. "$check"
(
  cd "$check"
  for _ in 1 2; do
    pdflatex -interaction=nonstopmode -halt-on-error main.tex >/dev/null
  done
  if grep -E "undefined|^!" main.log; then
    echo "arXiv-style build left problems; see $check/main.log" >&2
    exit 1
  fi
)
cp "$check/main.pdf" build/arxiv-check.pdf
rm -rf "$check"

tar -czf build/teleforce-arxiv.tar.gz -C "$stage" .
echo "built build/teleforce-arxiv.tar.gz ($(du -h build/teleforce-arxiv.tar.gz | cut -f1))"
