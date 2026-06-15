# Q0311: After Validator Created distribution invariant edge 3b53

## Question
Can an unprivileged attacker reach `AfterValidatorCreated` in `sei-cosmos/x/distribution/keeper/hooks.go` via public distribution withdrawal, community pool, or reward query/message flow, controlling withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing, and repeat claim/withdraw flows around period updates to extract more rewards than accrued so that the invariant `delegator and validator reward accounting must not be claimable twice or redirected by public inputs` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/distribution/keeper/hooks.go:20` `AfterValidatorCreated`
- Entrypoint: public distribution withdrawal, community pool, or reward query/message flow
- Attacker controls: withdraw addresses, delegator/validator addresses, reward periods, commission fields, and repeated withdrawal timing
- Exploit idea: repeat claim/withdraw flows around period updates to extract more rewards than accrued
- Invariant to test: delegator and validator reward accounting must not be claimable twice or redirected by public inputs
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Drive reward accrual and withdrawal in a keeper test, repeat around period changes, and assert bank balances plus outstanding rewards are conserved.
