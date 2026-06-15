# Q0371: latest Uncommitted Height tm rpc invariant edge 7c3a

## Question
Can an unprivileged attacker reach `latestUncommittedHeight` in `sei-tendermint/internal/rpc/core/env.go` via unauthenticated Tendermint RPC request on a default-enabled RPC endpoint, controlling RPC query parameters, heights, hashes, event filters, limits, pagination cursors, and malformed encodings, and make query results depend on inconsistent committed versus indexed state, enabling clients to observe invalid chain data so that the invariant `RPC query results must be bounded by request limits and committed index/state consistency` fails, causing `Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints`?

## Target
- File/function: `sei-tendermint/internal/rpc/core/env.go:213` `latestUncommittedHeight`
- Entrypoint: unauthenticated Tendermint RPC request on a default-enabled RPC endpoint
- Attacker controls: RPC query parameters, heights, hashes, event filters, limits, pagination cursors, and malformed encodings
- Exploit idea: make query results depend on inconsistent committed versus indexed state, enabling clients to observe invalid chain data
- Invariant to test: RPC query results must be bounded by request limits and committed index/state consistency
- Expected Immunefi impact: Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints
- Fast validation: Reproduce through the RPC handler with default config and measure allocations/latency while asserting malformed requests return errors rather than panics.
