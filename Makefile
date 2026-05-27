.PHONY: build test install fmt vet tidy clean

BIN := comms
INSTALL_DIR := $(HOME)/.local/bin

build:
	go build -o $(BIN) ./cmd/comms

test:
	go test ./...

install:
	@mkdir -p $(INSTALL_DIR)
	go build -o $(INSTALL_DIR)/$(BIN) ./cmd/comms
	@echo "Installed $(BIN) → $(INSTALL_DIR)/$(BIN)"
	@echo "Make sure $(INSTALL_DIR) is on \$$PATH."

fmt:
	gofmt -w .

vet:
	go vet ./...

tidy:
	go mod tidy

clean:
	rm -f $(BIN)
	go clean ./...
