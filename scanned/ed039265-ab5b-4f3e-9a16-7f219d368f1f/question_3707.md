# Q3707: decimals precompile invariant edge c9f1

## Question
Can an unprivileged attacker reach `decimals` in `precompiles/bank/legacy/v620/bank.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and bypass denom, address, amount, or authorization checks through ABI edge values and trigger unauthorized state movement so that the invariant `precompile-visible execution, Cosmos keeper state, EVM revert semantics, and gas accounting must remain atomic and consistent` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `precompiles/bank/legacy/v620/bank.go:335` `decimals`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: bypass denom, address, amount, or authorization checks through ABI edge values and trigger unauthorized state movement
- Invariant to test: precompile-visible execution, Cosmos keeper state, EVM revert semantics, and gas accounting must remain atomic and consistent
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
