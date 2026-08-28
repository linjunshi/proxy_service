# Every setting comes from .env, the single source. It is created from the
# template on first use, so no flow depends on remembering to copy it, and no
# default is mirrored here where it could drift from what the stack runs on.
.env:
	cp .env.example $@

include .env

.PHONY: build up down deploy restart recover logs status

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

# Restarting the GUI container re-runs auto-connect.
restart:
	docker compose restart wmsxwd

# Clean-slate restart of the app inside the running container, then re-verify.
# Use when the GUI is stuck (未连接 pill / dead start button) or the tunnel is
# down. Follow with `make logs`; expect a verdict within ~1-3 minutes.
recover:
	docker compose exec wmsxwd recover-app

logs:
	docker compose logs -f --tail 100 wmsxwd

# Tunnel verdict: traffic is tunneled only when the two exit IPs differ.
status:
	docker compose ps
	@proxied=$$(curl -s -m 12 --proxy http://127.0.0.1:$(WMSXWD_PROXY_PORT) http://api.ipify.org); \
	direct=$$(curl -s -m 12 http://api.ipify.org); \
	echo "exit via $(WMSXWD_PROXY_PORT): $${proxied:-unreachable}"; \
	echo "exit direct:    $${direct:-unreachable}"; \
	if [ -n "$$proxied" ] && [ -n "$$direct" ] && [ "$$proxied" != "$$direct" ]; then \
		echo "tunnel: UP"; \
	elif [ -n "$$proxied" ]; then \
		echo "tunnel: NOT tunneled (serving direct rules)"; \
	else \
		echo "tunnel: proxy unreachable for foreign targets"; \
	fi
# Plain HTTP proxying can work while CONNECT fails, and CONNECT is what every
# HTTPS consumer uses -- so it gets its own verdict.
	@code=$$(curl -s -m 15 -o /dev/null -w '%{http_code}' \
		--proxy http://127.0.0.1:$(WMSXWD_PROXY_PORT) https://api.ipify.org); \
	if [ "$$code" = "200" ]; then \
		echo "https (CONNECT): OK"; \
	else \
		echo "https (CONNECT): FAILED - consumers using HTTPS will see EOF"; \
	fi
