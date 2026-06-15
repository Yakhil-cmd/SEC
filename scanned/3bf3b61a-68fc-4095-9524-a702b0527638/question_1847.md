# Q1847: Get Title distribution invariant edge b08e

## Question
Can an unprivileged attacker reach `GetTitle` in `sei-cosmos/x/distribution/types/proposal.go` via public distribution withdrawal, community pool, or reward query/message flow, controlling withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing, and make rewards, commission, or community-pool balances diverge from bank module balances so that the invariant `delegator and validator reward accounting must not be claimable twice or redirected by public inputs` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/distribution/types/proposal.go:30` `GetTitle`
- Entrypoint: public distribution withdrawal, community pool, or reward query/message flow
- Attacker controls: withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing
- Exploit idea: make rewards, commission, or community-pool balances diverge from bank module balances
- Invariant to test: delegator and validator reward accounting must not be claimable twice or redirected by public inputs
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Drive reward accrual and withdrawal in a keeper test, repeat around period changes, and assert bank balances plus outstanding rewards are conserved.
