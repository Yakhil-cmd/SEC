# Q3602: decimals precompile invariant edge 6674

## Question
Can an unprivileged attacker reach `decimals` in `precompiles/bank/legacy/v614/bank.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and cause nested EVM/precompile execution to update Cosmos state while EVM execution later reverts or accounts gas incorrectly so that the invariant `ABI-controlled values must not bypass native authorization, denom, amount, or address validation` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `precompiles/bank/legacy/v614/bank.go:335` `decimals`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: cause nested EVM/precompile execution to update Cosmos state while EVM execution later reverts or accounts gas incorrectly
- Invariant to test: ABI-controlled values must not bypass native authorization, denom, amount, or address validation
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
