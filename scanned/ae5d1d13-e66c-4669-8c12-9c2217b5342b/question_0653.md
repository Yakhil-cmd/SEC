# Q0653: Evm Proxy tm rpc invariant edge f625

## Question
Can an unprivileged attacker reach `EvmProxy` in `sei-tendermint/internal/rpc/core/mempool.go` via unauthenticated Tendermint RPC request on a default-enabled RPC endpoint, controlling RPC query parameters, heights, hashes, event filters, limits, pagination cursors, and malformed encodings, and drive pagination, event filtering, or height lookup into an uncapped scan that crashes or stalls the default RPC node so that the invariant `default RPC endpoints must reject malformed requests cheaply and never crash or allocate unbounded memory` fails, causing `Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints`?

## Target
- File/function: `sei-tendermint/internal/rpc/core/mempool.go:22` `EvmProxy`
- Entrypoint: unauthenticated Tendermint RPC request on a default-enabled RPC endpoint
- Attacker controls: RPC query parameters, heights, hashes, event filters, limits, pagination cursors, and malformed encodings
- Exploit idea: drive pagination, event filtering, or height lookup into an uncapped scan that crashes or stalls the default RPC node
- Invariant to test: default RPC endpoints must reject malformed requests cheaply and never crash or allocate unbounded memory
- Expected Immunefi impact: Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints
- Fast validation: Reproduce through the RPC handler with default config and measure allocations/latency while asserting malformed requests return errors rather than panics.
