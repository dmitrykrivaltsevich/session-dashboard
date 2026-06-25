# Claude Code · Session Analytics
PYTHON ?= python3
PORT   ?= 8000
ROOT   ?= $(HOME)/.claude/projects

.PHONY: help data serve open clean

help:                ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-8s\033[0m %s\n",$$1,$$2}'

data:                ## Scan transcripts and (re)build data.js
	$(PYTHON) etl.py --root "$(ROOT)"

serve: data          ## Build, then serve the dashboard on http://localhost:$(PORT)
	@echo "Serving on http://localhost:$(PORT)/dashboard.html (Ctrl-C to stop)"
	@$(PYTHON) -m http.server $(PORT)

open: data           ## Build, then open the dashboard in your default browser
	@($(PYTHON) -c "import webbrowser,os; webbrowser.open('file://'+os.path.abspath('dashboard.html'))")

clean:               ## Remove generated data
	rm -f data.js
