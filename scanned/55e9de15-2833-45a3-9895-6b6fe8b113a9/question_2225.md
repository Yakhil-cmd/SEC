# Q2225: Get ABI precompile invariant edge 08bb

## Question
Can an unprivileged attacker reach `GetABI` in `precompiles/bank/bank.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and make the precompile parse user calldata into a different Cosmos action than the ABI-visible intent so that the invariant `ABI-controlled values must not bypass native authorization, denom, amount, or address validation` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `precompiles/bank/bank.go:63` `GetABI`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: make the precompile parse user calldata into a different Cosmos action than the ABI-visible intent
- Invariant to test: ABI-controlled values must not bypass native authorization, denom, amount, or address validation
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
