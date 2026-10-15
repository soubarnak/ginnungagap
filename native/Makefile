# SPDX-License-Identifier: Apache-2.0
#
# Builds the native helpers: ginn-wait (FR-081, NFR-033), ginn-rebind (ADR-008), ginn-watch
# (FR-044, NFR-009), ginn-agent-filter (FR-067, NFR-SEC-007), ginn-idle (FR-018) and the exec relay
# ginn-exec-relay, ginn-exec-worker, ginn-exec-run and ginn-exec-kill (ADR-014, FR-049). `make check` compiles each with
# gcc and clang with warnings as errors, and checks the 200-line limit (AGENTS.md). A helper that
# includes ginn-exec-frame.h counts the header's lines toward its limit. `make` builds into build/
# only; `make install` copies the helpers to $(DESTDIR)$(LIBEXECDIR), and no target installs by default.

GCC ?= gcc
CLANG ?= clang
CC ?= cc
WARN := -std=c11 -Wall -Wextra -Werror -O2
# The hardened build (xbps-src passes FORTIFY): _FORTIFY_SOURCE turns on the warn_unused_result attributes.
HARDEN := $(WARN) -D_FORTIFY_SOURCE=2
PREFIX ?= /usr
LIBEXECDIR ?= $(PREFIX)/libexec/ginn
BUILD := build
HELPERS := ginn-wait ginn-rebind ginn-watch ginn-agent-filter ginn-idle ginn-exec-relay ginn-exec-worker ginn-exec-run ginn-exec-kill
LINE_LIMIT := 200

all: $(addprefix $(BUILD)/,$(HELPERS))

$(BUILD)/%: %.c
	@mkdir -p $(BUILD)
	$(CC) $(WARN) -o $@ $<

# The frame header is a prerequisite of every helper that includes it (ADR-014, Wire protocol). The kill
# helper does not include it (its one define is written out in the source), so the frame's lines are not its.
$(BUILD)/ginn-exec-relay $(BUILD)/ginn-exec-worker $(BUILD)/ginn-exec-run: ginn-exec-frame.h

check: lines
	@mkdir -p $(BUILD)/check
	@for h in $(HELPERS); do \
		$(GCC) $(WARN) -o $(BUILD)/check/$$h.gcc $$h.c && \
		$(CLANG) $(WARN) -o $(BUILD)/check/$$h.clang $$h.c || exit 1; \
	done
	@echo "$(HELPERS): gcc and clang clean with $(WARN)"
	@for h in $(HELPERS); do \
		$(GCC) $(HARDEN) -o $(BUILD)/check/$$h.fortify $$h.c || exit 1; \
	done
	@echo "$(HELPERS): gcc clean with $(HARDEN) (the hardened build Void uses)"

lines:
	@hdr=$$(wc -l < ginn-exec-frame.h); \
	for h in $(HELPERS); do \
		n=$$(wc -l < $$h.c); \
		if grep -q '^#include "ginn-exec-frame.h"' $$h.c; then n=$$((n + hdr)); fi; \
		if [ "$$n" -gt $(LINE_LIMIT) ]; then \
			echo "$$h.c: $$n lines with its shared header, limit $(LINE_LIMIT) (AGENTS.md)"; exit 1; \
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
