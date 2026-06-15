# Q0251: Get Name precompile invariant edge ef30

## Question
Can an unprivileged attacker reach `GetName` in `precompiles/addr/legacy/v552/addr.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and bypass denom, address, amount, or authorization checks through ABI edge values and trigger unauthorized state movement so that the invariant `ABI-controlled values must not bypass native authorization, denom, amount, or address validation` fails, causing `Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds`?

## Target
- File/function: `precompiles/addr/legacy/v552/addr.go:93` `GetName`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: bypass denom, address, amount, or authorization checks through ABI edge values and trigger unauthorized state movement
- Invariant to test: ABI-controlled values must not bypass native authorization, denom, amount, or address validation
- Expected Immunefi impact: Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
