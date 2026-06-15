# Q0892: Sub Unlocked Coins bank invariant edge 7092

## Question
Can an unprivileged attacker reach `SubUnlockedCoins` in `sei-cosmos/x/bank/keeper/send.go` via public bank send, multi-send, metadata, supply, or balance query/message flow, controlling sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations, and make bank supply, balances, metadata, or send restrictions disagree by using denom/address/amount edge cases so that the invariant `public bank messages and queries must be bounded, panic-free, and reject invalid denoms/amount vectors before state changes` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/bank/keeper/send.go:210` `SubUnlockedCoins`
- Entrypoint: public bank send, multi-send, metadata, supply, or balance query/message flow
- Attacker controls: sender/receiver addresses, denom strings, amount vectors, metadata fields, pagination, and module-account-adjacent destinations
- Exploit idea: make bank supply, balances, metadata, or send restrictions disagree by using denom/address/amount edge cases
- Invariant to test: public bank messages and queries must be bounded, panic-free, and reject invalid denoms/amount vectors before state changes
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Execute send/multisend/query variants with attacker-controlled denoms and amount vectors, then assert supply, balances, blocked-address checks, and panic-free behavior.
