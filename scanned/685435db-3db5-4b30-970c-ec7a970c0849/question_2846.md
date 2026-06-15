# Q2846: New Precompile precompile invariant edge a1a3

## Question
Can an unprivileged attacker reach `NewPrecompile` in `precompiles/bank/legacy/v600/bank.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and force precompile gas accounting below the Cosmos-side work performed by the call so that the invariant `ABI-controlled values must not bypass native authorization, denom, amount, or address validation` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `precompiles/bank/legacy/v600/bank.go:61` `NewPrecompile`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: force precompile gas accounting below the Cosmos-side work performed by the call
- Invariant to test: ABI-controlled values must not bypass native authorization, denom, amount, or address validation
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
