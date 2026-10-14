# SPDX-License-Identifier: Apache-2.0
#
# Builds the native helpers: ginn-wait (FR-081, NFR-033), ginn-rebind (ADR-008), ginn-watch
# (FR-044, NFR-009) and ginn-agent-filter (FR-067, NFR-SEC-007). `make check` compiles each with
# gcc and clang with warnings as errors, and checks the 200-line limit (AGENTS.md). `make` builds
# into build/ only; `make install` copies the helpers to $(DESTDIR)$(LIBEXECDIR), and no target
# installs by default.

GCC ?= gcc
CLANG ?= clang
CC ?= cc
WARN := -std=c11 -Wall -Wextra -Werror -O2
PREFIX ?= /usr
LIBEXECDIR ?= $(PREFIX)/libexec/ginn
BUILD := build
HELPERS := ginn-wait ginn-rebind ginn-watch ginn-agent-filter
LINE_LIMIT := 200

all: $(addprefix $(BUILD)/,$(HELPERS))

$(BUILD)/%: %.c
	@mkdir -p $(BUILD)
	$(CC) $(WARN) -o $@ $<

check: lines
	@mkdir -p $(BUILD)/check
	@for h in $(HELPERS); do \
		$(GCC) $(WARN) -o $(BUILD)/check/$$h.gcc $$h.c && \
		$(CLANG) $(WARN) -o $(BUILD)/check/$$h.clang $$h.c || exit 1; \
	done
	@echo "$(HELPERS): gcc and clang clean with $(WARN)"

lines:
	@for h in $(HELPERS); do \
		n=$$(wc -l < $$h.c); \
		if [ "$$n" -gt $(LINE_LIMIT) ]; then \
			echo "$$h.c: $$n lines, limit $(LINE_LIMIT) (AGENTS.md)"; exit 1; \
		fi; \
		echo "$$h.c: $$n lines (limit $(LINE_LIMIT))"; \
	done

install: all
	@for h in $(HELPERS); do \
		install -D -m 0755 $(BUILD)/$$h $(DESTDIR)$(LIBEXECDIR)/$$h || exit 1; \
	done

clean:
	rm -rf $(BUILD)

.PHONY: all check lines install clean
