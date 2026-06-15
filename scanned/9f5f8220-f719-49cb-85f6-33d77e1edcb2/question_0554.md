# Q0554: Get Query Cmd tokenfactory invariant edge 62ec

## Question
Can an unprivileged attacker reach `GetQueryCmd` in `x/tokenfactory/module.go` via public tokenfactory message submitted by an account or contract binding, controlling denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings, and break supply/accounting invariants through repeated mint/burn/metadata/update flows across hooks and wasm bindings so that the invariant `tokenfactory authority, bank supply, balances, metadata, and denom ownership must remain consistent after every public flow` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `x/tokenfactory/module.go:93` `GetQueryCmd`
- Entrypoint: public tokenfactory message submitted by an account or contract binding
- Attacker controls: denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings
- Exploit idea: break supply/accounting invariants through repeated mint/burn/metadata/update flows across hooks and wasm bindings
- Invariant to test: tokenfactory authority, bank supply, balances, metadata, and denom ownership must remain consistent after every public flow
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Use keeper/msg-server tests with attacker-owned accounts and denoms, then assert authority, bank supply, balances, and metadata before and after the edge sequence.
