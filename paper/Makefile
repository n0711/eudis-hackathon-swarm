# Build the paper with XeLaTeX. `make data` regenerates the figure tables and
# numbers from the committed evidence logs first (needs the package installed).
PAPER = paper

all: $(PAPER).pdf

$(PAPER).pdf: $(PAPER).tex references.bib data/numbers.tex
	latexmk -xelatex -interaction=nonstopmode -halt-on-error $(PAPER).tex

data:
	cd .. && python paper/tools/make_data.py

clean:
	latexmk -C $(PAPER).tex

.PHONY: all data clean
