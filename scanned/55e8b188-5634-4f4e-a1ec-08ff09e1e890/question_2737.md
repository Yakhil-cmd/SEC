# Q2737: sdk To Full Delegation wasm invariant edge 408d

## Question
Can an unprivileged attacker reach `sdkToFullDelegation` in `sei-wasmd/x/wasm/keeper/query_plugins.go` via public CosmWasm instantiate, execute, migrate-restricted user flow, query, or EVM/wasm binding call, controlling contract msg JSON, funds, reply/submessage behavior, query payloads, contract address inputs, and EVM/wasm conversion data, and use address, denom, funds, or JSON edge cases to make wasm bindings execute a different native action than the contract intended so that the invariant `wasm-controlled native bindings, funds movement, replies, and rollbacks must be atomic with contract execution results` fails, causing `Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages`?

## Target
- File/function: `sei-wasmd/x/wasm/keeper/query_plugins.go:438` `sdkToFullDelegation`
- Entrypoint: public CosmWasm instantiate, execute, migrate-restricted user flow, query, or EVM/wasm binding call
- Attacker controls: contract msg JSON, funds, reply/submessage behavior, query payloads, contract address inputs, and EVM/wasm conversion data
- Exploit idea: use address, denom, funds, or JSON edge cases to make wasm bindings execute a different native action than the contract intended
- Invariant to test: wasm-controlled native bindings, funds movement, replies, and rollbacks must be atomic with contract execution results
- Expected Immunefi impact: Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages
- Fast validation: Deploy a minimal contract that emits the target binding/submessage/query, force success and failure paths, and assert native state rolls back or commits atomically.
