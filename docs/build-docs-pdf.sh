#!/usr/bin/env bash
# Regenerate the dispatcher-facing PDFs from their Markdown sources.
#
# WHY THIS EXISTS: the PDFs were previously built by hand with an unrecorded
# pandoc invocation. Issues #644 and #677 both name the same consequence — the
# .md gets edited, the .pdf silently keeps saying the old thing, and dispatchers
# read whichever one they happened to download. Recording the command is what
# stops that.
#
# The settings below were recovered from the committed 2026-07-19 PDF: US
# Letter, 1in margins, Helvetica Neue at 11pt, TOC to depth 2.
#
# Requires pandoc + xelatex (MacTeX). Run from the repo root:
#   bash docs/build-docs-pdf.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# dispatcher-guide is the only doc with a committed PDF; dispatcher-training is
# Markdown-only today. Add a name here if that changes — do not add one
# speculatively, or the repo gains a PDF nobody regenerates.
for doc in dispatcher-guide; do
  src="docs/${doc}.md"
  out="docs/${doc}.pdf"
  [ -f "$src" ] || { echo "skip: $src not found"; continue; }
  echo "building $out"
  pandoc "$src" -o "$out" \
    --toc --toc-depth=2 \
    --pdf-engine=xelatex \
    --include-in-header=docs/pdf-symbols.tex \
    -V mainfont="Helvetica Neue" \
    -V monofont="Menlo" \
    -V geometry:margin=1in \
    -V fontsize=11pt
done

echo "done — commit the regenerated PDFs alongside the .md changes"
