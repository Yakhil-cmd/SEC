# Q2964: Get Code evm rpc invariant edge 0618

## Question
Can an unprivileged attacker reach `GetCode` in `evmrpc/state.go` via unauthenticated EVM JSON-RPC request on a default-enabled RPC endpoint, controlling method parameters, block identifiers, addresses, topics, calldata, quantity encodings, and request batching, and make RPC-visible EVM state disagree with canonical execution state by mixing block tags, pending state, and address association edge cases so that the invariant `unauthenticated RPC requests must be bounded, panic-free, and reflect canonical committed EVM/Cosmos state` fails, causing `Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints`?

## Target
- File/function: `evmrpc/state.go:62` `GetCode`
- Entrypoint: unauthenticated EVM JSON-RPC request on a default-enabled RPC endpoint
- Attacker controls: method parameters, block identifiers, addresses, topics, calldata, quantity encodings, and request batching
- Exploit idea: make RPC-visible EVM state disagree with canonical execution state by mixing block tags, pending state, and address association edge cases
- Invariant to test: unauthenticated RPC requests must be bounded, panic-free, and reflect canonical committed EVM/Cosmos state
- Expected Immunefi impact: Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints
- Fast validation: Write a direct RPC regression harness with default config, cap request size normally, and assert bounded latency plus no panic for malformed and maximal inputs.
