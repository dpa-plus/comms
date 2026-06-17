.PHONY: build test race install fmt vet tidy lint clean version

BIN := comms
INSTALL_DIR := $(HOME)/.local/bin

# Version metadata injected into package main via -ldflags. VERSION derives from
# the latest git tag (e.g. v0.1.0, or v0.1.0-3-gabc123-dirty between releases).
# NOTE: the linker keys the main package as `main`, so it MUST be -X main.Version
# (the full import path silently no-ops — verified on this tree).
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
COMMIT  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo none)
# Commit date (not build time) so `make build` matches the GoReleaser releases,
# which inject {{ .CommitDate }} — keeps the same source reproducible.
DATE    ?= $(shell git log -1 --format=%cI 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
LDFLAGS := -s -w -X main.Version=$(VERSION) -X main.Commit=$(COMMIT) -X main.Date=$(DATE)

build:
	go build -ldflags "$(LDFLAGS)" -o $(BIN) ./cmd/comms

install:
	@mkdir -p $(INSTALL_DIR)
	go build -ldflags "$(LDFLAGS)" -o $(INSTALL_DIR)/$(BIN) ./cmd/comms
	@echo "Installed $(BIN) → $(INSTALL_DIR)/$(BIN)"
	@echo "Make sure $(INSTALL_DIR) is on \$$PATH."

test:
	go test ./...

# Mirrors CI: the ui package runs goroutines + a file watcher, so -race matters.
race:
	go test -race ./...

fmt:
	gofmt -w .

vet:
	go vet ./...

lint:
	golangci-lint run ./...

tidy:
	go mod tidy

version: build
	@./$(BIN) version

clean:
	rm -f $(BIN)
	go clean ./...
