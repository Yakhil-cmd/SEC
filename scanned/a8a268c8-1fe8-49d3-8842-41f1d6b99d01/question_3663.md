# Q3663: New Sei Transaction API evm rpc invariant edge 08ec

## Question
Can an unprivileged attacker reach `NewSeiTransactionAPI` in `evmrpc/tx.go` via unauthenticated EVM JSON-RPC request on a default-enabled RPC endpoint, controlling method parameters, block identifiers, addresses, topics, calldata, quantity encodings, and request batching, and trigger malformed quantity or topic handling that bypasses request validation and crashes the RPC worker so that the invariant `RPC block tags and filters must not make state lookups escape configured limits or return inconsistent execution results` fails, causing `Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints`?

## Target
- File/function: `evmrpc/tx.go:80` `NewSeiTransactionAPI`
- Entrypoint: unauthenticated EVM JSON-RPC request on a default-enabled RPC endpoint
- Attacker controls: method parameters, block identifiers, addresses, topics, calldata, quantity encodings, and request batching
- Exploit idea: trigger malformed quantity or topic handling that bypasses request validation and crashes the RPC worker
- Invariant to test: RPC block tags and filters must not make state lookups escape configured limits or return inconsistent execution results
- Expected Immunefi impact: Medium: Crash of RPC nodes running default configuration via direct unauthenticated network access to RPC/gRPC endpoints
- Fast validation: Write a direct RPC regression harness with default config, cap request size normally, and assert bounded latency plus no panic for malformed and maximal inputs.
