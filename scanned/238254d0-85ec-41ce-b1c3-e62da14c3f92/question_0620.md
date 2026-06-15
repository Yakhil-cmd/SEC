# Q0620: Get Token Denom tokenfactory invariant edge f14f

## Question
Can an unprivileged attacker reach `GetTokenDenom` in `x/tokenfactory/types/denoms.go` via public tokenfactory message submitted by an account or contract binding, controlling denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings, and use denom or authority edge values to mint, burn, move, or freeze tokens outside the intended ownership rules so that the invariant `one denom string must not resolve to different assets or authorities across tokenfactory, bank, wasm, and EVM paths` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `x/tokenfactory/types/denoms.go:26` `GetTokenDenom`
- Entrypoint: public tokenfactory message submitted by an account or contract binding
- Attacker controls: denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings
- Exploit idea: use denom or authority edge values to mint, burn, move, or freeze tokens outside the intended ownership rules
- Invariant to test: one denom string must not resolve to different assets or authorities across tokenfactory, bank, wasm, and EVM paths
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Use keeper/msg-server tests with attacker-owned accounts and denoms, then assert authority, bank supply, balances, and metadata before and after the edge sequence.
