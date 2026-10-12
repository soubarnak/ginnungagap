# SPDX-License-Identifier: Apache-2.0
#
# Builds the ginn-rebind helper (ADR-008). `make check` compiles it with gcc and clang with
# warnings as errors, and checks the 200-line limit (AGENTS.md). `make` builds into build/ only;
# `make install` copies the helper to $(DESTDIR)$(LIBEXECDIR), and no target installs by default.

GCC ?= gcc
CLANG ?= clang
CC ?= cc
WARN := -std=c11 -Wall -Wextra -Werror -O2
PREFIX ?= /usr
LIBEXECDIR ?= $(PREFIX)/libexec/ginn
BUILD := build
SRC := ginn-rebind.c
LINE_LIMIT := 200

all: $(BUILD)/ginn-rebind

$(BUILD)/ginn-rebind: $(SRC)
	@mkdir -p $(BUILD)
	$(CC) $(WARN) -o $@ $(SRC)

check: lines
	@mkdir -p $(BUILD)/check
	$(GCC) $(WARN) -o $(BUILD)/check/ginn-rebind.gcc $(SRC)
	$(CLANG) $(WARN) -o $(BUILD)/check/ginn-rebind.clang $(SRC)
	@echo "ginn-rebind: gcc and clang clean with $(WARN)"

lines:
	@n=$$(wc -l < $(SRC)); \
	if [ "$$n" -gt $(LINE_LIMIT) ]; then \
		echo "$(SRC): $$n lines, limit $(LINE_LIMIT) (AGENTS.md)"; exit 1; \
	fi; \
	echo "$(SRC): $$n lines (limit $(LINE_LIMIT))"

install: all
	install -D -m 0755 $(BUILD)/ginn-rebind $(DESTDIR)$(LIBEXECDIR)/ginn-rebind

clean:
	rm -rf $(BUILD)

.PHONY: all check lines install clean
