# Q3275: Get Code Key wasm invariant edge 9041

## Question
Can an unprivileged attacker reach `GetCodeKey` in `sei-wasmd/x/wasm/types/keys.go` via public CosmWasm instantiate, execute, migrate-restricted user flow, query, or EVM/wasm binding call, controlling contract msg JSON, funds, reply/submessage behavior, query payloads, contract address inputs, and EVM/wasm conversion data, and use address, denom, funds, or JSON edge cases to make wasm bindings execute a different native action than the contract intended so that the invariant `wasm-controlled native bindings, funds movement, replies, and rollbacks must be atomic with contract execution results` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `sei-wasmd/x/wasm/types/keys.go:40` `GetCodeKey`
- Entrypoint: public CosmWasm instantiate, execute, migrate-restricted user flow, query, or EVM/wasm binding call
- Attacker controls: contract msg JSON, funds, reply/submessage behavior, query payloads, contract address inputs, and EVM/wasm conversion data
- Exploit idea: use address, denom, funds, or JSON edge cases to make wasm bindings execute a different native action than the contract intended
- Invariant to test: wasm-controlled native bindings, funds movement, replies, and rollbacks must be atomic with contract execution results
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Deploy a minimal contract that emits the target binding/submessage/query, force success and failure paths, and assert native state rolls back or commits atomically.
