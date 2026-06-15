# Q0294: Community Pool distribution invariant edge 9694

## Question
Can an unprivileged attacker reach `CommunityPool` in `sei-cosmos/x/distribution/keeper/grpc_query.go` via public distribution withdrawal, community pool, or reward query/message flow, controlling withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing, and withdraw or redirect rewards using stale validator/delegator accounting, rounding, or withdraw-address edge cases so that the invariant `rewards, commission, community pool, and bank balances must remain conserved across every withdrawal and period update` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/distribution/keeper/grpc_query.go:245` `CommunityPool`
- Entrypoint: public distribution withdrawal, community pool, or reward query/message flow
- Attacker controls: withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing
- Exploit idea: withdraw or redirect rewards using stale validator/delegator accounting, rounding, or withdraw-address edge cases
- Invariant to test: rewards, commission, community pool, and bank balances must remain conserved across every withdrawal and period update
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Drive reward accrual and withdrawal in a keeper test, repeat around period changes, and assert bank balances plus outstanding rewards are conserved.
