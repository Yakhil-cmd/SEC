# Q1228: Get Query Cmd distribution invariant edge 5388

## Question
Can an unprivileged attacker reach `GetQueryCmd` in `sei-cosmos/x/distribution/module.go` via public distribution withdrawal, community pool, or reward query/message flow, controlling withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing, and withdraw or redirect rewards using stale validator/delegator accounting, rounding, or withdraw-address edge cases so that the invariant `delegator and validator reward accounting must not be claimable twice or redirected by public inputs` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/distribution/module.go:91` `GetQueryCmd`
- Entrypoint: public distribution withdrawal, community pool, or reward query/message flow
- Attacker controls: withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing
- Exploit idea: withdraw or redirect rewards using stale validator/delegator accounting, rounding, or withdraw-address edge cases
- Invariant to test: delegator and validator reward accounting must not be claimable twice or redirected by public inputs
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Drive reward accrual and withdrawal in a keeper test, repeat around period changes, and assert bank balances plus outstanding rewards are conserved.
