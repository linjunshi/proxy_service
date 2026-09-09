# Every setting comes from .env, the single source. It is created from the template
# on first use, so no flow depends on remembering to copy it, and no default is
# mirrored here where it could drift from what the stack runs on.
.env:
	cp .env.example $@

include .env

.PHONY: build up down deploy refresh rotate pin logs status nodes

# The refresher's unit tests run inside the build; a failing test fails the build.
build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

deploy:
	git pull
	$(MAKE) build
	$(MAKE) up

# Restarting a sidecar runs its cycle immediately: refresh re-fetches the node list,
# rotate leaves the country in play for the next one. `make rotate` is the answer to a
# block you have actually observed -- a health check cannot see one.
refresh:
	docker compose restart refresher

rotate:
	docker compose restart rotator

# Hold one country -- `make pin C=US` -- until `make up` puts the rotator back.
pin:
	@test -n "$(C)" || { echo "usage: make pin C=<JP|US|TW|KR|DE|IN>"; exit 2; }
	docker compose stop rotator
	docker compose run --rm rotator /app/rotate.py --pin $(C)

logs:
	docker compose logs -f --tail 100

# Five questions, five answers: are the containers up, how many nodes and how fresh,
# which country is in play and through which node, how much of each country is alive,
# and does traffic actually leave through it.
status:
	@docker compose ps
	@line=$$(docker compose exec -T refresher python -c 'import os, time, yaml; p = "/providers/nodes.yaml"; n = yaml.safe_load(open(p))["proxies"]; print("nodes:     %d, published %.0f min ago" % (len(n), (time.time() - os.path.getmtime(p)) / 60))' 2>/dev/null); \
	echo "$${line:-nodes:     unreadable - is the refresher up? (make logs)}"
# The rotator is the component that knows what a country is, so it answers for both
# lines. A pinned deployment has it stopped, which is what the fallback says.
	@report=$$(docker compose exec -T rotator python /app/rotate.py --status 2>/dev/null); \
	echo "$${report:-country:   unreadable - is the rotator up, or pinned? (make logs)}"
# --noproxy on the direct probe: a shell with HTTPS_PROXY exported would otherwise
# measure the tunnel twice and call a real leak healthy.
	@probe=$$(curl -s -m 15 --proxy http://127.0.0.1:$(PROXY_PORT) -w '\n%{time_total}s' https://api.ipify.org); \
	proxied=$$(echo "$$probe" | head -1); elapsed=$$(echo "$$probe" | tail -1); \
	direct=$$(curl -s -m 15 --noproxy '*' https://api.ipify.org); \
	echo "exit:      $${proxied:-refused} through the proxy$${proxied:+ in $$elapsed}, $${direct:-unreachable} direct"; \
	if [ -n "$$proxied" ] && [ "$$proxied" != "$$direct" ]; then \
		echo "verdict:   UP - HTTPS leaves through the tunnel"; \
	elif [ -n "$$proxied" ]; then \
		echo "verdict:   LEAKING - the proxy answers from this host's own address"; \
	else \
		echo "verdict:   DOWN - the proxy refused, which is the intended failure"; \
	fi

# The node names currently published, for checking a region filter.
nodes:
	@docker compose exec -T refresher python -c 'import yaml; print("\n".join(p["name"] for p in yaml.safe_load(open("/providers/nodes.yaml"))["proxies"]))'
