# Q1370: new RPCFunc tm rpc invariant edge bc6d

## Question
Can an unprivileged attacker reach `newRPCFunc` in `sei-tendermint/rpc/jsonrpc/server/rpc_func.go` via unauthenticated Tendermint RPC request on a default-enabled RPC endpoint, controlling RPC query parameters, heights, hashes, event filters, limits, pagination cursors, and malformed encodings, and submit malformed hashes/heights/encodings that reach a panic path instead of returning an RPC error so that the invariant `default RPC endpoints must reject malformed requests cheaply and never crash or allocate unbounded memory` fails, causing `Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints`?

## Target
- File/function: `sei-tendermint/rpc/jsonrpc/server/rpc_func.go:176` `newRPCFunc`
- Entrypoint: unauthenticated Tendermint RPC request on a default-enabled RPC endpoint
- Attacker controls: RPC query parameters, heights, hashes, event filters, limits, pagination cursors, and malformed encodings
- Exploit idea: submit malformed hashes/heights/encodings that reach a panic path instead of returning an RPC error
- Invariant to test: default RPC endpoints must reject malformed requests cheaply and never crash or allocate unbounded memory
- Expected Immunefi impact: Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints
- Fast validation: Reproduce through the RPC handler with default config and measure allocations/latency while asserting malformed requests return errors rather than panics.
