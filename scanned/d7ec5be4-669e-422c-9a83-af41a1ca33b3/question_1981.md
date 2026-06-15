# Q1981: New HTTPServer evm rpc invariant edge f160

## Question
Can an unprivileged attacker reach `NewHTTPServer` in `evmrpc/rpcstack.go` via unauthenticated EVM JSON-RPC request on a default-enabled RPC endpoint, controlling method parameters, block identifiers, addresses, topics, calldata, quantity encodings, and request batching, and force an unbounded historical/state lookup or trace conversion path that allocates or loops before normal limits are enforced so that the invariant `unauthenticated RPC requests must be bounded, panic-free, and reflect canonical committed EVM/Cosmos state` fails, causing `Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints`?

## Target
- File/function: `evmrpc/rpcstack.go:102` `NewHTTPServer`
- Entrypoint: unauthenticated EVM JSON-RPC request on a default-enabled RPC endpoint
- Attacker controls: method parameters, block identifiers, addresses, topics, calldata, quantity encodings, and request batching
- Exploit idea: force an unbounded historical/state lookup or trace conversion path that allocates or loops before normal limits are enforced
- Invariant to test: unauthenticated RPC requests must be bounded, panic-free, and reflect canonical committed EVM/Cosmos state
- Expected Immunefi impact: Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints
- Fast validation: Write a direct RPC regression harness with default config, cap request size normally, and assert bounded latency plus no panic for malformed and maximal inputs.
