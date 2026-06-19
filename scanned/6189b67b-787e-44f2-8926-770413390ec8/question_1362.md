# Q1362: sns governance: validate principal replay/idempotency

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/sns/swap/src/types.rs`::validate_principal with attacker-controlled claim tickets, buyer state, finalization timing, ledger transfer metadata, and neuron basket parameters to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state, violating the invariant that sale lifecycle transitions must not be skipped, replayed, or finalized from inconsistent state, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/swap/src/types.rs`::validate_principal
- Entrypoint: publicly reachable validation path
- Attacker controls: claim tickets, buyer state, finalization timing, ledger transfer metadata, and neuron basket parameters
- Exploit idea: bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state
- Invariant to test: sale lifecycle transitions must not be skipped, replayed, or finalized from inconsistent state
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
