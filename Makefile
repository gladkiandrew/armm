# ARMM bot — offline checks only. Nothing here touches the network or places an order.
PY ?= python3
MODULES := $(wildcard armm/*.py) $(wildcard tests/*.py)

# Every target here is phony. NOTE: a target listed here but MISSING a rule is
# silently treated as "already made" — that is how a botched edit once left
# `make check` printing GREEN after compile/test/secrets had been deleted.
# If you remove a target, remove it from this list too.
.PHONY: check compile test secrets no-new-order-paths no-llm-in-decisions demo-lock self-check clean install-service

check: self-check compile test secrets no-new-order-paths no-llm-in-decisions demo-lock
	@echo "make check: GREEN"

# Guard against the failure above: assert every prerequisite of `check` actually
# has a recipe, so a vacuous green is impossible.
self-check:
	@for t in compile test secrets no-new-order-paths no-llm-in-decisions demo-lock; do \
	  if $(MAKE) -q $$t >/dev/null 2>&1; then \
	    echo "FAIL: target $$t has no recipe — 'check' would pass vacuously"; exit 1; fi; \
	done
	@echo "self-check: all check prerequisites have real recipes"

compile:
	@$(PY) -m py_compile $(MODULES) && echo "py_compile: ok ($(words $(MODULES)) modules)"

test:
	@$(PY) -m unittest -q tests.test_armm tests.test_signals tests.test_autopilot tests.test_starvation tests.test_demo tests.test_explain

# Invariant 5: secrets only in ~/armm/.env — never in this repo, never in git.
secrets:
	@if [ -n "$$(git ls-files '*.env' '.env*' 2>/dev/null)" ]; then \
	  echo "FAIL: an env file is tracked by git"; exit 1; fi
	@fail=0; \
	for k in WEEX_API_KEY WEEX_API_SECRET WEEX_API_PASSPHRASE TELEGRAM_BOT_TOKEN ANTHROPIC_API_KEY; do \
	  v=$$(sed -n "s/^$$k=//p" $$HOME/armm/.env 2>/dev/null | head -1 | tr -d "\"'"); \
	  if [ -n "$$v" ] && [ $${#v} -ge 8 ]; then \
	    if git grep -qF -- "$$v" 2>/dev/null; then echo "FAIL: $$k value found in tracked files"; fail=1; fi; \
	    if grep -rqF -- "$$v" . --exclude-dir=__pycache__ --exclude-dir=.git 2>/dev/null; then \
	      echo "FAIL: $$k value found in the working tree"; fail=1; fi; \
	  fi; \
	done; \
	if [ $$fail -eq 0 ]; then echo "secrets: clean (no key fragments in the repo)"; else exit 1; fi

# v0.3 invariant: the signal layer advises, it never places. Assert via the AST
# which modules actually CALL an order function — a grep trips over the phrase
# appearing in a docstring that promises the opposite.
no-new-order-paths:
	@$(PY) -c "import ast,sys,glob; \
	ORDER={'place_order','place_tp_sl'}; \
	ALLOWED={'armm/ladder.py','armm/manager.py'}; \
	callers={f for f in glob.glob('armm/*.py') \
	  for n in ast.walk(ast.parse(open(f).read(),f)) \
	  if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in ORDER}; \
	extra=callers-ALLOWED; \
	sys.exit('FAIL: new order-placement path in '+', '.join(sorted(extra))) if extra else \
	print('order paths: only '+', '.join(sorted(callers))+' can place (unchanged from v0.2)')"
	@n=$$(grep -c 'LIVE_ORDER_PATH = \"/capi/v3/order\"' armm/weex_client.py); \
	if [ "$$n" != "1" ]; then echo "FAIL: live order path constant changed"; exit 1; fi

# v0.6 invariant: Claude explains, it never decides. No module that computes a signal,
# sizes, guards, places or records a trade may import the explainer or the model SDK,
# and the explainer may import nothing that can trade. Asserted from the AST.
no-llm-in-decisions:
	@$(PY) -c "import ast,sys; \
	DECIDE=['guards','ladder','manager','autopilot','signals','advisor','weex_client','store', \
	        'specs','candles','indicators','origin','scoreboard','starvation','research','config']; \
	TRADE={'ladder','manager','autopilot','guards','weex_client','advisor','store'}; \
	imports=lambda f: {(n.module or '').split('.')[-1] if isinstance(n,ast.ImportFrom) and n.module else a.name.split('.')[-1] \
	  for n in ast.walk(ast.parse(open(f).read(),f)) if isinstance(n,(ast.Import,ast.ImportFrom)) \
	  for a in n.names} | {a.name for n in ast.walk(ast.parse(open(f).read(),f)) \
	  if isinstance(n,ast.ImportFrom) and not n.module for a in n.names}; \
	bad=[m for m in DECIDE if imports('armm/'+m+'.py') & {'explain','anthropic'}]; \
	sys.exit('FAIL: decision module imports the explainer/LLM: '+', '.join(bad)) if bad else None; \
	leak=imports('armm/explain.py') & TRADE; \
	sys.exit('FAIL: explain.py imports a trading module: '+', '.join(sorted(leak))) if leak else \
	print('llm: '+str(len(DECIDE))+' decision modules never import the explainer; it imports nothing that trades')"

# v0.4 ACCEPTANCE TEST 1 — autopilot is demo-locked by construction.
# Three independent proofs, so no single edit can quietly undo the lock:
#   1. the live order path is not named anywhere in autopilot.py
#   2. every WeexClient autopilot builds passes the LITERAL True for demo=
#   3. no function in autopilot.py takes a live/force-style parameter
demo-lock:
	@if grep -qE 'LIVE_ORDER_PATH|/capi/v3/order' armm/autopilot.py; then \
	  echo "FAIL: autopilot.py names the live order path"; exit 1; fi
	@$(PY) -c "import ast,sys; \
	tree=ast.parse(open('armm/autopilot.py').read(),'armm/autopilot.py'); \
	lits=[k.value for n in ast.walk(tree) \
	  if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='WeexClient' \
	  for k in n.keywords if k.arg=='demo']; \
	sys.exit('FAIL: autopilot builds no client of its own') if not lits else None; \
	bad=[l for l in lits if not (isinstance(l,ast.Constant) and l.value is True)]; \
	sys.exit('FAIL: demo= is not a hard-coded True') if bad else \
	print('demo-lock: autopilot constructs '+str(len(lits))+' client(s), all demo=True (literal)')"
	@$(PY) -c "import ast,sys; \
	tree=ast.parse(open('armm/autopilot.py').read(),'armm/autopilot.py'); \
	banned={'live','force','allow_live','real','production'}; \
	bad=[n.name for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) \
	  and {a.arg for a in n.args.args+n.args.kwonlyargs} & banned]; \
	sys.exit('FAIL: live/force parameter in '+', '.join(bad)) if bad else \
	print('demo-lock: no function in autopilot.py accepts a live/force switch')"
	@$(PY) -m armm.demo_lock_check

clean:
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + && echo cleaned

install-service:
	@echo "Manual install (by design — the brief says the unit ships as a template):"
	@echo "  sudo cp deploy/armm-bot.service /etc/systemd/system/armm-bot.service"
	@echo "  sudo systemctl daemon-reload && sudo systemctl enable --now armm-bot"
	@echo "  journalctl -u armm-bot -f     # or: tail -f ~/armm/bot.log"
