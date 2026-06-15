# Q2766: send precompile invariant edge 1b64

## Question
Can an unprivileged attacker reach `send` in `precompiles/bank/legacy/v580/bank.go` via EVM transaction calling the public precompile address, controlling precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context, and force precompile gas accounting below the Cosmos-side work performed by the call so that the invariant `precompile-visible execution, Cosmos keeper state, EVM revert semantics, and gas accounting must remain atomic and consistent` fails, causing `Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds`?

## Target
- File/function: `precompiles/bank/legacy/v580/bank.go:121` `send`
- Entrypoint: EVM transaction calling the public precompile address
- Attacker controls: precompile calldata, caller address, value, gas limit, ABI types, repeated calls, and nested EVM execution context
- Exploit idea: force precompile gas accounting below the Cosmos-side work performed by the call
- Invariant to test: precompile-visible execution, Cosmos keeper state, EVM revert semantics, and gas accounting must remain atomic and consistent
- Expected Immunefi impact: Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds
- Fast validation: Build an EVM test transaction calling the precompile with edge ABI values, then assert native keeper state, EVM receipt, revert behavior, and gas accounting remain atomic.
