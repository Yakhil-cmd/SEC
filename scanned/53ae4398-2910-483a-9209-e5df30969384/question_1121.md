# Q1121: Set Denom Allow List bank invariant edge 2507

## Question
Can an unprivileged attacker reach `SetDenomAllowList` in `sei-cosmos/x/bank/keeper/send.go` via public bank send, multi-send, metadata, supply, or balance query/message flow, controlling sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations, and cause a transfer path to credit without matching debit or freeze funds in a module/account edge case so that the invariant `bank balances, total supply, blocked addresses, metadata, and send restrictions must remain conserved and consistently enforced` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/bank/keeper/send.go:453` `SetDenomAllowList`
- Entrypoint: public bank send, multi-send, metadata, supply, or balance query/message flow
- Attacker controls: sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations
- Exploit idea: cause a transfer path to credit without matching debit or freeze funds in a module/account edge case
- Invariant to test: bank balances, total supply, blocked addresses, metadata, and send restrictions must remain conserved and consistently enforced
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Execute send/multisend/query variants with attacker-controlled denoms and amount vectors, then assert supply, balances, blocked-address checks, and panic-free behavior.
