# Q1946: New Query Delegation Rewards Params distribution invariant edge 0424

## Question
Can an unprivileged attacker reach `NewQueryDelegationRewardsParams` in `sei-cosmos/x/distribution/types/querier.go` via public distribution withdrawal, community pool, or reward query/message flow, controlling withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing, and make rewards, commission, or community-pool balances diverge from bank module balances so that the invariant `rewards, commission, community pool, and bank balances must remain conserved across every withdrawal and period update` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/distribution/types/querier.go:67` `NewQueryDelegationRewardsParams`
- Entrypoint: public distribution withdrawal, community pool, or reward query/message flow
- Attacker controls: withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing
- Exploit idea: make rewards, commission, or community-pool balances diverge from bank module balances
- Invariant to test: rewards, commission, community pool, and bank balances must remain conserved across every withdrawal and period update
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Drive reward accrual and withdrawal in a keeper test, repeat around period changes, and assert bank balances plus outstanding rewards are conserved.
