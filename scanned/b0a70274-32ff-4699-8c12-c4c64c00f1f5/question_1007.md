# Q1007: Blocked Addr bank invariant edge f131

## Question
Can an unprivileged attacker reach `BlockedAddr` in `sei-cosmos/x/bank/keeper/send.go` via public bank send, multi-send, metadata, supply, or balance query/message flow, controlling sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations, and force queries or sends into panic/unbounded work through large amount vectors, metadata, or pagination inputs so that the invariant `bank balances, total supply, blocked addresses, metadata, and send restrictions must remain conserved and consistently enforced` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/bank/keeper/send.go:349` `BlockedAddr`
- Entrypoint: public bank send, multi-send, metadata, supply, or balance query/message flow
- Attacker controls: sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations
- Exploit idea: force queries or sends into panic/unbounded work through large amount vectors, metadata, or pagination inputs
- Invariant to test: bank balances, total supply, blocked addresses, metadata, and send restrictions must remain conserved and consistently enforced
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Execute send/multisend/query variants with attacker-controlled denoms and amount vectors, then assert supply, balances, blocked-address checks, and panic-free behavior.
