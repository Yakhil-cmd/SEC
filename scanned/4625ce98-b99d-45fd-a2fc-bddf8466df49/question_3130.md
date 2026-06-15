# Q3130: name precompile invariant edge 276c

## Question
Can an unprivileged attacker reach `name` in `precompiles/bank/legacy/v603/bank.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and force precompile gas accounting below the Cosmos-side work performed by the call so that the invariant `ABI-controlled values must not bypass native authorization, denom, amount, or address validation` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `precompiles/bank/legacy/v603/bank.go:298` `name`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: force precompile gas accounting below the Cosmos-side work performed by the call
- Invariant to test: ABI-controlled values must not bypass native authorization, denom, amount, or address validation
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
