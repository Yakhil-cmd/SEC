# Q0271: Denoms From Creator tokenfactory invariant edge 353f

## Question
Can an unprivileged attacker reach `DenomsFromCreator` in `x/tokenfactory/keeper/grpc_query.go` via public tokenfactory message submitted by an account or contract binding, controlling denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings, and use denom or authority edge values to mint, burn, move, or freeze tokens outside the intended ownership rules so that the invariant `tokenfactory authority, bank supply, balances, metadata, and denom ownership must remain consistent after every public flow` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `x/tokenfactory/keeper/grpc_query.go:33` `DenomsFromCreator`
- Entrypoint: public tokenfactory message submitted by an account or contract binding
- Attacker controls: denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings
- Exploit idea: use denom or authority edge values to mint, burn, move, or freeze tokens outside the intended ownership rules
- Invariant to test: tokenfactory authority, bank supply, balances, metadata, and denom ownership must remain consistent after every public flow
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Use keeper/msg-server tests with attacker-owned accounts and denoms, then assert authority, bank supply, balances, and metadata before and after the edge sequence.
