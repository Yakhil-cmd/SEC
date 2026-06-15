# Q1158: New Filter API evm rpc invariant edge 4d27

## Question
Can an unprivileged attacker reach `NewFilterAPI` in `evmrpc/filter.go` via unauthenticated EVM JSON-RPC request on a default-enabled RPC endpoint, controlling method parameters, block identifiers, addresses, topics, calldata, quantity encodings, and request batching, and force an unbounded historical/state lookup or trace conversion path that allocates or loops before normal limits are enforced so that the invariant `RPC block tags and filters must not make state lookups escape configured limits or return inconsistent execution results` fails, causing `Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints`?

## Target
- File/function: `evmrpc/filter.go:269` `NewFilterAPI`
- Entrypoint: unauthenticated EVM JSON-RPC request on a default-enabled RPC endpoint
- Attacker controls: method parameters, block identifiers, addresses, topics, calldata, quantity encodings, and request batching
- Exploit idea: force an unbounded historical/state lookup or trace conversion path that allocates or loops before normal limits are enforced
- Invariant to test: RPC block tags and filters must not make state lookups escape configured limits or return inconsistent execution results
- Expected Immunefi impact: Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints
- Fast validation: Write a direct RPC regression harness with default config, cap request size normally, and assert bounded latency plus no panic for malformed and maximal inputs.
