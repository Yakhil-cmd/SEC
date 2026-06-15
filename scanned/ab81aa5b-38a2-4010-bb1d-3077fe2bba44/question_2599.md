# Q2599: decimals precompile invariant edge 6de3

## Question
Can an unprivileged attacker reach `decimals` in `precompiles/bank/legacy/v555/bank.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and force precompile gas accounting below the Cosmos-side work performed by the call so that the invariant `precompile-visible execution, Cosmos keeper state, EVM revert semantics, and gas accounting must remain atomic and consistent` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `precompiles/bank/legacy/v555/bank.go:345` `decimals`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: force precompile gas accounting below the Cosmos-side work performed by the call
- Invariant to test: precompile-visible execution, Cosmos keeper state, EVM revert semantics, and gas accounting must remain atomic and consistent
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
