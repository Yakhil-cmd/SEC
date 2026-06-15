# Q1373: Get Tx Cmd bank invariant edge 841b

## Question
Can an unprivileged attacker reach `GetTxCmd` in `sei-cosmos/x/bank/module.go` via public bank send, multi-send, metadata, supply, or balance query/message flow, controlling sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations, and cause a transfer path to credit without matching debit or freeze funds in a module/account edge case so that the invariant `public bank messages and queries must be bounded, panic-free, and reject invalid denoms/amount vectors before state changes` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `sei-cosmos/x/bank/module.go:89` `GetTxCmd`
- Entrypoint: public bank send, multi-send, metadata, supply, or balance query/message flow
- Attacker controls: sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations
- Exploit idea: cause a transfer path to credit without matching debit or freeze funds in a module/account edge case
- Invariant to test: public bank messages and queries must be bounded, panic-free, and reject invalid denoms/amount vectors before state changes
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Execute send/multisend/query variants with attacker-controlled denoms and amount vectors, then assert supply, balances, blocked-address checks, and panic-free behavior.
