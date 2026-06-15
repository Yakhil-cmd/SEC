# Q0770: get Evm Addr precompile invariant edge c2fd

## Question
Can an unprivileged attacker reach `getEvmAddr` in `precompiles/addr/legacy/v600/addr.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and cause nested EVM/precompile execution to update Cosmos state while EVM execution later reverts or accounts gas incorrectly so that the invariant `ABI-controlled values must not bypass native authorization, denom, amount, or address validation` fails, causing `Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds`?

## Target
- File/function: `precompiles/addr/legacy/v600/addr.go:130` `getEvmAddr`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: cause nested EVM/precompile execution to update Cosmos state while EVM execution later reverts or accounts gas incorrectly
- Invariant to test: ABI-controlled values must not bypass native authorization, denom, amount, or address validation
- Expected Immunefi impact: Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
