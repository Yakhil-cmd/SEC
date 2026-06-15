# Q0807: Get Signers tokenfactory invariant edge 8a98

## Question
Can an unprivileged attacker reach `GetSigners` in `x/tokenfactory/types/msgs.go` via public tokenfactory message submitted by an account or contract binding, controlling denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings, and break supply/accounting invariants through repeated mint/burn/metadata/update flows across hooks and wasm bindings so that the invariant `one denom string must not resolve to different assets or authorities across tokenfactory, bank, wasm, and EVM paths` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `x/tokenfactory/types/msgs.go:49` `GetSigners`
- Entrypoint: public tokenfactory message submitted by an account or contract binding
- Attacker controls: denom names, metadata, mint/burn amounts, authority-controlled fields exposed to users, hooks, and wasm bindings
- Exploit idea: break supply/accounting invariants through repeated mint/burn/metadata/update flows across hooks and wasm bindings
- Invariant to test: one denom string must not resolve to different assets or authorities across tokenfactory, bank, wasm, and EVM paths
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Use keeper/msg-server tests with attacker-owned accounts and denoms, then assert authority, bank supply, balances, and metadata before and after the edge sequence.
