# Q0942: set Balance bank invariant edge 10bf

## Question
Can an unprivileged attacker reach `setBalance` in `sei-cosmos/x/bank/keeper/send.go` via public bank send, multi-send, metadata, supply, or balance query/message flow, controlling sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations, and make bank supply, balances, metadata, or send restrictions disagree by using denom/address/amount edge cases so that the invariant `bank balances, total supply, blocked addresses, metadata, and send restrictions must remain conserved and consistently enforced` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/bank/keeper/send.go:298` `setBalance`
- Entrypoint: public bank send, multi-send, metadata, supply, or balance query/message flow
- Attacker controls: sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations
- Exploit idea: make bank supply, balances, metadata, or send restrictions disagree by using denom/address/amount edge cases
- Invariant to test: bank balances, total supply, blocked addresses, metadata, and send restrictions must remain conserved and consistently enforced
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Execute send/multisend/query variants with attacker-controlled denoms and amount vectors, then assert supply, balances, blocked-address checks, and panic-free behavior.
