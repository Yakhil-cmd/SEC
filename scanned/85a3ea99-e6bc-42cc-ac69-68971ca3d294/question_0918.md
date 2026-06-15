# Q0918: get Sei Addr precompile invariant edge 54ce

## Question
Can an unprivileged attacker reach `getSeiAddr` in `precompiles/addr/legacy/v601/addr.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and bypass denom, address, amount, or authorization checks through ABI edge values and trigger unauthorized state movement so that the invariant `precompile-visible execution, Cosmos keeper state, EVM revert semantics, and gas accounting must remain atomic and consistent` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `precompiles/addr/legacy/v601/addr.go:113` `getSeiAddr`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: bypass denom, address, amount, or authorization checks through ABI edge values and trigger unauthorized state movement
- Invariant to test: precompile-visible execution, Cosmos keeper state, EVM revert semantics, and gas accounting must remain atomic and consistent
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
