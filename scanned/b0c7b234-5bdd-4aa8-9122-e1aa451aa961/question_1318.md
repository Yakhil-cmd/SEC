# Q1318: parse Params tm rpc invariant edge 4a5a

## Question
Can an unprivileged attacker reach `parseParams` in `sei-tendermint/rpc/jsonrpc/server/rpc_func.go` via unauthenticated Tendermint RPC request on a default-enabled RPC endpoint, controlling RPC query parameters, heights, hashes, event filters, limits, pagination cursors, and malformed encodings, and drive pagination, event filtering, or height lookup into an uncapped scan that crashes or stalls the default RPC node so that the invariant `RPC query results must be bounded by request limits and committed index/state consistency` fails, causing `Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints`?

## Target
- File/function: `sei-tendermint/rpc/jsonrpc/server/rpc_func.go:99` `parseParams`
- Entrypoint: unauthenticated Tendermint RPC request on a default-enabled RPC endpoint
- Attacker controls: RPC query parameters, heights, hashes, event filters, limits, pagination cursors, and malformed encodings
- Exploit idea: drive pagination, event filtering, or height lookup into an uncapped scan that crashes or stalls the default RPC node
- Invariant to test: RPC query results must be bounded by request limits and committed index/state consistency
- Expected Immunefi impact: Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints
- Fast validation: Reproduce through the RPC handler with default config and measure allocations/latency while asserting malformed requests return errors rather than panics.
