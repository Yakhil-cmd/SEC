# Q0186: validate Allow List tokenfactory invariant edge 2e4f

## Question
Can an unprivileged attacker reach `validateAllowList` in `x/tokenfactory/keeper/createdenom.go` via public tokenfactory message submitted by an account or contract binding, controlling denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings, and make one accepted denom representation resolve to a different bank denom in a later module path so that the invariant `one denom string must not resolve to different assets or authorities across tokenfactory, bank, wasm, and EVM paths` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `x/tokenfactory/keeper/createdenom.go:101` `validateAllowList`
- Entrypoint: public tokenfactory message submitted by an account or contract binding
- Attacker controls: denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings
- Exploit idea: make one accepted denom representation resolve to a different bank denom in a later module path
- Invariant to test: one denom string must not resolve to different assets or authorities across tokenfactory, bank, wasm, and EVM paths
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Use keeper/msg-server tests with attacker-owned accounts and denoms, then assert authority, bank supply, balances, and metadata before and after the edge sequence.
